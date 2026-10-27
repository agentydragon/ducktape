package mcpexec

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	_ "github.com/lib/pq"
	"gopkg.in/yaml.v3"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	// The stdio tests run this binary as their MCP server.
	if os.Getenv(stdioServerVariable) != "" {
		serveStdio()
		return
	}
	os.Exit(postgres.RunIntegrationTests(
		migrations.Up,
		func(database *postgres.Database) int {
			integrationDatabase = database
			return m.Run()
		},
	))
}

// awaitBound only fails a test whose condition never comes.
const awaitBound = 30 * time.Second

const testGroup = "test-group"

var (
	caller   = store.CallerPrincipal{Account: action.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"}}
	operator = store.OperatorPrincipal{Issuer: "https://issuer.test.example", Subject: "test-operator"}
)

// mcpGroup is a catalog of one MCP group, testGroup, whose server config is the JSON config.
func mcpGroup(t *testing.T, config string) *catalog.Catalog {
	t.Helper()
	var settings catalog.Settings
	document := `{"` + testGroup + `": {"title": "t", "description": "d",
		"executor": {"kind": "mcp", "description": "e", "config": ` + config + `}}}`
	if err := yaml.Unmarshal([]byte(document), &settings); err != nil {
		t.Fatal(err)
	}
	groups, err := catalog.New(settings)
	if err != nil {
		t.Fatal(err)
	}
	return groups
}

// httpConfig is the config of a streamable HTTP server at url that takes no credential.
func httpConfig(url string) string {
	return `{"transport": "streamable-http", "url": "` + url + `", "auth": "none"}`
}

func group(t *testing.T, groups *catalog.Catalog) *catalog.Group {
	t.Helper()
	for key, g := range groups.Groups() {
		if key == testGroup {
			return g
		}
	}
	t.Fatal("the catalog holds no test group")
	return nil
}

// awaitGroup waits until the test group satisfies condition, and returns it as it is then.
func awaitGroup(t *testing.T, groups *catalog.Catalog, condition func(*catalog.Group) bool) *catalog.Group {
	t.Helper()
	deadline := time.After(awaitBound)
	for {
		changed := groups.Changed()
		g := group(t, groups)
		if condition(g) {
			return g
		}
		select {
		case <-changed:
		case <-deadline:
			t.Fatalf("the group never reached the state awaited: available %t with %d Actions, health %v",
				g.Available, len(g.Actions), g.Health)
		}
	}
}

// offering is whether the group is available with exactly the Actions names.
func offering(names ...string) func(*catalog.Group) bool {
	return func(g *catalog.Group) bool {
		if !g.Available || len(g.Actions) != len(names) {
			return false
		}
		for _, name := range names {
			if _, offered := g.Actions[name]; !offered {
				return false
			}
		}
		return true
	}
}

// unavailableFor is whether the group is unavailable for reason, with a detail that satisfies detail.
func unavailableFor(reason action_servicepb.McpUnavailableReason, detail func(string) bool) func(*catalog.Group) bool {
	return func(g *catalog.Group) bool {
		h := g.Health
		return !g.Available && h.Reason != nil && *h.Reason == reason && h.Detail != nil && detail(*h.Detail)
	}
}

func anyDetail(string) bool { return true }

// executorTiming is the Python service's but for a shorter lifecycle bound, so that a test of a step that never
// answers ends sooner.
func executorTiming() Timing {
	timing := DefaultTiming()
	timing.Lifecycle = 5 * time.Second
	return timing
}

// supervised is an Executor whose Run a test started. The test's cleanup stops it and waits for it.
type supervised struct {
	*Executor
	// stop ends Run's context, which drains the group.
	stop context.CancelFunc
	// done is closed when Run returns.
	done chan struct{}
}

func supervise(t *testing.T, groups *catalog.Catalog, linkages Linkages, timing Timing) supervised {
	t.Helper()
	binding := group(t, groups).Executor.(catalog.MCPBinding)
	executor, err := New(groups, testGroup, binding, linkages, timing)
	if err != nil {
		t.Fatal(err)
	}
	ctx, stop := context.WithCancel(context.Background())
	s := supervised{Executor: executor, stop: stop, done: make(chan struct{})}
	go func() {
		defer close(s.done)
		executor.Run(ctx)
	}()
	t.Cleanup(func() {
		stop()
		select {
		case <-s.done:
		case <-time.After(awaitBound):
			t.Error("the executor did not stop")
		}
	})
	return s
}

// fixture is a database of the test's own and the store over it.
type fixture struct {
	store *store.Store
	db    *sql.DB
	// updates is signalled on every Event committed to the database, and dispatches on every dispatch.
	updates, dispatches <-chan struct{}
}

func newFixture(t *testing.T) fixture {
	t.Helper()
	url := integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	hub, err := wakeups.Listen(t.Context(), url, store.UpdatesChannel, store.DispatchChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	f := fixture{store: store.New(db), db: db}
	for _, channel := range []struct {
		name   string
		signal *<-chan struct{}
	}{{store.UpdatesChannel, &f.updates}, {store.DispatchChannel, &f.dispatches}} {
		signal, unsubscribe, err := hub.Subscribe(channel.name)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(unsubscribe)
		*channel.signal = signal
	}
	return f
}

// router routes the test group's Actions to its Executor as the service does: one the catalog does not offer now
// waits for it while the group is unavailable.
type router struct {
	groups   *catalog.Catalog
	executor *Executor
}

func (r router) Route(action store.ActionIdentity) (worker.Executor, bool) {
	switch _, _, err := r.groups.Resolve(action.Group, action.Name); {
	case errors.Is(err, catalog.ErrUnavailable):
		return r.executor, false
	case err != nil:
		return nil, false
	}
	return r.executor, true
}

// workerTiming is the Python service's, with a sweep and an executor heartbeat a test can wait for, and a drain
// that does not hold up a test's cleanup.
func workerTiming() worker.Timing {
	timing := worker.DefaultTiming()
	timing.Sweep = 20 * time.Millisecond
	timing.ExecutorHeartbeat = 20 * time.Millisecond
	timing.Drain = time.Second
	return timing
}

// working is a dispatch worker a test started. The test's cleanup drains it and waits for it.
type working struct {
	drain context.CancelFunc
	done  chan struct{}
}

// work runs a dispatch worker that runs the test group's Executions on executor, woken by the store's dispatches.
func (f fixture) work(t *testing.T, groups *catalog.Catalog, executor *Executor, timing worker.Timing) working {
	t.Helper()
	w, err := worker.New(f.store, router{groups: groups, executor: executor}, timing)
	if err != nil {
		t.Fatal(err)
	}
	ctx, drain := context.WithCancel(context.Background())
	r := working{drain: drain, done: make(chan struct{})}
	go func() {
		defer close(r.done)
		w.Run(ctx, f.dispatches)
	}()
	t.Cleanup(func() {
		drain()
		select {
		case <-r.done:
		case <-time.After(awaitBound):
			t.Error("the worker did not stop")
		}
	})
	return r
}

// allowed is a request for the test group's Action an operator allowed, so its Execution awaits dispatch.
func (f fixture) allowed(t *testing.T, action, arguments string) uuid.UUID {
	t.Helper()
	key := uuid.NewString()
	pending, err := f.store.Submit(t.Context(), caller, store.ActionRequestInput{
		IdempotencyKey: key, Action: store.ActionIdentity{Group: testGroup, Name: action},
		Arguments: json.RawMessage(arguments), Title: "test title",
	}, store.Admission{RequestID: uuid.New()})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := f.store.Decide(t.Context(), pending.ID, store.DecisionInput{
		Verdict: store.VerdictAllow, ExpectedVersion: pending.Version, IdempotencyKey: "allow-" + key,
	}, operator); err != nil {
		t.Fatal(err)
	}
	return pending.ID
}

func (f fixture) get(t *testing.T, id uuid.UUID) store.ActionRequest {
	t.Helper()
	request, err := f.store.Get(t.Context(), id, operator)
	if err != nil {
		t.Fatal(err)
	}
	return request
}

// awaitRequest waits until the request satisfies condition, and returns it.
func (f fixture) awaitRequest(t *testing.T, id uuid.UUID, condition func(store.ActionRequest) bool) store.ActionRequest {
	t.Helper()
	deadline := time.After(awaitBound)
	for {
		request := f.get(t, id)
		if condition(request) {
			return request
		}
		select {
		case <-f.updates:
		case <-deadline:
			t.Fatalf("request %s is %s, never reached the state awaited", id, request.State)
		}
	}
}

// await waits until the request is in state, and returns it.
func (f fixture) await(t *testing.T, id uuid.UUID, state action_servicepb.ActionState) store.ActionRequest {
	t.Helper()
	return f.awaitRequest(t, id, func(request store.ActionRequest) bool { return request.State == state })
}

// errorKind is the `kind` of an Execution's error document.
func errorKind(t *testing.T, request store.ActionRequest) string {
	t.Helper()
	var document struct {
		Kind string `json:"kind"`
	}
	if err := json.Unmarshal(request.Execution.Error, &document); err != nil {
		t.Fatalf("execution error %s: %v", request.Execution.Error, err)
	}
	return document.Kind
}

func sameJSON(t *testing.T, got, want json.RawMessage) {
	t.Helper()
	var gotValue, wantValue any
	if err := json.Unmarshal(got, &gotValue); err != nil {
		t.Fatalf("%s: %v", got, err)
	}
	if err := json.Unmarshal(want, &wantValue); err != nil {
		t.Fatalf("%s: %v", want, err)
	}
	if !reflect.DeepEqual(gotValue, wantValue) {
		t.Errorf("got %s, want %s", got, want)
	}
}

// eventually polls condition, for state that announces no change, until it holds. Its string describes the state
// when it does not.
func eventually(t *testing.T, condition func() (bool, string)) {
	t.Helper()
	deadline := time.After(awaitBound)
	for {
		held, state := condition()
		if held {
			return
		}
		select {
		case <-deadline:
			t.Fatal(state)
		case <-time.After(5 * time.Millisecond):
		}
	}
}

// gate holds a tool call until the test releases it, or until the call's context ends. A test opens its gates
// before it returns, with defer: the upstream cannot close while a tool call is held, and the test's cleanups close
// it.
type gate struct {
	started chan struct{}
	release chan struct{}
	once    sync.Once
}

func newGate() *gate {
	return &gate{started: make(chan struct{}, 16), release: make(chan struct{})}
}

func (g *gate) open() { g.once.Do(func() { close(g.release) }) }

// hold waits for the release, and reports whether it came before ctx ended.
func (g *gate) hold(ctx context.Context) bool {
	g.started <- struct{}{}
	select {
	case <-g.release:
		return true
	case <-ctx.Done():
		return false
	}
}

func (g *gate) awaitStarted(t *testing.T) {
	t.Helper()
	select {
	case <-g.started:
	case <-time.After(awaitBound):
		t.Fatal("nothing reached the gate")
	}
}
