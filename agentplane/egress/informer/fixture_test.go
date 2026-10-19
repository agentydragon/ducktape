package informer

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/presentation"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/bazelbuild/rules_go/go/runfiles"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/wait"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

const (
	// watchPeriod is short so a few cycles fit in a test; the staleness bound is StaleAfterCycles of it.
	watchPeriod = minWatchPeriod
	host        = "api.informer-test.invalid"
)

var (
	fixtures atomic.Int32

	subject = policyapi.ServiceAccountRef{Namespace: "test-subject", Name: "test-workload"}

	apiConfig *rest.Config
	apiClient client.Client
	apiScheme = runtime.NewScheme()
)

// TestMain runs one API server for every test; each test works in namespaces of its own.
func TestMain(m *testing.M) {
	os.Exit(runWithAPIServer(m))
}

func runWithAPIServer(m *testing.M) int {
	asset, err := runfiles.Rlocation("envtest_kubernetes_linux_amd64/kube-apiserver")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	crd, err := runfiles.Rlocation("ducktape/agentplane/crds/manifests/crd-policybindings.yaml")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	existing := false
	environment := &envtest.Environment{
		BinaryAssetsDirectory: filepath.Dir(asset),
		CRDDirectoryPaths:     []string{filepath.Dir(crd)},
		ErrorIfCRDPathMissing: true,
		UseExistingCluster:    &existing,
	}
	if apiConfig, err = environment.Start(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	defer func() {
		if err := environment.Stop(); err != nil {
			fmt.Fprintln(os.Stderr, "stop API server:", err)
		}
	}()
	for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, policyapi.AddToScheme, egressapi.AddToScheme} {
		if err := add(apiScheme); err != nil {
			fmt.Fprintln(os.Stderr, err)
			return 1
		}
	}
	if apiClient, err = client.New(apiConfig, client.Options{Scheme: apiScheme}); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return m.Run()
}

// faults is a transport under the informer's own, where a test can hold back a kind's requests, cut
// the watches a kind has open, and make its next watch answer that the point it resumes from is gone.
type faults struct {
	mu      sync.Mutex
	held    map[policy.Kind]chan struct{}
	expired map[policy.Kind]bool
	open    map[policy.Kind][]io.Closer
	// lists counts how many times each kind was read in full, by LIST or by a streaming list.
	lists map[policy.Kind]int
	// patches counts the writes to each binding's status.
	patches map[string]int
}

func newFaults() *faults {
	return &faults{
		held: map[policy.Kind]chan struct{}{}, expired: map[policy.Kind]bool{}, open: map[policy.Kind][]io.Closer{},
		lists: map[policy.Kind]int{}, patches: map[string]int{},
	}
}

func (f *faults) wrap(next http.RoundTripper) http.RoundTripper {
	return roundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.Method == http.MethodPatch {
			f.mu.Lock()
			f.patches[path.Base(path.Dir(request.URL.Path))]++
			f.mu.Unlock()
			return next.RoundTrip(request)
		}
		kind := policy.Kind(path.Base(request.URL.Path))
		f.mu.Lock()
		gate := f.held[kind]
		f.mu.Unlock()
		if gate != nil {
			select {
			case <-gate:
			case <-request.Context().Done():
				return nil, request.Context().Err()
			}
		}
		query := request.URL.Query()
		watching, streamingList := query.Get("watch") == "true", query.Get("sendInitialEvents") == "true"
		f.mu.Lock()
		if !watching || streamingList {
			f.lists[kind]++
		}
		expire := watching && !streamingList && f.expired[kind]
		delete(f.expired, kind)
		f.mu.Unlock()
		if expire {
			const gone = `{"type":"ERROR","object":{"kind":"Status","apiVersion":"v1","status":"Failure","reason":"Expired","code":410}}` + "\n"
			return &http.Response{
				StatusCode: http.StatusOK, Header: http.Header{"Content-Type": {"application/json"}},
				Body: io.NopCloser(strings.NewReader(gone)), Request: request,
			}, nil
		}
		response, err := next.RoundTrip(request)
		if err == nil && watching {
			f.mu.Lock()
			f.open[kind] = append(f.open[kind], response.Body)
			f.mu.Unlock()
		}
		return response, err
	})
}

// hold makes every request for kind wait until the returned release is called.
func (f *faults) hold(kind policy.Kind) (release func()) {
	gate := make(chan struct{})
	f.mu.Lock()
	f.held[kind] = gate
	f.mu.Unlock()
	return func() {
		f.mu.Lock()
		delete(f.held, kind)
		f.mu.Unlock()
		close(gate)
	}
}

// sever cuts the watches kind has open, as a lost connection does.
func (f *faults) sever(kind policy.Kind) {
	f.mu.Lock()
	streams := f.open[kind]
	delete(f.open, kind)
	f.mu.Unlock()
	for _, stream := range streams {
		_ = stream.Close()
	}
}

// expire makes the next watch of kind resuming from a version answer that it is too old.
func (f *faults) expire(kind policy.Kind) {
	f.mu.Lock()
	f.expired[kind] = true
	f.mu.Unlock()
}

func (f *faults) listCount(kind policy.Kind) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.lists[kind]
}

func (f *faults) patchCount(binding string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.patches[binding]
}

// fixture is one test's namespaces, its Index, and an Informer against the shared API server.
type fixture struct {
	t        *testing.T
	faults   *faults
	index    *policy.Index
	informer *Informer
	started  time.Time

	rules, credentials, elsewhere string
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	prefix := fmt.Sprintf("test-%d", fixtures.Add(1))
	f := &fixture{
		t: t, faults: newFaults(), rules: prefix + "-rules", credentials: prefix + "-credentials", elsewhere: prefix + "-elsewhere",
	}
	for _, namespace := range []string{f.rules, f.credentials, f.elsewhere} {
		f.create(&corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}})
	}
	return f
}

// start runs an Informer over the fixture's namespaces until the test ends.
func (f *fixture) start() {
	f.t.Helper()
	f.started = time.Now()
	f.index = policy.NewIndex(StaleAfterCycles * watchPeriod)
	base := rest.CopyConfig(apiConfig)
	base.Wrap(f.faults.wrap)
	var err error
	f.informer, err = New(base, Config{
		Index: f.index, RulesNamespace: f.rules, CredentialsNamespace: f.credentials, WatchPeriod: watchPeriod,
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

func (f *fixture) binding(name string) *policyapi.PolicyBinding {
	f.t.Helper()
	binding := &policyapi.PolicyBinding{}
	if err := apiClient.Get(context.Background(), client.ObjectKey{Namespace: f.rules, Name: name}, binding); err != nil {
		f.t.Fatal(err)
	}
	return binding
}

// egressReady reads the EgressReady condition the API server holds for a binding, or nil.
func (f *fixture) egressReady(name string) *metav1.Condition {
	f.t.Helper()
	return meta.FindStatusCondition(f.binding(name).Status.Conditions, EgressReady)
}

// eventuallyEgressReady waits for the binding's EgressReady to satisfy accept, and returns it.
func (f *fixture) eventuallyEgressReady(name string, accept func(*metav1.Condition) bool) *metav1.Condition {
	f.t.Helper()
	var condition *metav1.Condition
	f.eventually("EgressReady of "+name, func() bool {
		condition = f.egressReady(name)
		return condition != nil && accept(condition)
	})
	return condition
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
