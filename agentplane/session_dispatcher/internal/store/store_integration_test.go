package store

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/placementtest"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/terminalnotice"
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
	rows, err := store.ListDispatches(ctx, "session-a", "", nil)
	if err != nil || len(rows) != 3 {
		t.Fatalf("list: %v, %v", rows, err)
	}
	for _, row := range rows {
		if row.State != QueueState {
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

func TestSessionLeaseFencesStaleHolderAndRecovers(t *testing.T) {
	ctx := context.Background()
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
	stale, err := store.AcquireSessionLease(ctx, "session-b")
	if err != nil {
		t.Fatal(err)
	}
	if row, err := store.SettleSession(ctx, "session-b", *stale, []string{SessionWaitingState}, SessionInProgressState, ""); err != nil || row == nil {
		t.Fatalf("first holder could not settle: %+v %v", row, err)
	}
	expireSessionLease(t, store, *stale)
	successor, err := store.AcquireSessionLease(ctx, "session-b")
	if err != nil {
		t.Fatal(err)
	}
	if row, err := store.SettleSession(ctx, "session-b", *stale, []string{SessionInProgressState}, SessionAcceptedState, "stale"); err != nil || row != nil {
		t.Fatalf("stale holder settled after lease handoff: %+v %v", row, err)
	}
	if ok, err := store.RecoverSession(ctx, *successor); err != nil || !ok {
		t.Fatalf("recovery: %v %v", ok, err)
	}
	t.Cleanup(func() {
		_ = store.ReleaseSessionLease(context.Background(), *stale)
		_ = store.ReleaseSessionLease(context.Background(), *successor)
	})
	if row, err := store.GetSession(ctx, "session-b"); err != nil || row.State != SessionWaitingState || row.Reason != "dispatcher_restarted" {
		t.Fatalf("recovered opening: %+v %v", row, err)
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
	ctx := context.Background()
	store := testStore(t, 3)

	legacyFirst := wakeBudgetCaller("mixed-writer", "legacy-first", "")
	currentFirst := proto.Clone(legacyFirst).(*identitypb.ServiceAccountIdentity)
	currentFirst.Uid = "current-first-uid"
	legacySandboxUID := uuid.NewString()
	if wait, err := reserveWakeAttemptAsPreviousWriter(ctx, store, legacyFirst, legacySandboxUID, 1); err != nil || wait != 0 {
		t.Fatalf("legacy first reservation: %v %v", wait, err)
	}
	if wait, err := store.ReserveWakeAttempt(ctx, wakeKey(currentFirst), legacySandboxUID, 1); err != nil || wait != 0 {
		t.Fatalf("current reader did not recognize legacy same-generation reservation: %v %v", wait, err)
	}
	assertWakeAttemptCount(t, store, legacyFirst, legacySandboxUID, 1)

	currentThenLegacy := wakeBudgetCaller("mixed-writer", "current-first", "current-first-uid")
	currentSandboxUID := uuid.NewString()
	if wait, err := store.ReserveWakeAttempt(ctx, wakeKey(currentThenLegacy), currentSandboxUID, 1); err != nil || wait != 0 {
		t.Fatalf("current first reservation: %v %v", wait, err)
	}
	if wait, err := reserveWakeAttemptAsPreviousWriter(ctx, store, currentThenLegacy, currentSandboxUID, 1); err != nil || wait != 0 {
		t.Fatalf("legacy reader did not recognize current same-generation reservation: %v %v", wait, err)
	}
	assertWakeAttemptCount(t, store, currentThenLegacy, currentSandboxUID, 1)

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
	if _, err := lockTx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", lockKey); err != nil {
		_ = lockTx.Rollback()
		t.Fatal("take old-writer advisory lock:", err)
	}
	newWriterDone := make(chan error, 1)
	newWriterStarted := make(chan struct{})
	go func() {
		close(newWriterStarted)
		_, err := store.ReserveWakeAttempt(ctx, wakeKey(lockCaller), lockSandboxUID, 1)
		newWriterDone <- err
	}()
	<-newWriterStarted
	select {
	case err := <-newWriterDone:
		_ = lockTx.Rollback()
		t.Fatalf("new writer passed an old-version lock before release: %v", err)
	case <-time.After(200 * time.Millisecond):
	}
	if err := lockTx.Commit(); err != nil {
		t.Fatal("release old-writer advisory lock:", err)
	}
	select {
	case err := <-newWriterDone:
		if err != nil {
			t.Fatal("new writer after old lock release:", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("new writer stayed blocked after old lock release")
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
	deliveryOwner := claimSessionOwner(t, store, "session-deleted")
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
	events, err := store.EndSandbox(ctx, uid)
	if err != nil || len(events) != 2 {
		t.Fatalf("terminal events: %+v %v", events, err)
	}
	if events[0].Dispatch.GetCommandId() != "deleted-queued" || events[0].Dispatch.GetReason() != "sandbox_deleted" || events[0].Dispatch.GetState().String() != "DELIVERY_STATE_FAILED" {
		t.Fatalf("queued terminal: %+v", events[0])
	}
	if events[1].Dispatch.GetCommandId() != "deleted-delivering" || events[1].Dispatch.GetState().String() != "DELIVERY_STATE_UNKNOWN" {
		t.Fatalf("in-flight terminal: %+v", events[1])
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
	for commandID, want := range map[string]string{"deleted-queued": FailedState, "deleted-delivering": UnknownState, "test-deleted-cancelled": CancelledState} {
		var state, reason string
		if err := store.DB.QueryRowContext(ctx, `SELECT state,reason FROM dispatch_tombstone WHERE command_id=$1`, commandID).Scan(&state, &reason); err != nil {
			t.Fatal(err)
		}
		wantReason := "sandbox_deleted"
		if want == CancelledState {
			wantReason = "test-prior-cancellation"
		}
		if state != want || reason != wantReason {
			t.Fatalf("retained deletion %s = %s/%s, want %s/%s", commandID, state, reason, want, wantReason)
		}
	}
}

// A command interrupted after its claim remains durable. An expired Session lease lets a
// successor recover the in-flight command without losing the attempt marker.
func TestSuccessorSessionLeaseRequeuesDeliveringCommand(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000083"
	if _, err := enqueueWithSession(t, store, ctx, "interrupted-session", "interrupted-sandbox", uid, input("interrupted-command", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	owner := claimSessionOwner(t, store, "interrupted-session")
	if row, err := store.Settle(ctx, "interrupted-command", owner, []string{QueueState}, DeliveringState, "", nil); err != nil || row == nil {
		t.Fatalf("claim: %+v %v", row, err)
	}
	if marked, err := store.MarkDeliveryAttempt(ctx, "interrupted-command", owner); err != nil || !marked {
		t.Fatalf("mark attempt: %t %v", marked, err)
	}
	expireSessionLease(t, store, owner)
	successor := claimSessionOwner(t, store, "interrupted-session")
	if ok, err := store.RecoverSession(ctx, successor); err != nil || !ok {
		t.Fatalf("recover Session: %t %v", ok, err)
	}
	if row, err := store.GetDispatch(ctx, "interrupted-command"); err != nil || row.State != QueueState || row.Reason != "dispatcher_restarted" || !row.DeliveryAttempted {
		t.Fatalf("successor did not durably requeue the interrupted command: %+v %v", row, err)
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
	var gatePID int
	if err := gate.QueryRowContext(ctx, "SELECT pg_backend_pid()").Scan(&gatePID); err != nil {
		t.Fatal(err)
	}
	connect := func() (*Store, int) {
		db := postgres.Open(t, databaseURL)
		db.SetMaxOpenConns(1)
		var pid int
		if err := db.QueryRowContext(ctx, "SELECT pg_backend_pid()").Scan(&pid); err != nil {
			t.Fatal(err)
		}
		return NewStore(db, 3), pid
	}
	openingStore, openingPID := connect()
	cleanupStore, cleanupPID := connect()
	opened := make(chan error, 1)
	go func() {
		_, err := createSession(openingStore, ctx, sessionID, "sandbox-race", uid, &sessionpb.SessionSpec{}, nil, nil)
		opened <- err
	}()
	waitForPostgresBlocker(t, ctx, store.DB, openingPID, gatePID)
	cleaned := make(chan error, 1)
	go func() {
		_, err := cleanupStore.EndSandbox(ctx, uid)
		cleaned <- err
	}()
	// Cleanup must wait for the transaction whose opening has not yet been inserted.
	waitForPostgresBlocker(t, ctx, store.DB, cleanupPID, openingPID)
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
	store, databaseURL := testStoreWithURL(t, 3)
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	sessionID, uid, commandID := uuid.NewString(), uuid.NewString(), uuid.NewString()
	if _, err := createSession(store, ctx, sessionID, "test-rejection-race", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
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
	var gatePID int
	if err := gate.QueryRowContext(ctx, "SELECT pg_backend_pid()").Scan(&gatePID); err != nil {
		t.Fatal(err)
	}
	connect := func() (*Store, int) {
		db := postgres.Open(t, databaseURL)
		db.SetMaxOpenConns(1)
		var pid int
		if err := db.QueryRowContext(ctx, "SELECT pg_backend_pid()").Scan(&pid); err != nil {
			t.Fatal(err)
		}
		return NewStore(db, 3), pid
	}
	admitter, admitterPID := connect()
	rejecter, rejecterPID := connect()
	admitted := make(chan error, 1)
	go func() {
		_, err := enqueueWithSession(t, admitter, ctx, sessionID, "test-rejection-race", uid, input(commandID, "concurrent command"), nil)
		admitted <- err
	}()
	waitForPostgresBlocker(t, ctx, store.DB, admitterPID, gatePID)
	rejected := make(chan error, 1)
	go func() {
		_, err := rejecter.RejectSession(ctx, sessionID, deliveryOwner, []string{SessionWaitingState}, "proven rejection")
		rejected <- err
	}()
	// Admission has the Session lock but has not inserted the command yet.
	waitForPostgresBlocker(t, ctx, store.DB, rejecterPID, admitterPID)
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
	if _, err := store.EndSandbox(ctx, uid); err != nil {
		t.Fatal(err)
	}
	if row, err := store.RejectSession(ctx, sessionID, deliveryOwner, []string{FailedState}, "late rejection"); err != nil || row != nil {
		t.Fatalf("rejection resurrected cleaned state: %+v %v", row, err)
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
			connect := func() (*Store, int) {
				db := postgres.Open(t, databaseURL)
				db.SetMaxOpenConns(1)
				var pid int
				if err := db.QueryRowContext(ctx, "SELECT pg_backend_pid()").Scan(&pid); err != nil {
					t.Fatal(err)
				}
				return NewStore(db, 3), pid
			}
			marker, markerPID := connect()
			deleter, deleterPID := connect()
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
			deleted := make(chan []terminalnotice.Event, 1)
			errors := make(chan error, 2)
			mark := func() {
				value, err := marker.MarkDeliveryAttempt(ctx, head.CommandID, deliveryOwner)
				marked <- value
				errors <- err
			}
			deleteEnvironment := func() { events, err := deleter.EndSandbox(ctx, sandboxUID); deleted <- events; errors <- err }
			// Queue the real transactions in both lock orders before opening the gate.
			if markFirst {
				go mark()
				waitForPostgresBlocker(t, ctx, store.DB, markerPID, gatePID)
				go deleteEnvironment()
				waitForPostgresBlocker(t, ctx, store.DB, deleterPID, markerPID)
			} else {
				go deleteEnvironment()
				waitForPostgresBlocker(t, ctx, store.DB, deleterPID, gatePID)
				go mark()
				waitForPostgresBlocker(t, ctx, store.DB, markerPID, deleterPID)
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
			events := <-deleted
			want := dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED
			if markFirst {
				want = dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN
			}
			if len(events) != 1 || events[0].Dispatch.GetState() != want {
				t.Fatalf("ordered deletion: %+v; want %s", events, want)
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
	placementtest.EnsurePlacement(spec, "transaction-sandbox", uid)
	if _, err := store.CreateSessionWithPlacementForCaller(t.Context(), id, "transaction-sandbox", uid,
		spec, nil, nil, SessionExecution{Identity: owner}, ownerCaller); err != nil {
		t.Fatal(err)
	}
	// This is the transactional path after another opener wins the Session-ID lock,
	// including when the API's initial read did not yet see that winning opening.
	for _, name := range []string{"transaction-sandbox", "conflicting-sandbox"} {
		conflictingSpec := &sessionpb.SessionSpec{}
		placementtest.EnsurePlacement(conflictingSpec, name, uid)
		if _, err := store.CreateSessionWithPlacementForCaller(t.Context(), id, name, uid,
			conflictingSpec, nil, nil, SessionExecution{Identity: foreign}, foreignCaller); !errors.Is(err, ErrNotFound) {
			t.Fatalf("transaction exposed an existing opening to foreign Identity: %v", err)
		}
	}
}

func TestDeletedAdmittedInputKeepsKnownAdmissionInUnknownEffectSnapshot(t *testing.T) {
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
	events, err := store.EndSandbox(ctx, sandboxUID)
	if err != nil || len(events) != 1 {
		t.Fatalf("terminal snapshot: %+v %v", events, err)
	}
	if events[0].Dispatch.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN || !proto.Equal(events[0].Dispatch.GetAdmission(), admission) {
		t.Fatalf("unknown effects erased known admission: %+v", events[0])
	}
}
