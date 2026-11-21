package store

import (
	"context"
	"database/sql"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/wake"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

const sandboxUID = "00000000-0000-4000-8000-000000000001"

// caller supplies the legacy account provenance and namespace used by retained-row fixtures.
var caller = auth.Account{Namespace: "test-callers", Name: "test-app"}

func testStore(t *testing.T, cap int) *Store {
	t.Helper()
	store, _ := testStoreWithURL(t, cap)
	return store
}

func testStoreWithURL(t *testing.T, cap int) (*Store, string) {
	t.Helper()
	databaseURL := integrationDatabase.Clone(t)
	db := postgres.Open(t, databaseURL)
	store := NewStore(db, cap)
	testStoreDatabaseURLs.Store(db, databaseURL)
	t.Cleanup(func() { testStoreDatabaseURLs.Delete(db) })
	return store, databaseURL
}

// connectStore opens a Store with queue capacity 3, the main store's in every test that calls it, on
// its own single connection to databaseURL, and returns it with that connection's backend PID.
func connectStore(t *testing.T, ctx context.Context, databaseURL string) (*Store, int) {
	t.Helper()
	db := postgres.Open(t, databaseURL)
	db.SetMaxOpenConns(1)
	return NewStore(db, 3), postgres.BackendPID(t, ctx, db)
}

var testStoreDatabaseURLs sync.Map // map[*sql.DB]string

func testWatchHub(t *testing.T, store *Store) *wakeups.Hub {
	t.Helper()
	value, ok := testStoreDatabaseURLs.Load(store.DB)
	databaseURL, isString := value.(string)
	if !ok || !isString {
		t.Fatal("test store has no registered PostgreSQL URL")
	}
	hub, err := wakeups.Listen(t.Context(), databaseURL, NotifyChannel)
	if err != nil {
		t.Fatal("listen for dispatcher notifications:", err)
	}
	t.Cleanup(func() {
		if err := hub.Close(); err != nil {
			t.Errorf("close dispatcher notification listener: %v", err)
		}
	})
	return hub
}

func claimSessionOwner(t *testing.T, store *Store, sessionID string) SessionOwner {
	t.Helper()
	owner, err := store.AcquireSessionLease(t.Context(), sessionID)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := store.ReleaseSessionLease(context.Background(), *owner); err != nil {
			t.Error(err)
		}
	})
	return *owner
}

func input(id, value string) *commandpb.Command {
	return &commandpb.Command{CommandId: id, Operation: &commandpb.Command_SubmitInput{
		SubmitInput: &commandpb.SubmitInput{Text: value},
	}}
}

// enqueueWithSession seeds an already materialized Session for tests of command delivery.
func enqueueWithSession(t *testing.T, store *Store, ctx context.Context, sessionID, sandboxName, sandboxUID string, command *commandpb.Command, wakeCaller *auth.Account) (*Dispatch, error) {
	t.Helper()
	if _, err := store.GetSession(ctx, sessionID); errors.Is(err, ErrNotFound) {
		if _, err := createSession(store, ctx, sessionID, sandboxName, sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
			return nil, err
		}
		if _, err := store.DB.ExecContext(ctx, "UPDATE session SET state=$2 WHERE session_id=$1", sessionID, SessionAcceptedState); err != nil {
			return nil, err
		}
	} else if err != nil {
		return nil, err
	}
	return store.EnqueueForCaller(ctx, sessionID, sandboxName, sandboxUID, command, wakeCaller, testStoreCaller(wakeCaller))
}

func wakeBudgetCaller(namespace, name, uid string) *identitypb.ServiceAccountIdentity {
	return serviceAccountIdentityFor(auth.Account{Namespace: namespace, Name: name}, uid)
}

// wakeKey is the budget key the lifecycle manager derives for a ServiceAccount caller.
func wakeKey(caller *identitypb.ServiceAccountIdentity) wake.WakeBudgetKey {
	return wake.WakeBudgetKey{Namespace: caller.GetAccount().GetNamespace(), Name: caller.GetAccount().GetName(), UID: caller.GetUid()}
}

func reserveWakeAttemptAsPreviousWriter(
	ctx context.Context,
	store *Store,
	caller *identitypb.ServiceAccountIdentity,
	sandboxUID string,
	generation int64,
) (time.Duration, error) {
	var retryAfter time.Duration
	err := transaction(ctx, store.DB, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,0))`,
			"dispatcher-wake:"+caller.GetAccount().GetNamespace()+":"+caller.GetAccount().GetName())
		if err != nil {
			return err
		}
		var now time.Time
		if err := tx.QueryRowContext(ctx, "SELECT clock_timestamp()").Scan(&now); err != nil {
			return err
		}
		windowStart := now.Add(-WakeWindow)
		if _, err := tx.ExecContext(ctx, "DELETE FROM wake_attempt WHERE attempted_at <= $1", windowStart); err != nil {
			return err
		}
		var exists bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM wake_attempt
			WHERE caller_namespace=$1 AND caller_name=$2 AND sandbox_uid=$3 AND wake_generation=$4)`,
			caller.GetAccount().GetNamespace(), caller.GetAccount().GetName(), sandboxUID, generation).Scan(&exists); err != nil {
			return err
		}
		if exists {
			return nil
		}
		var count int
		var oldest sql.NullTime
		if err := tx.QueryRowContext(ctx, `SELECT count(*),min(attempted_at) FROM wake_attempt
			WHERE caller_namespace=$1 AND caller_name=$2 AND attempted_at>$3`,
			caller.GetAccount().GetNamespace(), caller.GetAccount().GetName(), windowStart).Scan(&count, &oldest); err != nil {
			return err
		}
		if count >= WakeLimit {
			if !oldest.Valid {
				return errors.New("wake budget full without attempt")
			}
			retryAfter = oldest.Time.Add(WakeWindow).Sub(now)
			if retryAfter < time.Millisecond {
				retryAfter = time.Millisecond
			}
			return nil
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO wake_attempt
			(caller_namespace,caller_name,sandbox_uid,wake_generation,attempted_at)
			VALUES ($1,$2,$3,$4,$5)`, caller.GetAccount().GetNamespace(), caller.GetAccount().GetName(), sandboxUID, generation, now)
		return err
	})
	return retryAfter, err
}

func assertWakeAttemptCount(t *testing.T, store *Store, caller *identitypb.ServiceAccountIdentity, sandboxUID string, want int) {
	t.Helper()
	var count int
	if err := store.DB.QueryRow(`SELECT count(*) FROM wake_attempt
		WHERE caller_namespace=$1 AND caller_name=$2 AND sandbox_uid=$3`,
		caller.GetAccount().GetNamespace(), caller.GetAccount().GetName(), sandboxUID).Scan(&count); err != nil {
		t.Fatal("count wake attempts:", err)
	}
	if count != want {
		t.Fatalf("wake attempt count for %s/%s sandbox %s = %d, want %d", caller.GetAccount().GetNamespace(), caller.GetAccount().GetName(), sandboxUID, count, want)
	}
}

func storeDispatchFilter(session string, ids ...string) *dispatcherpb.DispatchFilter {
	return &dispatcherpb.DispatchFilter{Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: session}, CommandIds: ids}
}
