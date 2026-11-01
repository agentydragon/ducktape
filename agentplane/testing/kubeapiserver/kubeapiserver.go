// Package kubeapiserver runs a real Kubernetes API server and etcd for Go component tests, with
// Agentplane's CRDs installed and no scheduler, controllers or kubelet, and mints the Pod-bound
// ServiceAccount tokens workloads present. Its Python counterpart is
// agentplane/testing/kubernetes_apiserver.py.
package kubeapiserver

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/bazelbuild/rules_go/go/runfiles"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/client-go/kubernetes"
	authenticationclient "k8s.io/client-go/kubernetes/typed/authentication/v1"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

// User is who UserToken authenticates: a user that is no ServiceAccount.
const User = "test-user"

// Server is a running control plane.
type Server struct {
	// Config reaches the API server as a cluster administrator.
	Config    *rest.Config
	Clientset kubernetes.Interface
	// UserToken authenticates User, for the audiences WithUserAudiences names.
	UserToken string

	environment *envtest.Environment
	directory   string
}

// Option configures the control plane before it starts.
type Option func(*envtest.Environment)

// WithAPIServerFlag sets a kube-apiserver flag.
func WithAPIServerFlag(name string, values ...string) Option {
	return func(environment *envtest.Environment) {
		environment.ControlPlane.GetAPIServer().Configure().Set(name, values...)
	}
}

// WithUserAudiences lets UserToken authenticate for audiences. The API server reviews a token that
// names no audience of its own, as a static token does not, only for the audiences it serves.
func WithUserAudiences(audiences ...string) Option {
	return WithAPIServerFlag("api-audiences", audiences...)
}

// Start runs a control plane until Stop.
func Start(options ...Option) (*Server, error) {
	apiServer, err := runfiles.Rlocation("envtest_kubernetes_linux_amd64/kube-apiserver")
	if err != nil {
		return nil, fmt.Errorf("locate kube-apiserver: %w", err)
	}
	crd, err := runfiles.Rlocation("ducktape/agentplane/crds/manifests/crd-sandboxes.yaml")
	if err != nil {
		return nil, fmt.Errorf("locate Agentplane's CRDs: %w", err)
	}
	directory, err := os.MkdirTemp("", "kubeapiserver-")
	if err != nil {
		return nil, fmt.Errorf("create the API server's token directory: %w", err)
	}
	userToken := rand.Text()
	tokens := filepath.Join(directory, "tokens.csv")
	if err := os.WriteFile(tokens, []byte(userToken+","+User+","+User+"-uid\n"), 0o600); err != nil {
		return nil, errors.Join(fmt.Errorf("write the static token file: %w", err), os.RemoveAll(directory))
	}
	environment := &envtest.Environment{
		BinaryAssetsDirectory: filepath.Dir(apiServer),
		CRDDirectoryPaths:     []string{filepath.Dir(crd)},
		ErrorIfCRDPathMissing: true,
		UseExistingCluster:    new(false),
	}
	WithAPIServerFlag("token-auth-file", tokens)(environment)
	for _, option := range options {
		option(environment)
	}
	server := &Server{UserToken: userToken, environment: environment, directory: directory}
	if server.Config, err = environment.Start(); err != nil {
		return nil, errors.Join(fmt.Errorf("start the Kubernetes API server: %w", err), server.Stop())
	}
	if server.Clientset, err = kubernetes.NewForConfig(server.Config); err != nil {
		return nil, errors.Join(fmt.Errorf("build a Kubernetes client: %w", err), server.Stop())
	}
	return server, nil
}

// Stop stops the control plane and removes what it kept on disk.
func (s *Server) Stop() error {
	var stopped error
	if err := s.environment.Stop(); err != nil {
		stopped = fmt.Errorf("stop the Kubernetes API server: %w", err)
	}
	return errors.Join(stopped, os.RemoveAll(s.directory))
}

// Run starts a control plane, hands it to setUp, runs m's tests and stops it. It returns the code for
// os.Exit: m.Run's, or 1 when the control plane or setUp failed.
func Run(m *testing.M, setUp func(*Server) error, options ...Option) int {
	server, err := Start(options...)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	code := 1
	if err := setUp(server); err != nil {
		fmt.Fprintln(os.Stderr, "set up tests on the Kubernetes API server:", err)
	} else {
		code = m.Run()
	}
	if err := server.Stop(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		code = 1
	}
	return code
}

// TokenReviews is the API server's TokenReview API.
func (s *Server) TokenReviews() authenticationclient.TokenReviewInterface {
	return s.Clientset.AuthenticationV1().TokenReviews()
}

// ServiceAccount is account's ServiceAccount, created with its namespace when absent.
func (s *Server) ServiceAccount(ctx context.Context, account auth.Account) (*corev1.ServiceAccount, error) {
	core := s.Clientset.CoreV1()
	namespace := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: account.Namespace}}
	if _, err := core.Namespaces().Create(ctx, namespace, metav1.CreateOptions{}); err != nil && !apierrors.IsAlreadyExists(err) {
		return nil, fmt.Errorf("create namespace %s: %w", account.Namespace, err)
	}
	accounts := core.ServiceAccounts(account.Namespace)
	created, err := accounts.Create(ctx, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: account.Name}}, metav1.CreateOptions{})
	if apierrors.IsAlreadyExists(err) {
		created, err = accounts.Get(ctx, account.Name, metav1.GetOptions{})
	}
	if err != nil {
		return nil, fmt.Errorf("create ServiceAccount %s/%s: %w", account.Namespace, account.Name, err)
	}
	return created, nil
}

// RecreateServiceAccount deletes account's ServiceAccount and creates it again under the same name
// with a new UID, so tokens of the old one stop authenticating. It returns once the API server
// authenticates the new one's tokens: it checks a token's ServiceAccount against a cache that can
// still hold the deleted one for a moment.
func (s *Server) RecreateServiceAccount(ctx context.Context, account auth.Account) error {
	err := s.Clientset.CoreV1().ServiceAccounts(account.Namespace).Delete(ctx, account.Name, metav1.DeleteOptions{})
	if err != nil {
		return fmt.Errorf("delete ServiceAccount %s/%s: %w", account.Namespace, account.Name, err)
	}
	probe, err := s.UnboundToken(ctx, account)
	if err != nil {
		return err
	}
	err = wait.PollUntilContextTimeout(ctx, 100*time.Millisecond, time.Minute, true, func(ctx context.Context) (bool, error) {
		review, err := s.TokenReviews().Create(ctx, &authenticationv1.TokenReview{
			Spec: authenticationv1.TokenReviewSpec{Token: probe},
		}, metav1.CreateOptions{})
		if err != nil {
			return false, err
		}
		return review.Status.Authenticated, nil
	})
	if err != nil {
		return fmt.Errorf("wait for the API server to authenticate recreated ServiceAccount %s/%s: %w", account.Namespace, account.Name, err)
	}
	return nil
}

// Pod is a new Pod running as account, whose ServiceAccount and namespace are created when absent.
// No kubelet runs it: it is what a token is bound to.
func (s *Server) Pod(ctx context.Context, account auth.Account) (*corev1.Pod, error) {
	if _, err := s.ServiceAccount(ctx, account); err != nil {
		return nil, err
	}
	pod, err := s.Clientset.CoreV1().Pods(account.Namespace).Create(ctx, &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{GenerateName: account.Name + "-"},
		Spec: corev1.PodSpec{
			ServiceAccountName: account.Name,
			Containers:         []corev1.Container{{Name: "workload", Image: "registry.test/unused"}},
		},
	}, metav1.CreateOptions{})
	if err != nil {
		return nil, fmt.Errorf("create a Pod running as %s/%s: %w", account.Namespace, account.Name, err)
	}
	return pod, nil
}

// PodToken is a token for audiences of the ServiceAccount pod runs as, bound to pod: the API server
// stops authenticating it once pod is gone, and its review names pod.
func (s *Server) PodToken(ctx context.Context, pod *corev1.Pod, audiences ...string) (string, error) {
	return s.token(ctx, auth.Account{Namespace: pod.Namespace, Name: pod.Spec.ServiceAccountName}, audiences,
		&authenticationv1.BoundObjectReference{Kind: "Pod", APIVersion: "v1", Name: pod.Name, UID: pod.UID})
}

// WorkloadToken is a token for audiences bound to a new Pod running as account.
func (s *Server) WorkloadToken(ctx context.Context, account auth.Account, audiences ...string) (string, error) {
	pod, err := s.Pod(ctx, account)
	if err != nil {
		return "", err
	}
	return s.PodToken(ctx, pod, audiences...)
}

// UnboundToken is a token for audiences of account's ServiceAccount, bound to no Pod, so its review
// names none.
func (s *Server) UnboundToken(ctx context.Context, account auth.Account, audiences ...string) (string, error) {
	if _, err := s.ServiceAccount(ctx, account); err != nil {
		return "", err
	}
	return s.token(ctx, account, audiences, nil)
}

func (s *Server) token(ctx context.Context, account auth.Account, audiences []string, bound *authenticationv1.BoundObjectReference) (string, error) {
	request, err := s.Clientset.CoreV1().ServiceAccounts(account.Namespace).CreateToken(ctx, account.Name, &authenticationv1.TokenRequest{
		Spec: authenticationv1.TokenRequestSpec{Audiences: audiences, BoundObjectRef: bound},
	}, metav1.CreateOptions{})
	if err != nil {
		return "", fmt.Errorf("request a token for %s/%s: %w", account.Namespace, account.Name, err)
	}
	return request.Status.Token, nil
}

// Revoke deletes pod and returns once the API server no longer authenticates token, which is bound to
// it, for audience. The API server reuses an acceptance for up to ten seconds.
func (s *Server) Revoke(ctx context.Context, pod *corev1.Pod, token, audience string) error {
	err := s.Clientset.CoreV1().Pods(pod.Namespace).Delete(ctx, pod.Name, metav1.DeleteOptions{
		Preconditions: metav1.NewUIDPreconditions(string(pod.UID)),
	})
	if err != nil {
		return fmt.Errorf("delete Pod %s/%s: %w", pod.Namespace, pod.Name, err)
	}
	err = wait.PollUntilContextTimeout(ctx, 100*time.Millisecond, time.Minute, true, func(ctx context.Context) (bool, error) {
		review, err := s.TokenReviews().Create(ctx, &authenticationv1.TokenReview{
			Spec: authenticationv1.TokenReviewSpec{Token: token, Audiences: []string{audience}},
		}, metav1.CreateOptions{})
		if err != nil {
			return false, err
		}
		return !review.Status.Authenticated, nil
	})
	if err != nil {
		return fmt.Errorf("wait for the API server to stop authenticating a token bound to Pod %s/%s: %w", pod.Namespace, pod.Name, err)
	}
	return nil
}
