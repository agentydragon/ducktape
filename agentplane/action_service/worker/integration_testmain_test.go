package worker

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
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

var (
	caller         = store.CallerPrincipal{Account: action.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"}}
	operator       = store.OperatorPrincipal{Issuer: "https://issuer.test.example", Subject: "test-operator"}
	actionIdentity = store.ActionIdentity{Group: "test-group", Name: "test-action"}
)

// fixture is a database of the test's own and the store over it.
type fixture struct {
	store *store.Store
	db    *sql.DB
	url   string
	// updates is signalled on every Event committed to the database.
	updates <-chan struct{}
}

func newFixture(t *testing.T) fixture {
	t.Helper()
	url := integrationDatabase.Clone(t)
	db := open(t, url)
	return fixture{store: store.New(db), db: db, url: url, updates: listen(t, url, store.UpdatesChannel)}
}

func open(t *testing.T, url string) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

// listen returns a signal of the channel's notifications.
func listen(t *testing.T, url, channel string) <-chan struct{} {
	t.Helper()
	hub, err := wakeups.Listen(t.Context(), url, channel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	signal, unsubscribe, err := hub.Subscribe(channel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(unsubscribe)
	return signal
}

// dispatches is how a deployment wakes the worker: the store's dispatch notifications.
func (f fixture) dispatches(t *testing.T) <-chan struct{} {
	t.Helper()
	return listen(t, f.url, store.DispatchChannel)
}

// allowed is a request an operator allowed, so its Execution awaits dispatch.
func (f fixture) allowed(t *testing.T, key string) uuid.UUID {
	t.Helper()
	pending, err := f.store.Submit(t.Context(), caller, store.ActionRequestInput{
		IdempotencyKey: key, Action: actionIdentity, Arguments: json.RawMessage(`{"key": "` + key + `"}`), Title: "test title for " + key,
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

// await waits until the request is in the state, and returns it.
func (f fixture) await(t *testing.T, id uuid.UUID, state action.State) store.ActionRequest {
	t.Helper()
	deadline := time.After(awaitBound)
	for {
		request := f.get(t, id)
		if request.State == state {
			return request
		}
		select {
		case <-f.updates:
		case <-deadline:
			t.Fatalf("request %s is %s, never %s", id, request.State, state)
		}
	}
}

func (f fixture) pending(t *testing.T) []uuid.UUID {
	t.Helper()
	pending, err := f.store.PendingDispatches(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	return pending
}

func (f fixture) count(t *testing.T, query string, args ...any) int {
	t.Helper()
	var count int
	if err := f.db.QueryRowContext(t.Context(), query, args...).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

// lock takes the row lock the query names in a transaction of its own, so what wants it next queues behind it, and
// returns the commit that lets it through.
func (f fixture) lock(t *testing.T, query string, args ...any) (release func()) {
	t.Helper()
	tx, err := f.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { tx.Rollback() })
	if _, err := tx.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			if err := tx.Commit(); err != nil {
				t.Error(err)
			}
		})
	}
}

// The row locks the store takes first when it claims, renews or finishes an Execution.
const (
	lockRequest   = `SELECT 1 FROM action_request WHERE id = $1 FOR UPDATE`
	lockExecution = `SELECT 1 FROM action_execution WHERE request_id = $1 FOR UPDATE`
)

// lockWaiters counts the backends that wait for a lock another holds.
const lockWaiters = `SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'`

// awaitCount waits until the query counts n.
func (f fixture) awaitCount(t *testing.T, n int, query string, args ...any) {
	t.Helper()
	eventually(t, func() (bool, string) {
		count := f.count(t, query, args...)
		return count == n, fmt.Sprintf("%q counts %d, want %d", query, count, n)
	})
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

// claimElsewhere claims the request's Execution for another executor, and starts it.
func (f fixture) claimElsewhere(t *testing.T, id uuid.UUID, executorID string, lease time.Duration) store.ExecutionClaim {
	t.Helper()
	claim, err := f.store.ClaimExecution(t.Context(), id, executorID, lease, func(store.ActionIdentity) bool { return true })
	if err != nil || claim == nil {
		t.Fatalf("claim %v, %v", claim, err)
	}
	if _, err := f.store.MarkRunning(t.Context(), id); err != nil {
		t.Fatal(err)
	}
	return *claim
}

// testTiming is the Python service's, with a sweep and an executor heartbeat a test can wait for.
func testTiming() Timing {
	timing := DefaultTiming()
	timing.Sweep = 20 * time.Millisecond
	timing.ExecutorHeartbeat = 20 * time.Millisecond
	return timing
}

// running is a worker a test started. The test's cleanup drains it and waits for it.
type running struct {
	worker *Worker
	// drain starts the drain, as the end of Run's context does.
	drain context.CancelFunc
	// done is closed when Run returns.
	done chan struct{}
}

func run(t *testing.T, s *store.Store, executors Executors, timing Timing, wakeups <-chan struct{}) running {
	t.Helper()
	w, err := New(s, executors, timing)
	if err != nil {
		t.Fatal(err)
	}
	ctx, drain := context.WithCancel(context.Background())
	r := running{worker: w, drain: drain, done: make(chan struct{})}
	go func() {
		defer close(r.done)
		w.Run(ctx, wakeups)
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

// wait waits until Run returns.
func (r running) wait(t *testing.T) {
	t.Helper()
	select {
	case <-r.done:
	case <-time.After(awaitBound):
		t.Fatal("the worker did not stop")
	}
}

// awaitIdle waits until the worker has no dispatch under way.
func (r running) awaitIdle(t *testing.T) {
	t.Helper()
	eventually(t, func() (bool, string) {
		r.worker.mu.Lock()
		defer r.worker.mu.Unlock()
		return len(r.worker.scheduled) == 0, fmt.Sprintf("%d dispatches still under way", len(r.worker.scheduled))
	})
}

// executor is an Executor and the Executors that route every Action to it. It records each request it runs, and
// runs it with its step.
type executor struct {
	step func(ctx context.Context, request store.ExecutionRequest, lease *Lease) (store.ExecutionOutcome, error)
	// unready keeps Executions awaiting dispatch.
	unready atomic.Bool
	// routed counts the Route calls.
	routed atomic.Int64

	mu   sync.Mutex
	runs []store.ExecutionRequest
}

func (e *executor) Route(store.ActionIdentity) (Executor, bool) {
	e.routed.Add(1)
	return e, !e.unready.Load()
}

func (e *executor) Execute(ctx context.Context, request store.ExecutionRequest, lease *Lease) (store.ExecutionOutcome, error) {
	e.mu.Lock()
	e.runs = append(e.runs, request)
	e.mu.Unlock()
	return e.step(ctx, request, lease)
}

// ran returns the requests the executor ran, by ID.
func (e *executor) ran() []uuid.UUID {
	e.mu.Lock()
	defer e.mu.Unlock()
	ids := make([]uuid.UUID, len(e.runs))
	for i, request := range e.runs {
		ids[i] = request.RequestID
	}
	return ids
}

var echoed = json.RawMessage(`{"echo": true}`)

func succeed(context.Context, store.ExecutionRequest, *Lease) (store.ExecutionOutcome, error) {
	return store.ExecutionOutcome{State: store.ExecutionSucceeded, Result: echoed}, nil
}

// gate holds an Execution until the test releases it, or until its context ends, which it returns.
type gate struct {
	started chan uuid.UUID
	release chan struct{}
}

func newGate() *gate { return &gate{started: make(chan uuid.UUID, 16), release: make(chan struct{})} }

func (g *gate) step(ctx context.Context, request store.ExecutionRequest, _ *Lease) (store.ExecutionOutcome, error) {
	g.started <- request.RequestID
	select {
	case <-g.release:
		return succeed(ctx, request, nil)
	case <-ctx.Done():
		return store.ExecutionOutcome{}, ctx.Err()
	}
}

func (g *gate) awaitStarted(t *testing.T) uuid.UUID {
	t.Helper()
	select {
	case id := <-g.started:
		return id
	case <-time.After(awaitBound):
	}
	t.Fatal("no Execution started")
	return uuid.Nil
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
