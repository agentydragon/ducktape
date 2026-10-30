package reader

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/wait"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// testRuleType is a real rule type, so the API server admits the sets these tests create. What its
// reader makes of the rules is not under test here.
const testRuleType = "egress"

// testCondition is the condition the reader under test keeps on each binding.
var testCondition = Condition{Type: "TestReady", FieldManager: "agentplane-reader-test", Rules: "test rules", Reason: "TestEnforced"}

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
		for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, policyapi.AddToScheme} {
			if err := add(apiScheme); err != nil {
				return err
			}
		}
		apiClient, err = client.New(apiConfig, client.Options{Scheme: apiScheme})
		return err
	}))
}

// faults is a transport under the informer's own, where a test can hold back a kind's requests, fail
// them, cut the watches a kind has open, and make its next watch answer that the point it resumes
// from is gone.
type faults struct {
	mu      sync.Mutex
	held    map[watchhealth.Kind]chan struct{}
	failing map[watchhealth.Kind]bool
	expired map[watchhealth.Kind]bool
	open    map[watchhealth.Kind][]io.Closer
	// lists counts how many times each kind was read in full, by LIST or by a streaming list.
	lists map[watchhealth.Kind]int
	// patches counts the writes to each binding's status.
	patches map[string]int
}

func newFaults() *faults {
	return &faults{
		held: map[watchhealth.Kind]chan struct{}{}, failing: map[watchhealth.Kind]bool{},
		expired: map[watchhealth.Kind]bool{}, open: map[watchhealth.Kind][]io.Closer{},
		lists: map[watchhealth.Kind]int{}, patches: map[string]int{},
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) { return f(request) }

func (f *faults) wrap(next http.RoundTripper) http.RoundTripper {
	return roundTripFunc(func(request *http.Request) (*http.Response, error) {
		if request.Method == http.MethodPatch {
			f.mu.Lock()
			f.patches[path.Base(path.Dir(request.URL.Path))]++
			f.mu.Unlock()
			return next.RoundTrip(request)
		}
		kind := watchhealth.Kind(path.Base(request.URL.Path))
		f.mu.Lock()
		gate, failing := f.held[kind], f.failing[kind]
		f.mu.Unlock()
		if failing {
			return nil, errors.New("test fault: the API server is unreachable")
		}
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
func (f *faults) hold(kind watchhealth.Kind) (release func()) {
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

// fail makes every request for kind fail until the returned restore is called.
func (f *faults) fail(kind watchhealth.Kind) (restore func()) {
	f.mu.Lock()
	f.failing[kind] = true
	f.mu.Unlock()
	return func() {
		f.mu.Lock()
		delete(f.failing, kind)
		f.mu.Unlock()
	}
}

// sever cuts the watches kind has open, as a lost connection does.
func (f *faults) sever(kind watchhealth.Kind) {
	f.mu.Lock()
	streams := f.open[kind]
	delete(f.open, kind)
	f.mu.Unlock()
	for _, stream := range streams {
		_ = stream.Close()
	}
}

// expire makes the next watch of kind resuming from a version answer that it is too old.
func (f *faults) expire(kind watchhealth.Kind) {
	f.mu.Lock()
	f.expired[kind] = true
	f.mu.Unlock()
}

func (f *faults) listCount(kind watchhealth.Kind) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.lists[kind]
}

func (f *faults) patchCount(binding string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.patches[binding]
}

// fixture is one test's namespace, and a reader of it against the shared API server.
type fixture struct {
	t         *testing.T
	faults    *faults
	health    *watchhealth.Health
	index     *Index[[]string]
	reader    *rules[[]string]
	namespace string
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	f := &fixture{t: t, faults: newFaults(), namespace: fmt.Sprintf("test-%d", fixtures.Add(1))}
	f.create(&corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: f.namespace}})
	return f
}

// start runs a reader of the fixture's namespace until the test ends.
func (f *fixture) start() {
	f.t.Helper()
	f.health = watchhealth.New()
	f.index = NewIndex(testRuleType, parseHosts)
	base := rest.CopyConfig(apiConfig)
	base.Wrap(f.faults.wrap)
	watches := NewWatches(base, f.health)
	var err error
	if f.reader, err = watchRules(watches, f.index, f.namespace, testCondition); err != nil {
		f.t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		watches.Run(ctx)
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
	if err := apiClient.Get(context.Background(), client.ObjectKey{Namespace: f.namespace, Name: name}, binding); err != nil {
		f.t.Fatal(err)
	}
	return binding
}

// eventuallyCondition waits for the binding's condition to satisfy accept, and returns it.
func (f *fixture) eventuallyCondition(name string, accept func(*metav1.Condition) bool) *metav1.Condition {
	f.t.Helper()
	var condition *metav1.Condition
	f.eventually(testCondition.Type+" of "+name, func() bool {
		condition = meta.FindStatusCondition(f.binding(name).Status.Conditions, testCondition.Type)
		return condition != nil && accept(condition)
	})
	return condition
}

func (f *fixture) policySet(name string, rules ...policyapi.Rule) *policyapi.PolicySet {
	return &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: f.namespace, Name: name},
		Spec:       policyapi.PolicySetSpec{Rules: rules},
	}
}

func (f *fixture) policyBinding(name string, sets ...string) *policyapi.PolicyBinding {
	return &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: f.namespace, Name: name},
		Spec:       policyapi.PolicyBindingSpec{Subject: subject, PolicySets: sets},
	}
}

// testRule is a rule of the test type, which the API server admits and parseHosts reads.
func testRule() policyapi.Rule {
	return policyapi.Rule{Type: testRuleType, Hosts: []string{"reader-test.invalid"}}
}

// parseHosts reads a rule as its hosts, refusing one with none.
func parseHosts(rule policyapi.Rule) ([]string, error) {
	if len(rule.Hosts) == 0 {
		return nil, errors.New("names no host")
	}
	return rule.Hosts, nil
}
