package informer

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/wait"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
)

var (
	fixtures atomic.Int32

	server    *kubeapiserver.Server
	apiClient client.Client
	apiScheme = runtime.NewScheme()
)

// TestMain runs one API server for every test; each test works in namespaces of its own.
func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(started *kubeapiserver.Server) (err error) {
		server = started
		for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, policyapi.AddToScheme} {
			if err := add(apiScheme); err != nil {
				return err
			}
		}
		apiClient, err = client.New(server.Config, client.Options{Scheme: apiScheme})
		return err
	}))
}

// fixture is one test's namespaces, its Index, and an Informer against the shared API server.
type fixture struct {
	t      *testing.T
	index  *Index
	faults *failingPaths
	// policy holds the PolicySets and PolicyBindings, callers and moreCallers the callers, and elsewhere is a
	// namespace the Informer does not read.
	policy, callers, moreCallers, elsewhere string
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	prefix := fmt.Sprintf("test-%d", fixtures.Add(1))
	f := &fixture{
		t: t, faults: &failingPaths{failing: map[string]bool{}, open: map[string][]io.Closer{}},
		policy: prefix + "-policy", callers: prefix + "-callers", moreCallers: prefix + "-more-callers", elsewhere: prefix + "-elsewhere",
	}
	for _, namespace := range []string{f.policy, f.callers, f.moreCallers, f.elsewhere} {
		f.create(&corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}})
	}
	return f
}

// start runs an Informer over the fixture's namespaces until the test ends.
func (f *fixture) start() {
	f.t.Helper()
	f.index = NewIndex([]string{f.callers, f.moreCallers})
	base := rest.CopyConfig(server.Config)
	base.Wrap(f.faults.wrap)
	informer, err := New(base, f.index, f.policy)
	if err != nil {
		f.t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		informer.Run(ctx)
	}()
	f.t.Cleanup(func() {
		cancel()
		<-done
	})
}

// eventually waits for the condition; on giving up, it reports what each of lastSeen says.
func (f *fixture) eventually(what string, condition func() bool, lastSeen ...func() string) {
	f.t.Helper()
	err := wait.PollUntilContextTimeout(context.Background(), 20*time.Millisecond, time.Minute, true,
		func(context.Context) (bool, error) { return condition(), nil })
	if err != nil {
		for _, seen := range lastSeen {
			f.t.Error(seen())
		}
		f.t.Fatalf("gave up waiting for %s", what)
	}
}

func (f *fixture) available() bool { return f.index.Health.Available() }

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

// serviceAccount is the account's ServiceAccount, created with the given caller label value, or without the label
// when the value is empty.
func (f *fixture) serviceAccount(account auth.Account, label string) *corev1.ServiceAccount {
	f.t.Helper()
	created, err := server.ServiceAccount(context.Background(), account)
	if err != nil {
		f.t.Fatal(err)
	}
	if label != "" {
		created.Labels = map[string]string{CallerLabel: label}
		f.update(created)
	}
	return created
}

func (f *fixture) policySet(name string, rules ...policyapi.Rule) *policyapi.PolicySet {
	return &policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: f.policy, Name: name},
		Spec:       policyapi.PolicySetSpec{Rules: rules},
	}
}

func (f *fixture) policyBinding(namespace, name string, subject policyapi.ServiceAccountRef, sets ...string) *policyapi.PolicyBinding {
	return &policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name},
		Spec:       policyapi.PolicyBindingSpec{Subject: subject, PolicySets: sets},
	}
}

func (f *fixture) set(name string) *policyapi.PolicySet {
	f.t.Helper()
	set := &policyapi.PolicySet{}
	if err := apiClient.Get(context.Background(), client.ObjectKey{Namespace: f.policy, Name: name}, set); err != nil {
		f.t.Fatal(err)
	}
	return set
}

func (f *fixture) binding(namespace, name string) *policyapi.PolicyBinding {
	f.t.Helper()
	binding := &policyapi.PolicyBinding{}
	if err := apiClient.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: name}, binding); err != nil {
		f.t.Fatal(err)
	}
	return binding
}

// eventuallyActionsReady waits for the policy namespace's binding to carry an ActionsReady that satisfies accept, and
// returns it.
func (f *fixture) eventuallyActionsReady(name string, accept func(*metav1.Condition) bool) *metav1.Condition {
	f.t.Helper()
	var condition *metav1.Condition
	f.eventually(ActionsReady.Type+" of "+name, func() bool {
		condition = meta.FindStatusCondition(f.binding(f.policy, name).Status.Conditions, ActionsReady.Type)
		return condition != nil && accept(condition)
	})
	return condition
}

// failingPaths is a transport under the Informer's own, where a test can make the requests under a path fail, as an
// unreachable API server does.
type failingPaths struct {
	mu      sync.Mutex
	failing map[string]bool
	// open holds each watch's response body by path, so failing a path can cut the watches already open under it.
	open map[string][]io.Closer
}

func (p *failingPaths) wrap(next http.RoundTripper) http.RoundTripper {
	return roundTripFunc(func(request *http.Request) (*http.Response, error) {
		p.mu.Lock()
		failing := false
		for prefix := range p.failing {
			failing = failing || strings.HasPrefix(request.URL.Path, prefix)
		}
		p.mu.Unlock()
		if failing {
			return nil, errors.New("test fault: the API server is unreachable")
		}
		response, err := next.RoundTrip(request)
		if err == nil && request.URL.Query().Get("watch") == "true" {
			p.mu.Lock()
			p.open[request.URL.Path] = append(p.open[request.URL.Path], response.Body)
			p.mu.Unlock()
		}
		return response, err
	})
}

// fail makes every request under the path fail, and cuts the watches open under it, until the returned restore is
// called.
func (p *failingPaths) fail(prefix string) (restore func()) {
	p.mu.Lock()
	p.failing[prefix] = true
	var cut []io.Closer
	for path, bodies := range p.open {
		if strings.HasPrefix(path, prefix) {
			cut = append(cut, bodies...)
			delete(p.open, path)
		}
	}
	p.mu.Unlock()
	for _, body := range cut {
		_ = body.Close()
	}
	return func() {
		p.mu.Lock()
		delete(p.failing, prefix)
		p.mu.Unlock()
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) { return f(request) }
