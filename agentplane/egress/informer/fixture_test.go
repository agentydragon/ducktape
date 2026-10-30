package informer

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"reflect"
	"sync/atomic"
	"testing"
	"time"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/presentation"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/wait"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const host = "api.informer-test.invalid"

var (
	fixtures atomic.Int32

	subject = policyapi.ServiceAccountRef{Namespace: "test-subject", Name: "test-workload"}

	apiConfig *rest.Config
	apiClient client.Client
	apiScheme = runtime.NewScheme()
)

// TestMain runs one API server for every test; each test works in namespaces of its own.
func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) (err error) {
		apiConfig = server.Config
		for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, policyapi.AddToScheme, egressapi.AddToScheme} {
			if err := add(apiScheme); err != nil {
				return err
			}
		}
		apiClient, err = client.New(apiConfig, client.Options{Scheme: apiScheme})
		return err
	}))
}

// fixture is one test's namespaces, its Index, and an Informer against the shared API server.
type fixture struct {
	t        *testing.T
	index    *policy.Index
	informer *Informer

	rules, credentials, elsewhere string
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	prefix := fmt.Sprintf("test-%d", fixtures.Add(1))
	f := &fixture{
		t: t, rules: prefix + "-rules", credentials: prefix + "-credentials", elsewhere: prefix + "-elsewhere",
	}
	for _, namespace := range []string{f.rules, f.credentials, f.elsewhere} {
		f.create(&corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}})
	}
	return f
}

// start runs an Informer over the fixture's namespaces until the test ends.
func (f *fixture) start() {
	f.t.Helper()
	f.startThrough(apiConfig)
}

// startThrough runs an Informer that reaches the API server through base.
func (f *fixture) startThrough(base *rest.Config) {
	f.t.Helper()
	f.index = policy.NewIndex()
	var err error
	f.informer, err = New(base, Config{
		Index: f.index, RulesNamespace: f.rules, CredentialsNamespace: f.credentials,
	})
	if err != nil {
		f.t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		f.informer.Run(ctx)
	}()
	f.t.Cleanup(func() {
		cancel()
		<-done
	})
}

func (f *fixture) eventually(what string, condition func() bool) {
	f.t.Helper()
	err := wait.PollUntilContextTimeout(context.Background(), 20*time.Millisecond, time.Minute, true,
		func(context.Context) (bool, error) { return condition(), nil })
	if err != nil {
		f.t.Fatalf("gave up waiting for %s", what)
	}
}

func (f *fixture) create(object client.Object) {
	f.t.Helper()
	if err := apiClient.Create(context.Background(), object); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fixture) update(object client.Object) {
	f.t.Helper()
	if err := apiClient.Update(context.Background(), object); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fixture) remove(object client.Object) {
	f.t.Helper()
	if err := apiClient.Delete(context.Background(), object); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fixture) policySet(name string, rules ...policyapi.Rule) *policyapi.PolicySet {
	return &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: f.rules, Name: name},
		Spec:       policyapi.PolicySetSpec{Rules: rules},
	}
}

func (f *fixture) policyBinding(name string, sets ...string) *policyapi.PolicyBinding {
	return &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: f.rules, Name: name},
		Spec:       policyapi.PolicyBindingSpec{Subject: subject, PolicySets: sets},
	}
}

func (f *fixture) credential(name, secret string) *egressapi.EgressCredential {
	return credentialObject(f.rules, name, secret)
}

func (f *fixture) secret(namespace, name, value string) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name},
		Data:       map[string][]byte{"token": []byte(value)},
	}
}

func credentialObject(namespace, name, secret string) *egressapi.EgressCredential {
	return &egressapi.EgressCredential{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name},
		Spec: egressapi.EgressCredentialSpec{
			Description: "test credential",
			Source:      egressapi.CredentialSource{SecretRef: &egressapi.SecretKeyRef{Name: secret, Key: "token"}},
			Targets:     []egressapi.Target{{Method: egressapi.SchemeToken, Header: "Authorization", Scheme: "Bearer"}},
		},
	}
}

func egressRule(credential string) policyapi.Rule {
	rule := policyapi.Rule{Type: resources.EgressRuleType, Hosts: []string{host}}
	if credential != "" {
		rule.CredentialRef = &policyapi.CredentialRef{Name: credential}
	}
	return rule
}

// decide is what the index decides for the subject's GET to the test host presenting the credential's
// placeholder, or nothing when credential is empty.
func decide(index *policy.Index, credential string) policy.Decision {
	headers := http.Header{}
	if credential != "" {
		headers.Set("Authorization", "Bearer "+resources.Placeholder(credential))
	}
	return index.Evaluate(subject, policy.Request{Method: http.MethodGet, Host: host, Path: "/", Headers: headers}, nil)
}

func substituted(value string) []presentation.HeaderRewrite {
	return []presentation.HeaderRewrite{{Header: "Authorization", Values: []string{"Bearer " + value}}}
}

func (f *fixture) eventuallyDecides(credential string, want policy.Decision) {
	f.t.Helper()
	f.eventually(fmt.Sprintf("the decision for %q to be %#v", credential, want), func() bool {
		return reflect.DeepEqual(decide(f.index, credential), want)
	})
}
