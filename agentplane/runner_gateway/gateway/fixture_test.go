package gateway

import (
	"context"
	"fmt"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	tlstest "github.com/agentydragon/ducktape/agentplane/runner_transport/testing"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc/metadata"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	testAudience = "test-gateway"
	testSandbox  = "test-sandbox-a"
	testPod      = "test-runner-pod-a"
)

var (
	// api is the API server the binary's tests share; each test's Sandbox is in a namespace of its own.
	api        *kubeapiserver.Server
	namespaces atomic.Int32

	appAccount        = auth.Account{Namespace: "test-apps", Name: "test-app"}
	executorAccount   = auth.Account{Namespace: "test-samplers", Name: "test-sampler"}
	controllerAccount = auth.Account{Namespace: "test-system", Name: "test-sandbox-controller"}
)

func useAPIServer(server *kubeapiserver.Server) error {
	api = server
	return nil
}

// sandboxFixture is a Running Sandbox whose runner Pod is Ready, in a namespace of the test's own, with
// tokens for that runner and for the Gateway's callers, and resolvers that review them.
type sandboxFixture struct {
	namespace                                             string
	sandbox                                               *sandboxapi.Sandbox
	pod                                                   *corev1.Pod
	objects                                               client.Client
	runnerToken, appToken, executorToken, controllerToken string
	callerAuth                                            *auth.Resolver
	ca                                                    *tlstest.CA
	gatewayTLS, runnerTLS                                 runner_transport.Config
}

func newSandboxFixture(t *testing.T) *sandboxFixture {
	t.Helper()
	ctx := t.Context()
	f := &sandboxFixture{namespace: fmt.Sprintf("test-sandboxes-%d", namespaces.Add(1))}
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, sandboxapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	var err error
	if f.objects, err = client.New(api.Config, client.Options{Scheme: scheme}); err != nil {
		t.Fatal(err)
	}
	if _, err := api.ServiceAccount(ctx, auth.Account{Namespace: f.namespace, Name: testSandbox}); err != nil {
		t.Fatal(err)
	}
	f.sandbox = &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: testSandbox, Namespace: f.namespace},
		Spec:       sandboxapi.SandboxSpec{Template: "test-template"},
	}
	if err := f.objects.Create(ctx, f.sandbox); err != nil {
		t.Fatal(err)
	}
	controller := true
	pods := api.Clientset.CoreV1().Pods(f.namespace)
	f.pod, err = pods.Create(ctx, &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: testPod, Namespace: f.namespace,
			Labels: map[string]string{sandboxapi.RunnerCredentialLabel: "test-runner-credential"},
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind, Name: testSandbox,
				UID: f.sandbox.UID, Controller: &controller,
			}},
		},
		Spec: corev1.PodSpec{
			ServiceAccountName: testSandbox,
			Containers:         []corev1.Container{{Name: "runner", Image: "registry.test/unused"}},
		},
	}, metav1.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	f.pod.Status = corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "192.0.2.20", Conditions: []corev1.PodCondition{{
		Type: corev1.PodReady, Status: corev1.ConditionTrue,
	}}}
	if f.pod, err = pods.UpdateStatus(ctx, f.pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	f.sandbox.Status = sandboxapi.SandboxStatus{
		OperatingMode: sandboxapi.OperatingModeRunning,
		Incarnation:   &sandboxapi.Incarnation{Name: f.pod.Name, UID: string(f.pod.UID)},
		Conditions: []metav1.Condition{{
			Type: sandboxapi.ConditionRunning, Status: metav1.ConditionTrue, Reason: "Ready", LastTransitionTime: metav1.Now(),
		}},
	}
	if err := f.objects.Status().Update(ctx, f.sandbox); err != nil {
		t.Fatal(err)
	}
	if f.runnerToken, err = api.PodToken(ctx, f.pod, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.appToken, err = api.WorkloadToken(ctx, appAccount, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.executorToken, err = api.WorkloadToken(ctx, executorAccount, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.controllerToken, err = api.WorkloadToken(ctx, controllerAccount, testAudience); err != nil {
		t.Fatal(err)
	}
	if f.callerAuth, err = auth.NewResolver(api.TokenReviews(), testAudience,
		[]string{appAccount.Namespace, controllerAccount.Namespace, executorAccount.Namespace}); err != nil {
		t.Fatal(err)
	}
	f.ca = tlstest.NewCA(t)
	f.gatewayTLS = f.ca.Issue(t, runner_transport.GatewayURI)
	uri, err := runner_transport.RunnerURI(f.namespace, string(f.sandbox.UID), f.pod.Labels[sandboxapi.RunnerCredentialLabel])
	if err != nil {
		t.Fatal(err)
	}
	f.runnerTLS = f.ca.Issue(t, uri)
	return f
}

// newGateway is a Gateway replica serving the fixture's namespace that names itself holderName at
// holderIP in the control routes it writes.
func (f *sandboxFixture) newGateway(t *testing.T, holderName, holderIP string, executorCallers ...auth.Account) *Gateway {
	t.Helper()
	gateway, err := New(Config{
		Namespace:                f.namespace,
		RunnerSecurity:           runner_transport.Security{Mode: runner_transport.MutualTLS, TLS: &f.gatewayTLS},
		CallerAccounts:           []auth.Account{appAccount},
		ExecutorCallers:          executorCallers,
		SandboxControllerAccount: &controllerAccount,
		HolderName:               holderName, HolderIP: holderIP, ProxyPort: sandboxapi.ControlServicePort,
		ControlTiming: ControlTiming{
			HeartbeatInterval:  time.Second,
			LivenessTimeout:    10 * time.Second,
			LeaseTouchInterval: 5 * time.Second,
		},
	}, f.callerAuth, f.objects, api.Clientset.CoreV1(), api.Clientset.CoordinationV1(), api.Clientset.DiscoveryV1())
	if err != nil {
		t.Fatal(err)
	}
	return gateway
}

func (f *sandboxFixture) callerContext(token string) context.Context {
	return metadata.NewOutgoingContext(context.Background(), metadata.Pairs(
		"authorization", "Bearer "+token,
		sandboxapi.RunnerProxySandboxMetadataKey, testSandbox,
		sandboxapi.RunnerProxySandboxUIDMetadataKey, string(f.sandbox.UID),
		sandboxapi.RunnerProxySandboxOwnerNamespaceMetadataKey, f.namespace,
		sandboxapi.RunnerProxySandboxOwnerNameMetadataKey, testSandbox,
	))
}
