package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestAdmissionIsDurableAndIdempotent(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	first, err := enqueueWithSession(t, store, ctx, "session-a", "sandbox-a", sandboxUID, input("a", "hello"), &auth.Account{"agentplane", "app"})
	if err != nil {
		t.Fatal(err)
	}
	if first.State != QueueState || !first.WakeEligible || first.WakeCaller == nil {
		t.Fatalf("unexpected accepted row: %+v", first)
	}
	// A different store instance models a retry after a lost reply/restart.
	restarted := NewStore(store.DB, 3)
	again, err := enqueueWithSession(t, restarted, ctx, "session-a", "sandbox-a", sandboxUID, input("a", "hello"), nil)
	if err != nil || again.Position != first.Position {
		t.Fatalf("retry: row=%+v err=%v", again, err)
	}
	if _, err := enqueueWithSession(t, restarted, ctx, "session-a", "sandbox-a", sandboxUID, input("a", "other"), nil); !errors.Is(err, ErrCommandConflict) {
		t.Fatalf("payload conflict: %v", err)
	}
	for i := 1; i < 3; i++ {
		if _, err := enqueueWithSession(t, store, ctx, "session-a", "sandbox-a", sandboxUID, input(fmt.Sprintf("input-%d", i), "x"), nil); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := enqueueWithSession(t, store, ctx, "session-a", "sandbox-a", sandboxUID, input("overflow", "x"), nil); !errors.Is(err, ErrQueueFull) {
		t.Fatalf("cap: %v", err)
	}
	rows, err := store.ListDispatchesForCaller(ctx, storeDispatchFilter("session-a"), ServiceAccountCaller{Privileged: true})
	if err != nil || len(rows) != 3 {
		t.Fatalf("list: %v, %v", rows, err)
	}
	for _, row := range rows {
		if row.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER {
			t.Fatalf("accepted command was unexpectedly cancelled: %+v", row)
		}
	}
}

func TestExpiredWakeFailureStoresTypedAssessmentWithTerminalDelivery(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	uid := uuid.NewString()
	dispatch, err := enqueueWithSession(t, store, ctx, "wake-deadline-session", "sandbox-wake-deadline", uid,
		input("wake-deadline-command", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	deliveryOwner := claimSessionOwner(t, store, dispatch.SessionID)
	requestedAt := time.Now().Add(-WakeDeadline - time.Second)
	if _, err := store.NoteWakeRequestedAt(ctx, dispatch.CommandID, deliveryOwner, false, requestedAt); err != nil {
		t.Fatal(err)
	}
	assessment := &environmentpb.WakeAssessment{
		State:      environmentpb.WakeState_WAKE_STATE_FAILED,
		Reason:     environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE,
		ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(5),
		Error: status.New(codes.DeadlineExceeded, "Sandbox did not become Ready before the wake deadline").Proto(),
	}
	failed, _, remaining, err := store.FailExpiredWake(ctx, dispatch.CommandID, deliveryOwner, false,
		"wake_deadline_expired", nil, assessment)
	if err != nil || remaining != 0 || failed == nil {
		t.Fatalf("expired wake: row=%+v remaining=%v error=%v", failed, remaining, err)
	}
	stored, err := store.GetDispatch(ctx, dispatch.CommandID)
	if err != nil {
		t.Fatal(err)
	}
	if stored.State != FailedState || !proto.Equal(stored.WakeAssessment, assessment) {
		t.Fatalf("terminal dispatch lost typed wake failure: state=%s assessment=%v", stored.State, stored.WakeAssessment)
	}
}

func TestSessionOpeningRetryPreservesBinding(t *testing.T) {
	ctx := t.Context()
	store := testStore(t, 3)
	spec := &sessionpb.SessionSpec{}
	opened, err := createSession(store, ctx, "session-b", "sandbox-b", sandboxUID, spec, nil, nil)
	if err != nil || opened.State != SessionWaitingState {
		t.Fatalf("open: %+v %v", opened, err)
	}
	again, err := createSession(store, ctx, "session-b", "sandbox-b", sandboxUID, spec, nil, nil)
	if err != nil || again.CreatedAt != opened.CreatedAt {
		t.Fatalf("opening retry: %+v %v", again, err)
	}
	otherUID := "00000000-0000-4000-8000-000000000002"
	otherSpec := proto.Clone(spec).(*sessionpb.SessionSpec)
	otherSpec.GetPlacement().GetEnvironmentHosted().GetEnvironment().Uid = otherUID
	if _, err := createSession(store, ctx, "session-b", "sandbox-b", otherUID, otherSpec, nil, nil); !errors.Is(err, ErrSessionConflict) {
		t.Fatalf("opening UID conflict: %v", err)
	}
}

func TestConcurrentSessionCapAndWakeBudget(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 2)
	var wg sync.WaitGroup
	errs := make(chan error, 3)
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := enqueueWithSession(t, store, ctx, "session-c", "sandbox-c", sandboxUID, input(fmt.Sprintf("c-%d", i), "x"), nil)
			errs <- err
		}(i)
	}
	wg.Wait()
	close(errs)
	accepted, full := 0, 0
	for err := range errs {
		switch {
		case err == nil:
			accepted++
		case errors.Is(err, ErrQueueFull):
			full++
		default:
			t.Fatal(err)
		}
	}
	if accepted != 2 || full != 1 {
		t.Fatalf("cap race: accepted=%d full=%d", accepted, full)
	}
	caller := wakeBudgetCaller("agentplane", "app", "wake-budget-app-uid")
	var wakeWG sync.WaitGroup
	wakeErrs := make(chan error, WakeLimit+1)
	wakeDurations := make(chan bool, WakeLimit+1)
	acceptedGenerations := make(chan int64, WakeLimit+1)
	for i := 0; i < WakeLimit+1; i++ {
		wakeWG.Add(1)
		go func(i int) {
			defer wakeWG.Done()
			wait, err := store.ReserveWakeAttempt(ctx, wakeKey(caller), sandboxUID, int64(i+1))
			wakeErrs <- err
			wakeDurations <- wait > 0
			if err == nil && wait == 0 {
				acceptedGenerations <- int64(i + 1)
			}
		}(i)
	}
	wakeWG.Wait()
	close(wakeErrs)
	close(wakeDurations)
	close(acceptedGenerations)
	for err := range wakeErrs {
		if err != nil {
			t.Fatal(err)
		}
	}
	limited := 0
	for yes := range wakeDurations {
		if yes {
			limited++
		}
	}
	if limited != 1 {
		t.Fatalf("wake budget race: %d limited", limited)
	}
	var acceptedGeneration int64
	for generation := range acceptedGenerations {
		acceptedGeneration = generation
		break
	}
	if wait, err := store.ReserveWakeAttempt(ctx, wakeKey(caller), sandboxUID, acceptedGeneration); err != nil || wait != 0 {
		t.Fatalf("same-generation retry: %v %v", wait, err)
	}
	callerAccount := accountOfIdentity(caller)
	replacement := wakeBudgetCaller(callerAccount.Namespace, callerAccount.Name, "replacement-wake-budget-app-uid")
	if wait, err := store.ReserveWakeAttempt(ctx, wakeKey(replacement), sandboxUID, acceptedGeneration); err != nil || wait != 0 {
		t.Fatalf("replacement ServiceAccount inherited old budget: %v %v", wait, err)
	}
}

func TestMixedVersionWakeWritersShareBudgetAndFenceReplacementUID(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	store, databaseURL := testStoreWithURL(t, 3)

	// Hold the old advisory key so this check proves new writers serialize with
	// previous replicas before reading or inserting their budget row.
	lockCaller := wakeBudgetCaller("mixed-writer", "shared-lock", "shared-lock-uid")
	lockSandboxUID := uuid.NewString()
	lockAccount := accountOfIdentity(lockCaller)
	lockKey := "dispatcher-wake:" + lockAccount.Namespace + ":" + lockAccount.Name
	lockTx, err := store.DB.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal("begin old-writer lock fixture:", err)
	}
	defer lockTx.Rollback()
	if _, err := lockTx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", lockKey); err != nil {
		t.Fatal("take old-writer advisory lock:", err)
	}
	oldWriterPID := postgres.BackendPID(t, ctx, lockTx)
	newWriter, newWriterPID := connectStore(t, ctx, databaseURL)
	newWriterDone := make(chan error, 1)
	go func() {
		_, err := newWriter.ReserveWakeAttempt(ctx, wakeKey(lockCaller), lockSandboxUID, 1)
		newWriterDone <- err
	}()
	postgres.WaitForBlocker(t, ctx, store.DB, newWriterPID, oldWriterPID)
	if err := lockTx.Commit(); err != nil {
		t.Fatal("release old-writer advisory lock:", err)
	}
	if err := <-newWriterDone; err != nil {
		t.Fatal("new writer after old lock release:", err)
	}

	// An interleaved old/new burst consumes one shared ten-wake budget even
	// though old writes use the empty UID and current writes use the live UID.
	mixedCaller := wakeBudgetCaller("mixed-writer", "shared-cap", "shared-cap-uid")
	mixedSandboxUID := uuid.NewString()
	type reservationResult struct {
		limited bool
		err     error
	}
	const reservations = WakeLimit + 4
	results := make(chan reservationResult, reservations)
	var wg sync.WaitGroup
	for i := 0; i < reservations; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			var wait time.Duration
			var err error
			if i%2 == 0 {
				wait, err = reserveWakeAttemptAsPreviousWriter(ctx, store, mixedCaller, mixedSandboxUID, int64(i+1))
			} else {
				wait, err = store.ReserveWakeAttempt(ctx, wakeKey(mixedCaller), mixedSandboxUID, int64(i+1))
			}
			results <- reservationResult{limited: wait > 0, err: err}
		}(i)
	}
	wg.Wait()
	close(results)
	accepted, limited := 0, 0
	for result := range results {
		if result.err != nil {
			t.Fatal("mixed writer reservation:", result.err)
		}
		if result.limited {
			limited++
		} else {
			accepted++
		}
	}
	if accepted != WakeLimit || limited != reservations-WakeLimit {
		t.Fatalf("mixed-writer shared cap: accepted=%d limited=%d, want accepted=%d limited=%d",
			accepted, limited, WakeLimit, reservations-WakeLimit)
	}
	assertWakeAttemptCount(t, store, mixedCaller, mixedSandboxUID, WakeLimit)

	// Old code sees every UID for the named caller, while the new caller must
	// not inherit another nonempty UID's reservations after ServiceAccount replacement.
	replaced := wakeBudgetCaller("mixed-writer", "uid-replacement", "old-uid")
	replacementSandboxUID := uuid.NewString()
	for generation := int64(1); generation <= WakeLimit; generation++ {
		if wait, err := store.ReserveWakeAttempt(ctx, wakeKey(replaced), replacementSandboxUID, generation); err != nil || wait != 0 {
			t.Fatalf("old UID reservation %d: %v %v", generation, wait, err)
		}
	}
	if wait, err := reserveWakeAttemptAsPreviousWriter(ctx, store, replaced, replacementSandboxUID, WakeLimit+1); err != nil || wait == 0 {
		t.Fatalf("previous writer did not count all nonempty UIDs: %v %v", wait, err)
	}
	replaced.Uid = "replacement-uid"
	if wait, err := store.ReserveWakeAttempt(ctx, wakeKey(replaced), replacementSandboxUID, WakeLimit+1); err != nil || wait != 0 {
		t.Fatalf("replacement UID inherited another nonempty UID's budget: %v %v", wait, err)
	}
}

func TestOnlyRunnerAdmissionRecordsAdmission(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000085"
	if _, err := enqueueWithSession(t, store, ctx, "admission-session", "sandbox-admission", uid, input("admission-input", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	deliveryOwner := claimSessionOwner(t, store, "admission-session")
	claimed, err := store.Settle(ctx, "admission-input", deliveryOwner, []string{QueueState}, DeliveringState, "", nil)
	if err != nil || claimed == nil || claimed.Admission != nil {
		t.Fatalf("claimed row carries an admission: %+v %v", claimed, err)
	}
	admission := &eventlogpb.EventEntry{Cursor: 7, Origin: &eventlogpb.EventOrigin{SourceId: "runner", Sequence: 7}}
	if _, err := store.Settle(ctx, "admission-input", deliveryOwner, []string{DeliveringState}, AdmittedState, "", admission); err != nil {
		t.Fatal(err)
	}
	if row, err := store.GetDispatch(ctx, "admission-input"); err != nil || !proto.Equal(row.Admission, admission) {
		t.Fatalf("admitted row: %+v %v", row, err)
	}
}

func TestDeletedSandboxWakesReplicasAndRetainsTerminalTombstones(t *testing.T) {
	ctx := context.Background()
	store, _ := testStoreWithURL(t, 3)
	uid := "00000000-0000-4000-8000-000000000099"
	if _, err := enqueueWithSession(t, store, ctx, "session-deleted", "sandbox-deleted", uid, input("deleted-queued", "q"), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := enqueueWithSession(t, store, ctx, "session-deleted", "sandbox-deleted", uid, input("deleted-delivering", "d"), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := enqueueWithSession(t, store, ctx, "session-deleted", "sandbox-deleted", uid, input("test-deleted-cancelled", "test-cancelled-input"), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB.ExecContext(ctx, "UPDATE dispatch SET state=$2,reason='test-prior-cancellation' WHERE command_id=$1", "test-deleted-cancelled", CancelledState); err != nil {
		t.Fatal(err)
	}
	interrupt := &commandpb.Command{
		CommandId: "deleted-admitted-interrupt",
		Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: "active-turn"}},
	}
	if _, err := enqueueWithSession(t, store, ctx, "session-deleted", "sandbox-deleted", uid, interrupt, nil); err != nil {
		t.Fatal(err)
	}
	deliveryOwner := claimSessionOwner(t, store, "session-deleted")
	if row, err := store.Settle(ctx, interrupt.GetCommandId(), deliveryOwner, []string{QueueState}, AdmittedState, "", &eventlogpb.EventEntry{Cursor: 17}); err != nil || row == nil {
		t.Fatalf("admit interrupt: %+v %v", row, err)
	}
	if row, err := store.Settle(ctx, "deleted-delivering", deliveryOwner, []string{QueueState}, DeliveringState, "", nil); err != nil || row == nil {
		t.Fatalf("settle: %+v %v", row, err)
	}
	if marked, err := store.MarkDeliveryAttempt(ctx, "deleted-delivering", deliveryOwner); err != nil || !marked {
		t.Fatalf("mark in-flight delivery: %t %v", marked, err)
	}
	// Both Dispatcher replicas must wake for the committed cleanup, then read the
	// durable tombstones because the dispatch rows are deleted in the same transaction.
	subscribers := make([]<-chan struct{}, 0, 2)
	for range 2 {
		hub := testWatchHub(t, store)
		sub, unsubscribe, err := hub.Subscribe(NotifyChannel)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(unsubscribe)
		subscribers = append(subscribers, sub)
	}
	if err := store.EndSandbox(ctx, uid); err != nil {
		t.Fatal(err)
	}
	for replica, sub := range subscribers {
		select {
		case <-sub:
		case <-time.After(5 * time.Second):
			t.Fatalf("replica %d missed Sandbox deletion wakeup", replica)
		}
	}
	if _, err := store.GetDispatch(ctx, "deleted-queued"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleted row visible: %v", err)
	}
	for commandID, want := range map[string]struct{ state, reason string }{
		"deleted-queued":         {FailedState, "sandbox_deleted"},
		"deleted-delivering":     {UnknownState, "sandbox_deleted"},
		"test-deleted-cancelled": {CancelledState, "test-prior-cancellation"},
		interrupt.GetCommandId(): {AdmittedState, ""},
	} {
		var state string
		var reason sql.NullString
		if err := store.DB.QueryRowContext(ctx, `SELECT state,reason FROM dispatch_tombstone WHERE command_id=$1`, commandID).Scan(&state, &reason); err != nil {
			t.Fatal(err)
		}
		if state != want.state || reason.String != want.reason {
			t.Fatalf("retained deletion %s = %s/%s, want %s/%s", commandID, state, reason.String, want.state, want.reason)
		}
	}
}

func TestSandboxCleanupWaitsForInFlightSession(t *testing.T) {
	store, databaseURL := testStoreWithURL(t, 3)
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	uid, sessionID := uuid.NewString(), uuid.NewString()
	gate, err := store.DB.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Rollback()
	if err := sessionLock(ctx, gate, sessionID); err != nil {
		t.Fatal(err)
	}
	gatePID := postgres.BackendPID(t, ctx, gate)
	openingStore, openingPID := connectStore(t, ctx, databaseURL)
	cleanupStore, cleanupPID := connectStore(t, ctx, databaseURL)
	opened := make(chan error, 1)
	go func() {
		_, err := createSession(openingStore, ctx, sessionID, "sandbox-race", uid, &sessionpb.SessionSpec{}, nil, nil)
		opened <- err
	}()
	postgres.WaitForBlocker(t, ctx, store.DB, openingPID, gatePID)
	cleaned := make(chan error, 1)
	go func() {
		cleaned <- cleanupStore.EndSandbox(ctx, uid)
	}()
	// Cleanup must wait for the transaction whose opening has not yet been inserted.
	postgres.WaitForBlocker(t, ctx, store.DB, cleanupPID, openingPID)
	if err := gate.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := <-opened; err != nil {
		t.Fatal(err)
	}
	if err := <-cleaned; err != nil {
		t.Fatal(err)
	}
	retained, err := store.GetSession(ctx, sessionID)
	if err != nil || retained.EnvironmentDeletedAt == nil || retained.State != SessionFailedState {
		t.Fatalf("cleanup did not settle the in-flight opening: %+v %v", retained, err)
	}
}

func TestSessionRejectionSerializesWithAdmissionAndFencesSessionLeases(t *testing.T) {
	for _, kind := range []string{"native", "central"} {
		t.Run(kind, func(t *testing.T) {
			store, databaseURL := testStoreWithURL(t, 3)
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			sessionID, uid, commandID := uuid.NewString(), uuid.NewString(), uuid.NewString()
			spec := &sessionpb.SessionSpec{}
			if kind == "central" {
				spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: "test-rejection-race", Uid: uid}}}}
			}
			if _, err := createSession(store, ctx, sessionID, "test-rejection-race", uid, spec, nil, nil); err != nil {
				t.Fatal(err)
			}
			staleDelivery := claimSessionOwner(t, store, sessionID)
			expireSessionLease(t, store, staleDelivery)
			deliveryOwner := claimSessionOwner(t, store, sessionID)
			if row, err := store.RejectSession(ctx, sessionID, staleDelivery, []string{SessionWaitingState}, "stale rejection"); err != nil || row != nil {
				t.Fatalf("stale Session lease settled opening: %+v %v", row, err)
			}
			gate, err := store.DB.BeginTx(ctx, nil)
			if err != nil {
				t.Fatal(err)
			}
			defer gate.Rollback()
			if _, err := gate.ExecContext(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "command:"+commandID); err != nil {
				t.Fatal(err)
			}
			gatePID := postgres.BackendPID(t, ctx, gate)
			admitter, admitterPID := connectStore(t, ctx, databaseURL)
			rejecter, rejecterPID := connectStore(t, ctx, databaseURL)
			admitted := make(chan error, 1)
			go func() {
				_, err := enqueueWithSession(t, admitter, ctx, sessionID, "test-rejection-race", uid, input(commandID, "concurrent command"), nil)
				admitted <- err
			}()
			postgres.WaitForBlocker(t, ctx, store.DB, admitterPID, gatePID)
			rejected := make(chan error, 1)
			go func() {
				_, err := rejecter.RejectSession(ctx, sessionID, deliveryOwner, []string{SessionWaitingState}, "proven rejection")
				rejected <- err
			}()
			// Admission has the Session lock but has not inserted the command yet.
			postgres.WaitForBlocker(t, ctx, store.DB, rejecterPID, admitterPID)
			if err := gate.Commit(); err != nil {
				t.Fatal(err)
			}
			if err := <-admitted; err != nil {
				t.Fatal(err)
			}
			if err := <-rejected; err != nil {
				t.Fatal(err)
			}
			if row, err := store.GetDispatch(ctx, commandID); err != nil || row.State != FailedState {
				t.Fatalf("rejection missed concurrent admission: %+v %v", row, err)
			}
			if err := store.EndSandbox(ctx, uid); err != nil {
				t.Fatal(err)
			}
			if row, err := store.RejectSession(ctx, sessionID, deliveryOwner, []string{FailedState}, "late rejection"); err != nil || row != nil {
				t.Fatalf("rejection resurrected cleaned state: %+v %v", row, err)
			}
		})
	}
}

func TestDeliveryAttemptAndDeletionFenceOrderings(t *testing.T) {
	for _, markFirst := range []bool{false, true} {
		t.Run(fmt.Sprintf("mark-first=%t", markFirst), func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			store, databaseURL := testStoreWithURL(t, 3)
			head, err := enqueueWithSession(t, store, ctx, "fence-order-session", "sandbox-worker", sandboxUID, input("fence-order-input", "hello"), nil)
			if err != nil {
				t.Fatal(err)
			}
			deliveryOwner := claimSessionOwner(t, store, head.SessionID)
			if _, err := store.Settle(ctx, head.CommandID, deliveryOwner, []string{QueueState}, DeliveringState, "", nil); err != nil {
				t.Fatal(err)
			}
			marker, markerPID := connectStore(t, ctx, databaseURL)
			deleter, deleterPID := connectStore(t, ctx, databaseURL)
			gate, err := store.DB.BeginTx(ctx, nil)
			if err != nil {
				t.Fatal(err)
			}
			defer gate.Rollback()
			var gatePID int
			if err := gate.QueryRowContext(ctx, "SELECT pg_backend_pid() FROM session_delivery_lease WHERE session_id=$1 FOR UPDATE", head.SessionID).Scan(&gatePID); err != nil {
				t.Fatal(err)
			}
			marked := make(chan bool, 1)
			errors := make(chan error, 2)
			mark := func() {
				value, err := marker.MarkDeliveryAttempt(ctx, head.CommandID, deliveryOwner)
				marked <- value
				errors <- err
			}
			deleteEnvironment := func() { errors <- deleter.EndSandbox(ctx, sandboxUID) }
			// Queue the real transactions in both lock orders before opening the gate.
			if markFirst {
				go mark()
				postgres.WaitForBlocker(t, ctx, store.DB, markerPID, gatePID)
				go deleteEnvironment()
				postgres.WaitForBlocker(t, ctx, store.DB, deleterPID, markerPID)
			} else {
				go deleteEnvironment()
				postgres.WaitForBlocker(t, ctx, store.DB, deleterPID, gatePID)
				go mark()
				postgres.WaitForBlocker(t, ctx, store.DB, markerPID, deleterPID)
			}
			if err := gate.Commit(); err != nil {
				t.Fatal(err)
			}
			for range 2 {
				if err := <-errors; err != nil {
					t.Fatal(err)
				}
			}
			if got := <-marked; got != markFirst {
				t.Fatalf("marker permission=%t, want %t", got, markFirst)
			}
			want := FailedState
			if markFirst {
				want = UnknownState
			}
			var state string
			if err := store.DB.QueryRowContext(ctx, "SELECT state FROM dispatch_tombstone WHERE command_id=$1", head.CommandID).Scan(&state); err != nil {
				t.Fatal(err)
			}
			if state != want {
				t.Fatalf("ordered deletion state = %s, want %s", state, want)
			}
			if marked, err := store.MarkDeliveryAttempt(ctx, head.CommandID, deliveryOwner); err != nil || marked {
				t.Fatalf("deleted worker gained permission to forward: %t, %v", marked, err)
			}
		})
	}
}

func TestSessionTransactionChecksRetainedAuthorityBeforeConflict(t *testing.T) {
	store := testStore(t, 3)
	owner := &identitypb.Identity{Name: "transaction-owner", Uid: "00000000-0000-4000-8000-00000000a006"}
	foreign := &identitypb.Identity{Name: "transaction-foreign", Uid: "00000000-0000-4000-8000-00000000a007"}
	ownerCaller := IdentityCaller{Identity: owner, Proof: IdentitySVID{}}
	foreignCaller := IdentityCaller{Identity: foreign, Proof: IdentitySVID{}}
	id, uid := uuid.NewString(), uuid.NewString()
	spec := &sessionpb.SessionSpec{}
	testPlacement(spec, "transaction-sandbox", uid)
	if _, err := store.CreateSessionWithPlacementForCaller(t.Context(), id, "transaction-sandbox", uid,
		spec, nil, nil, SessionExecution{Identity: owner}, ownerCaller); err != nil {
		t.Fatal(err)
	}
	// This is the transactional path after another opener wins the Session-ID lock,
	// including when the API's initial read did not yet see that winning opening.
	for _, name := range []string{"transaction-sandbox", "conflicting-sandbox"} {
		conflictingSpec := &sessionpb.SessionSpec{}
		testPlacement(conflictingSpec, name, uid)
		if _, err := store.CreateSessionWithPlacementForCaller(t.Context(), id, name, uid,
			conflictingSpec, nil, nil, SessionExecution{Identity: foreign}, foreignCaller); !errors.Is(err, ErrNotFound) {
			t.Fatalf("transaction exposed an existing opening to foreign Identity: %v", err)
		}
	}
}

func TestDeletedAdmittedInputRetainsKnownAdmissionWithUnknownEffect(t *testing.T) {
	ctx := t.Context()
	store := testStore(t, 3)
	head, err := enqueueWithSession(t, store, ctx, "admitted-effect-session", "sandbox-worker", sandboxUID, input("admitted-effect-input", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	deliveryOwner := claimSessionOwner(t, store, head.SessionID)
	admission := &eventlogpb.EventEntry{Cursor: 17}
	if _, err := store.Settle(ctx, head.CommandID, deliveryOwner, []string{QueueState}, AdmittedState, "", admission); err != nil {
		t.Fatal(err)
	}
	if err := store.EndSandbox(ctx, sandboxUID); err != nil {
		t.Fatal(err)
	}
	var state string
	var admissionBytes []byte
	if err := store.DB.QueryRowContext(ctx, "SELECT state, admission FROM dispatch_tombstone WHERE command_id=$1", head.CommandID).Scan(&state, &admissionBytes); err != nil {
		t.Fatal(err)
	}
	retained := &eventlogpb.EventEntry{}
	if err := proto.Unmarshal(admissionBytes, retained); err != nil {
		t.Fatal(err)
	}
	if state != UnknownState || !proto.Equal(retained, admission) {
		t.Fatalf("deleted admitted input = %s with admission %v, want %s with admission %v", state, retained, UnknownState, admission)
	}
}
