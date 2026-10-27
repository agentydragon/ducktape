package store

import (
	"database/sql"
	"encoding/json"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/migrations"
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

// fixture is a Store over a database of its own.
type fixture struct {
	*Store
	db  *sql.DB
	url string
}

func newFixture(t *testing.T) fixture {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return fixture{Store: New(db), db: db, url: dsn}
}

var (
	caller      = CallerPrincipal{Account: action.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"}}
	otherCaller = CallerPrincipal{Account: action.ServiceAccount{Namespace: "test-namespace", Name: "test-other-caller"}}
	operator    = OperatorPrincipal{Issuer: "https://issuer.test.example", Subject: "test-operator"}
	reader      = ServiceReaderPrincipal{Account: action.ServiceAccount{Namespace: "test-namespace", Name: "test-reader"}}
)

// input is a submission named by its key. Copies change what they set.
func input(key string) ActionRequestInput {
	return ActionRequestInput{
		IdempotencyKey: key,
		Action:         ActionIdentity{Group: "test-group", Name: "test-action"},
		Arguments:      json.RawMessage(`{}`),
		Title:          "test title for " + key,
	}
}

func (f fixture) submit(t *testing.T, owner CallerPrincipal, key string) ActionRequest {
	t.Helper()
	return f.submitInput(t, owner, input(key), Admission{RequestID: uuid.New()})
}

func (f fixture) submitInput(t *testing.T, owner CallerPrincipal, body ActionRequestInput, admission Admission) ActionRequest {
	t.Helper()
	request, err := f.Submit(t.Context(), owner, body, admission)
	if err != nil {
		t.Fatal(err)
	}
	return request
}

// allowed is a request an operator allowed, so its Execution awaits dispatch.
func (f fixture) allowed(t *testing.T, owner CallerPrincipal, key string) ActionRequest {
	t.Helper()
	return f.decide(t, f.submit(t, owner, key), VerdictAllow)
}

func (f fixture) decide(t *testing.T, request ActionRequest, verdict Verdict) ActionRequest {
	t.Helper()
	decided, _, err := f.Decide(t.Context(), request.ID, DecisionInput{
		Verdict: verdict, ExpectedVersion: request.Version, IdempotencyKey: "decision-" + request.IdempotencyKey,
	}, operator)
	if err != nil {
		t.Fatal(err)
	}
	return decided
}

func (f fixture) get(t *testing.T, id uuid.UUID) ActionRequest {
	t.Helper()
	request, err := f.Get(t.Context(), id, operator)
	if err != nil {
		t.Fatal(err)
	}
	return request
}

func (f fixture) events(t *testing.T, id uuid.UUID) []Event {
	t.Helper()
	events, err := f.Events(t.Context(), id, operator, EventsFilter{})
	if err != nil {
		t.Fatal(err)
	}
	return events
}

func eventStates(events []Event) []action.State {
	states := make([]action.State, len(events))
	for i, event := range events {
		states[i] = event.State
	}
	return states
}

func (f fixture) exec(t *testing.T, query string, args ...any) {
	t.Helper()
	if _, err := f.db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
}

func (f fixture) count(t *testing.T, query string, args ...any) int {
	t.Helper()
	var count int
	if err := f.db.QueryRowContext(t.Context(), query, args...).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

// awaitBound only fails a test whose condition never comes.
const awaitBound = 30 * time.Second

// holdRequest locks the request's row in a transaction of its own, so what the test starts next queues behind it,
// and returns the commit that lets it through.
func (f fixture) holdRequest(t *testing.T, id uuid.UUID) (release func()) {
	t.Helper()
	tx, err := f.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { tx.Rollback() })
	if _, err := tx.ExecContext(t.Context(), `SELECT 1 FROM action_request WHERE id = $1 FOR UPDATE`, id); err != nil {
		t.Fatal(err)
	}
	return func() {
		if err := tx.Commit(); err != nil {
			t.Error(err)
		}
	}
}

// awaitBlocked waits until n backends wait for a lock another holds.
func (f fixture) awaitBlocked(t *testing.T, n int) {
	t.Helper()
	deadline := time.After(awaitBound)
	for {
		blocked := f.count(t, `SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'`)
		if blocked >= n {
			return
		}
		select {
		case <-deadline:
			t.Fatalf("%d backends wait for a lock, want %d", blocked, n)
		case <-time.After(5 * time.Millisecond):
		}
	}
}

// async runs f in a goroutine and returns what it returns.
func async[T any](f func() T) <-chan T {
	result := make(chan T, 1)
	go func() { result <- f() }()
	return result
}

func await[T any](t *testing.T, result <-chan T) T {
	t.Helper()
	select {
	case value := <-result:
		return value
	case <-time.After(awaitBound):
		t.Fatal("the call never returned")
		panic("unreachable")
	}
}

type decided struct {
	request  ActionRequest
	dispatch bool
	err      error
}
