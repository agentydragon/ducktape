package store

import (
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

// observeWakeups returns a check of whether a step wakes whoever follows the request Events and whoever
// dispatches Executions.
func observeWakeups(t *testing.T, f fixture) func(name string, wantUpdate, wantDispatch bool, step func()) {
	t.Helper()
	const sentinel = "action_store_test_sentinel"
	hub, err := wakeups.Listen(t.Context(), f.url, UpdatesChannel, DispatchChannel, sentinel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	subscribe := func(channel string) <-chan struct{} {
		signal, stop, err := hub.Subscribe(channel)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(stop)
		return signal
	}
	updates, dispatches, settled := subscribe(UpdatesChannel), subscribe(DispatchChannel), subscribe(sentinel)

	pending := func(signal <-chan struct{}) bool {
		select {
		case <-signal:
			return true
		default:
			return false
		}
	}
	return func(name string, wantUpdate, wantDispatch bool, step func()) {
		t.Helper()
		step()
		// The hub applies notifications in the order PostgreSQL delivers them, so once the sentinel's has
		// arrived, every notification the step committed before it has too.
		f.exec(t, "SELECT pg_notify($1, '')", sentinel)
		select {
		case <-settled:
		case <-time.After(awaitBound):
			t.Fatal("no wakeup from the sentinel")
		}
		if got := pending(updates); got != wantUpdate {
			t.Errorf("%s woke followers = %v, want %v", name, got, wantUpdate)
		}
		if got := pending(dispatches); got != wantDispatch {
			t.Errorf("%s woke dispatchers = %v, want %v", name, got, wantDispatch)
		}
	}
}

func TestWritesThatChangeWhatReadersFollowWakeThemAndOthersDoNot(t *testing.T) {
	f := newFixture(t)
	check := observeWakeups(t, f)

	var pending ActionRequest
	check("submitting", true, false, func() { pending = f.submit(t, caller, "woken") })
	check("resubmitting the key", false, false, func() {
		if _, err := f.Submit(t.Context(), caller, input("woken"), Admission{RequestID: uuid.New()}); conflictReason(err) != ConflictIdempotencyKeyUsed {
			t.Error(err)
		}
	})
	check("an invalid submission", false, false, func() {
		invalid := input("invalid")
		invalid.Title = ""
		if _, err := f.Submit(t.Context(), caller, invalid, Admission{RequestID: uuid.New()}); !errors.Is(err, ErrInvalid) {
			t.Error(err)
		}
	})
	check("reading", false, false, func() {
		f.get(t, pending.ID)
		f.events(t, pending.ID)
		f.list(t, operator, ListFilter{})
		f.history(t, HistoryPage{Limit: 10})
	})
	check("refusing a stale decision", false, false, func() {
		stale := decision(pending, VerdictAllow, "stale")
		stale.ExpectedVersion++
		if _, _, err := f.Decide(t.Context(), pending.ID, stale, operator); conflictReason(err) != ConflictVersionChanged {
			t.Error(err)
		}
	})
	var allowed ActionRequest
	check("allowing", true, true, func() { allowed = f.decide(t, pending, VerdictAllow) })
	check("replaying the decision", false, false, func() {
		if _, dispatch, err := f.Decide(t.Context(), pending.ID, decision(pending, VerdictAllow, "decision-woken"), operator); err != nil || dispatch {
			t.Errorf("replay dispatch %v, %v", dispatch, err)
		}
	})
	check("cancelling an allowed request", true, false, func() { f.cancel(t, allowed.ID) })
	check("cancelling a cancelled request", false, false, func() { f.cancel(t, allowed.ID) })
	check("denying", true, false, func() { f.decide(t, f.submit(t, caller, "denied"), VerdictDeny) })
	check("a submission a provider allowed", true, true, func() {
		f.submitInput(t, caller, input("auto-allowed"), Admission{RequestID: uuid.New(), Vote: vote(VerdictAllow)})
	})
	check("a submission a provider denied", true, false, func() {
		f.submitInput(t, caller, input("auto-denied"), Admission{RequestID: uuid.New(), Vote: vote(VerdictDeny)})
	})
}

func TestExecutionWritesWakeFollowersAndNeverDispatchers(t *testing.T) {
	f := newFixture(t)
	check := observeWakeups(t, f)
	refuse := func(ActionIdentity) bool { return false }

	var allowed ActionRequest
	var claim ExecutionClaim
	check("allowing", true, true, func() { allowed = f.allowed(t, caller, "executed") })
	check("a claim the dispatch filter refuses", false, false, func() {
		if claim, err := f.ClaimExecution(t.Context(), allowed.ID, testExecutor, leaseWindow, refuse); claim != nil || err != nil {
			t.Errorf("claim %+v, %v", claim, err)
		}
	})
	check("claiming", true, false, func() { claim = f.claim(t, allowed.ID) })
	check("claiming again", false, false, func() {
		if claim, err := f.ClaimExecution(t.Context(), allowed.ID, testExecutor, leaseWindow, anywhere); claim != nil || err != nil {
			t.Errorf("claim %+v, %v", claim, err)
		}
	})
	check("renewing a lease", false, false, func() { f.heartbeat(t, allowed.ID, claim.Lease) })
	check("an executor heartbeat", false, false, func() {
		if err := f.RecordExecutorHeartbeat(t.Context(), testExecutor); err != nil {
			t.Error(err)
		}
	})
	check("starting", true, false, func() { f.markRunning(t, allowed.ID) })
	check("sweeping a live lease", false, false, func() { f.expire(t) })
	check("finishing", true, false, func() { f.finish(t, allowed.ID, claim.Lease, ExecutionSucceeded) })

	var lapsed ActionRequest
	check("allowing another", true, true, func() { lapsed = f.allowed(t, caller, "lapsed") })
	check("claiming for a lapsed lease", true, false, func() { f.claimFor(t, lapsed.ID, -time.Second) })
	check("sweeping a lapsed lease", true, false, func() { f.expire(t) })
}

func TestAnUpdateNamesItsRequestAndADispatchNamesNothing(t *testing.T) {
	f := newFixture(t)
	listener := pq.NewListener(f.url, time.Second, time.Minute, nil)
	t.Cleanup(func() { listener.Close() })
	for _, channel := range []string{UpdatesChannel, DispatchChannel} {
		if err := listener.Listen(channel); err != nil {
			t.Fatal(err)
		}
	}

	allowed := f.allowed(t, caller, "named")

	// The submission commits before the decision, and a transaction notifies in the order it wrote.
	for _, want := range []pq.Notification{
		{Channel: UpdatesChannel, Extra: allowed.ID.String()},
		{Channel: UpdatesChannel, Extra: allowed.ID.String()},
		{Channel: DispatchChannel, Extra: ""},
	} {
		select {
		case got := <-listener.Notify:
			if got == nil || got.Channel != want.Channel || got.Extra != want.Extra {
				t.Fatalf("notification %+v, want %+v", got, want)
			}
		case <-time.After(awaitBound):
			t.Fatalf("no notification, want %+v", want)
		}
	}
}
