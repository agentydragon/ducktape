package dispatcher

import (
	"context"
	"fmt"
	"testing"
	"time"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func TestFailedClaimAloneSchedulesDelivery(t *testing.T) {
	for _, opening := range []bool{true, false} {
		t.Run(fmt.Sprint(opening), func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			store := testStore(t, 3)
			const id = "test-claim-retry-session"
			sandbox := SandboxSnapshot{Name: "test-claim-retry-sandbox", Namespace: "test-namespace", UID: sandboxUID}
			var command *Dispatch
			var err error
			if opening {
				_, err = store.CreateSession(ctx, id, sandbox.Name, sandbox.UID, &sessionpb.SessionSpec{}, nil, nil)
			} else {
				command, err = enqueueWithSession(t, store, ctx, id, sandbox.Name, sandbox.UID, input("test-claim-retry-command", "test input"), nil)
			}
			if err != nil {
				t.Fatal(err)
			}
			session, err := store.GetSession(ctx, id)
			if err != nil {
				t.Fatal(err)
			}
			owner, err := store.AcquireSessionLease(ctx, id)
			if err != nil {
				t.Fatal(err)
			}
			table, state := "dispatch", DeliveringState
			if opening {
				table, state = "session", SessionInProgressState
			}
			if _, err := store.DB.ExecContext(ctx, `CREATE SEQUENCE test_claim_attempt;
   CREATE FUNCTION test_claim_once() RETURNS trigger LANGUAGE plpgsql AS $$
   BEGIN IF nextval('test_claim_attempt')=1 THEN RAISE EXCEPTION 'test transient claim error' USING ERRCODE='40001'; END IF; RETURN NEW; END $$;
   CREATE TRIGGER test_claim_once BEFORE UPDATE ON `+table+` FOR EACH ROW
   WHEN (OLD.state IS DISTINCT FROM NEW.state AND NEW.state='`+state+`') EXECUTE FUNCTION test_claim_once()`); err != nil {
				t.Fatal(err)
			}
			runtime := &acceptingRuntime{calls: make(chan string, 1)}
			worker := &DeliveryWorker{Store: store, Catalog: testCatalog{sandbox: sandbox}, Delivery: runtime, Wake: testWake{}}
			worker.init()
			defer worker.queue.ShutDown()
			// No listener, resync or explicit enqueue runs in this test. Only the failed
			// claim can schedule the work that runQueue must eventually deliver.
			var progressed bool
			if opening {
				progressed = worker.deliverSession(ctx, session, sandbox, *owner)
			} else {
				progressed = worker.deliverCommand(ctx, command, sandbox, *owner, session)
			}
			if progressed {
				t.Fatal("injected claim error advanced work")
			}
			if err := store.ReleaseSessionLease(ctx, *owner); err != nil {
				t.Fatal(err)
			}
			done := make(chan struct{})
			go func() { defer close(done); worker.runQueue(ctx) }()
			defer func() { cancel(); worker.queue.ShutDown(); <-done }()
			expected := "opening:" + id
			if !opening {
				expected = command.CommandID
			}
			select {
			case got := <-runtime.calls:
				if got != expected {
					t.Fatalf("delivered %s, want %s", got, expected)
				}
			case <-ctx.Done():
				t.Fatal(ctx.Err())
			}
		})
	}
}
