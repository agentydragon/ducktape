package dispatcher

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	_ "github.com/lib/pq"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/client-go/util/workqueue"
)

const sandboxUID = "00000000-0000-4000-8000-000000000001"

// caller is the account the store tests' calls were admitted as.
var caller = auth.Account{Namespace: "test-callers", Name: "test-app"}

func configuredDispatcherCallers() sets.Set[auth.Account] {
	return sets.New(caller)
}

type admissionContextCapture struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	captured chan context.Context
	release  chan struct{}
}

func (s admissionContextCapture) GetDispatch(ctx context.Context, _ *dispatcherpb.GetDispatchRequest) (*dispatcherpb.GetDispatchResponse, error) {
	s.captured <- ctx
	<-s.release
	return nil, status.Error(codes.Canceled, "fixture complete")
}

// admitted returns a live context captured behind the real shared gRPC admission middleware.
// The fixture holds its RPC open so the context remains valid for direct handler/store tests.
func admitted(t *testing.T) context.Context {
	t.Helper()
	const audience, token = "dispatcher-test", "dispatcher-test-token"
	resolver, err := auth.NewResolver(tokenReviewStub{
		token: {account: caller, uid: "test-account-uid", pod: true},
	}, audience, []string{caller.Namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{dispatcherpb.SessionDispatcher_GetDispatch_FullMethodName: auth.ReviewedServiceAccount(resolver)}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	service := admissionContextCapture{captured: make(chan context.Context, 1), release: make(chan struct{})}
	server := grpc.NewServer(methods.ServerOptions(0)...)
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	go func() { _ = server.Serve(listener) }()
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		server.Stop()
		t.Fatal(err)
	}
	callContext, cancel := context.WithCancel(context.Background())
	callContext = metadata.AppendToOutgoingContext(callContext, "authorization", "Bearer "+token)
	go func() {
		_, _ = dispatcherpb.NewSessionDispatcherClient(connection).GetDispatch(callContext, &dispatcherpb.GetDispatchRequest{})
	}()
	var captured context.Context
	select {
	case captured = <-service.captured:
	case <-time.After(5 * time.Second):
		cancel()
		server.Stop()
		connection.Close()
		t.Fatal("shared auth middleware did not admit the fixture call")
	}
	t.Cleanup(func() {
		cancel()
		close(service.release)
		server.Stop()
		_ = connection.Close()
	})
	return captured
}

type oneSandbox struct{}

func (oneSandbox) GetExecutionIdentity(context.Context, SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return nil, true, nil
}

func (oneSandbox) GetExecutionAccount(context.Context, SandboxSnapshot) (*ServiceAccountIdentity, bool, error) {
	return nil, false, nil
}

func (oneSandbox) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	if name != "sandbox-api" {
		return SandboxSnapshot{}, false, nil
	}
	return SandboxSnapshot{Name: name, Namespace: caller.Namespace, UID: sandboxUID}, true, nil
}

type blockedSandbox struct{}

func (blockedSandbox) GetExecutionIdentity(context.Context, SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return nil, true, nil
}

func (blockedSandbox) GetExecutionAccount(context.Context, SandboxSnapshot) (*ServiceAccountIdentity, bool, error) {
	return nil, false, nil
}

func (blockedSandbox) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	if name != "sandbox-api" {
		return SandboxSnapshot{}, false, nil
	}
	return SandboxSnapshot{Name: name, Namespace: caller.Namespace, UID: sandboxUID, Blocker: "sandbox_suspended"}, true, nil
}

type rejectingRunner struct {
	command  *commandpb.Command
	err      error
	admitted map[string]bool
}

func (r *rejectingRunner) Deliver(_ context.Context, _ SandboxSnapshot, _ string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	r.command = command
	if r.admitted != nil {
		r.admitted[command.CommandId] = true
	}
	return nil, r.err
}

func (r *rejectingRunner) CreateSession(context.Context, SandboxSnapshot, string, *sessionpb.SessionSpec, *string, SessionExecution) error {
	return r.err
}

func testStore(t *testing.T, cap int) *Store {
	t.Helper()
	store, _ := testStoreWithURL(t, cap)
	return store
}

func testStoreWithURL(t *testing.T, cap int) (*Store, string) {
	t.Helper()
	databaseURL := integrationDatabase.Clone(t)
	db := postgres.Open(t, databaseURL)
	return NewStore(db, cap), databaseURL
}

func input(id, value string) *commandpb.Command {
	return &commandpb.Command{CommandId: id, Operation: &commandpb.Command_SubmitInput{
		SubmitInput: &commandpb.SubmitInput{Text: value},
	}}
}

// enqueueWithSession seeds an already materialized Session for tests of command delivery.
func enqueueWithSession(t *testing.T, store *Store, ctx context.Context, sessionID, sandboxName, sandboxUID string, command *commandpb.Command, wakeCaller *Account) (*Dispatch, error) {
	t.Helper()
	if _, err := store.GetSession(ctx, sessionID); errors.Is(err, ErrNotFound) {
		if _, err := store.CreateSession(ctx, sessionID, sandboxName, sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
			return nil, err
		}
		if _, err := store.DB.ExecContext(ctx, "UPDATE session SET state=$2 WHERE session_id=$1", sessionID, SessionAcceptedState); err != nil {
			return nil, err
		}
	} else if err != nil {
		return nil, err
	}
	return store.Enqueue(ctx, sessionID, sandboxName, sandboxUID, command, wakeCaller)
}

func TestAdmissionIsDurableAndIdempotent(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	first, err := enqueueWithSession(t, store, ctx, "session-a", "sandbox-a", sandboxUID, input("a", "hello"), &Account{"agentplane", "app"})
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

func TestWakeAssessmentRoundTripsForDispatchAndSession(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	uid := uuid.NewString()
	dispatch, err := enqueueWithSession(t, store, ctx, "wake-receipt-session", "sandbox-wake-receipt", uid,
		input("wake-receipt-command", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	opening, err := store.CreateSession(ctx, "wake-receipt-opening", "sandbox-wake-receipt", uid,
		&sessionpb.SessionSpec{}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	for name, got := range map[string]*environmentpb.WakeAssessment{
		"dispatch": dispatch.WakeAssessment,
		"opening":  opening.WakeAssessment,
	} {
		if got.GetState() != environmentpb.WakeState_WAKE_STATE_NOT_EVALUATED {
			t.Fatalf("initial %s wake state = %s", name, got.GetState())
		}
	}
	owner, err := store.ClaimSandboxOwner(ctx, uid, "wake-receipt-lease", 1, "wake-receipt-replica", 0)
	if err != nil || owner == nil {
		t.Fatalf("claim Sandbox owner: %+v %v", owner, err)
	}
	blocker := &environmentpb.DeliveryBlocker{Kind: environmentpb.DeliveryBlockerKind_DELIVERY_BLOCKER_KIND_ENVIRONMENT_SUSPENDED}
	assessment := &environmentpb.WakeAssessment{
		State:      environmentpb.WakeState_WAKE_STATE_DENIED,
		Reason:     environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED,
		ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(7),
	}
	if err := store.SetDeliveryAssessment(ctx, dispatch.CommandID, *owner, false, blocker, assessment); err != nil {
		t.Fatal(err)
	}
	if err := store.SetDeliveryAssessment(ctx, opening.SessionID, *owner, true, blocker, assessment); err != nil {
		t.Fatal(err)
	}
	gotDispatch, err := store.GetDispatch(ctx, dispatch.CommandID)
	if err != nil {
		t.Fatal(err)
	}
	rows, err := store.ListDispatches(ctx, dispatch.SessionID, "", nil)
	if err != nil || len(rows) != 1 {
		t.Fatalf("list dispatches: %d rows, %v", len(rows), err)
	}
	gotSession, err := store.GetSession(ctx, opening.SessionID)
	if err != nil {
		t.Fatal(err)
	}
	for name, got := range map[string]struct {
		blocker    *environmentpb.DeliveryBlocker
		assessment *environmentpb.WakeAssessment
	}{
		"get dispatch":  {gotDispatch.DeliveryBlocker, gotDispatch.WakeAssessment},
		"list dispatch": {rows[0].DeliveryBlocker, rows[0].WakeAssessment},
		"opening":       {gotSession.DeliveryBlocker, gotSession.WakeAssessment},
	} {
		if !proto.Equal(got.blocker, blocker) || !proto.Equal(got.assessment, assessment) {
			t.Fatalf("%s assessment: blocker=%v wake=%v", name, got.blocker, got.assessment)
		}
	}
	wireDispatch, err := DescribeDispatch(gotDispatch)
	if err != nil || !proto.Equal(wireDispatch.GetWakeAssessment(), assessment) || !proto.Equal(wireDispatch.GetDeliveryBlocker(), blocker) {
		t.Fatalf("dispatch projection: %+v %v", wireDispatch, err)
	}
	wireSession, err := DescribeSession(gotSession)
	if err != nil || !proto.Equal(wireSession.GetStatus().GetWakeAssessment(), assessment) || !proto.Equal(wireSession.GetStatus().GetDeliveryBlocker(), blocker) {
		t.Fatalf("opening projection: %+v %v", wireSession, err)
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
	owner, err := store.ClaimSandboxOwner(ctx, uid, "wake-deadline-lease", 1, "wake-deadline-replica", 0)
	if err != nil || owner == nil {
		t.Fatalf("claim Sandbox owner: %+v %v", owner, err)
	}
	requestedAt := time.Now().Add(-WakeDeadline - time.Second)
	if _, err := store.NoteWakeRequestedAt(ctx, dispatch.CommandID, *owner, false, requestedAt); err != nil {
		t.Fatal(err)
	}
	assessment := &environmentpb.WakeAssessment{
		State:      environmentpb.WakeState_WAKE_STATE_FAILED,
		Reason:     environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE,
		ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(5),
		Error: status.New(codes.DeadlineExceeded, "Sandbox did not become Ready before the wake deadline").Proto(),
	}
	failed, _, remaining, err := store.FailExpiredWake(ctx, dispatch.CommandID, *owner, false,
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

func TestSessionAndOwnershipFence(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	spec := &sessionpb.SessionSpec{}
	opened, err := store.CreateSession(ctx, "session-b", "sandbox-b", sandboxUID, spec, nil, nil)
	if err != nil || opened.State != SessionWaitingState {
		t.Fatalf("open: %+v %v", opened, err)
	}
	again, err := store.CreateSession(ctx, "session-b", "sandbox-b", sandboxUID, spec, nil, nil)
	if err != nil || again.CreatedAt != opened.CreatedAt {
		t.Fatalf("opening retry: %+v %v", again, err)
	}
	otherUID := "00000000-0000-4000-8000-000000000002"
	otherSpec := proto.Clone(spec).(*sessionpb.SessionSpec)
	otherSpec.GetPlacement().GetEnvironmentHosted().GetEnvironment().Uid = otherUID
	if _, err := store.CreateSession(ctx, "session-b", "sandbox-b", otherUID, otherSpec, nil, nil); !errors.Is(err, ErrSessionConflict) {
		t.Fatalf("opening UID conflict: %v", err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, sandboxUID, "lease", 1, "replica-a", 0)
	if err != nil || owner == nil || owner.Generation != 1 {
		t.Fatalf("first owner: %+v %v", owner, err)
	}
	if stale, err := store.ClaimSandboxOwner(ctx, sandboxUID, "old-lease", 1, "stale", 0); err != nil || stale != nil {
		t.Fatalf("stale CAS: %+v %v", stale, err)
	}
	successor, err := store.ClaimSandboxOwner(ctx, sandboxUID, "lease", 2, "replica-b", 1)
	if err != nil || successor == nil || successor.Generation != 2 {
		t.Fatalf("successor: %+v %v", successor, err)
	}
	if row, err := store.SettleSession(ctx, "session-b", *owner, []string{SessionWaitingState}, SessionInProgressState, ""); err != nil || row != nil {
		t.Fatalf("old owner settled: %+v %v", row, err)
	}
	if row, err := store.SettleSession(ctx, "session-b", *successor, []string{SessionWaitingState}, SessionInProgressState, ""); err != nil || row == nil {
		t.Fatalf("new owner could not settle: %+v %v", row, err)
	}
	if ok, err := store.RecoverSandbox(ctx, *successor); err != nil || !ok {
		t.Fatalf("recovery: %v %v", ok, err)
	}
	if row, err := store.GetSession(ctx, "session-b"); err != nil || row.State != SessionWaitingState || row.Reason != "dispatcher_restarted" {
		t.Fatalf("recovered opening: %+v %v", row, err)
	}
}

func wakeBudgetCaller(namespace, name, uid string) *ServiceAccountIdentity {
	return serviceAccountIdentityFor(Account{Namespace: namespace, Name: name}, uid)
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
			wait, err := store.ReserveWakeAttempt(ctx, caller, sandboxUID, int64(i+1))
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
	if wait, err := store.ReserveWakeAttempt(ctx, caller, sandboxUID, acceptedGeneration); err != nil || wait != 0 {
		t.Fatalf("same-generation retry: %v %v", wait, err)
	}
	callerAccount := accountOfIdentity(caller)
	replacement := wakeBudgetCaller(callerAccount.Namespace, callerAccount.Name, "replacement-wake-budget-app-uid")
	if wait, err := store.ReserveWakeAttempt(ctx, replacement, sandboxUID, acceptedGeneration); err != nil || wait != 0 {
		t.Fatalf("replacement ServiceAccount inherited old budget: %v %v", wait, err)
	}
}

func TestMixedVersionWakeWritersShareBudgetAndFenceReplacementUID(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)

	legacyFirst := wakeBudgetCaller("mixed-writer", "legacy-first", "")
	currentFirst := proto.Clone(legacyFirst).(*ServiceAccountIdentity)
	currentFirst.Uid = "current-first-uid"
	legacySandboxUID := uuid.NewString()
	if wait, err := reserveWakeAttemptAsPreviousWriter(ctx, store, legacyFirst, legacySandboxUID, 1); err != nil || wait != 0 {
		t.Fatalf("legacy first reservation: %v %v", wait, err)
	}
	if wait, err := store.ReserveWakeAttempt(ctx, currentFirst, legacySandboxUID, 1); err != nil || wait != 0 {
		t.Fatalf("current reader did not recognize legacy same-generation reservation: %v %v", wait, err)
	}
	assertWakeAttemptCount(t, store, legacyFirst, legacySandboxUID, 1)

	currentThenLegacy := wakeBudgetCaller("mixed-writer", "current-first", "current-first-uid")
	currentSandboxUID := uuid.NewString()
	if wait, err := store.ReserveWakeAttempt(ctx, currentThenLegacy, currentSandboxUID, 1); err != nil || wait != 0 {
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
		_, err := store.ReserveWakeAttempt(ctx, lockCaller, lockSandboxUID, 1)
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
				wait, err = store.ReserveWakeAttempt(ctx, mixedCaller, mixedSandboxUID, int64(i+1))
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
		if wait, err := store.ReserveWakeAttempt(ctx, replaced, replacementSandboxUID, generation); err != nil || wait != 0 {
			t.Fatalf("old UID reservation %d: %v %v", generation, wait, err)
		}
	}
	if wait, err := reserveWakeAttemptAsPreviousWriter(ctx, store, replaced, replacementSandboxUID, WakeLimit+1); err != nil || wait == 0 {
		t.Fatalf("previous writer did not count all nonempty UIDs: %v %v", wait, err)
	}
	replaced.Uid = "replacement-uid"
	if wait, err := store.ReserveWakeAttempt(ctx, replaced, replacementSandboxUID, WakeLimit+1); err != nil || wait != 0 {
		t.Fatalf("replacement UID inherited another nonempty UID's budget: %v %v", wait, err)
	}
}

// reserveWakeAttemptAsPreviousWriter reproduces the SQL shape deployed before
// caller_uid existed: namespace/name locking, all-UID reads, and an INSERT that
// relies on the migration's empty-UID default.
func reserveWakeAttemptAsPreviousWriter(
	ctx context.Context,
	store *Store,
	caller *ServiceAccountIdentity,
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

func assertWakeAttemptCount(t *testing.T, store *Store, caller *ServiceAccountIdentity, sandboxUID string, want int) {
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

func TestOnlyRunnerAdmissionRecordsAdmission(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000085"
	if _, err := enqueueWithSession(t, store, ctx, "admission-session", "sandbox-admission", uid, input("admission-input", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, uid, "admission-lease", 1, "replica-a", 0)
	if err != nil || owner == nil {
		t.Fatalf("owner: %+v %v", owner, err)
	}
	claimed, err := store.Settle(ctx, "admission-input", *owner, []string{QueueState}, DeliveringState, "", nil)
	if err != nil || claimed == nil || claimed.Admission != nil {
		t.Fatalf("claimed row carries an admission: %+v %v", claimed, err)
	}
	admission := &eventlogpb.EventEntry{Cursor: 7, Origin: &eventlogpb.EventOrigin{SourceId: "runner", Sequence: 7}}
	if _, err := store.Settle(ctx, "admission-input", *owner, []string{DeliveringState}, AdmittedState, "", admission); err != nil {
		t.Fatal(err)
	}
	if row, err := store.GetDispatch(ctx, "admission-input"); err != nil || !proto.Equal(row.Admission, admission) {
		t.Fatalf("admitted row: %+v %v", row, err)
	}
}

func TestDeletedSandboxBroadcastsTerminalRowsBeforeDeletingThem(t *testing.T) {
	ctx := context.Background()
	store, databaseURL := testStoreWithURL(t, 3)
	uid := "00000000-0000-4000-8000-000000000099"
	if _, err := enqueueWithSession(t, store, ctx, "session-deleted", "sandbox-deleted", uid, input("deleted-queued", "q"), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := enqueueWithSession(t, store, ctx, "session-deleted", "sandbox-deleted", uid, input("deleted-delivering", "d"), nil); err != nil {
		t.Fatal(err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, uid, "lease-deleted", 1, "replica-a", 0)
	if err != nil || owner == nil {
		t.Fatalf("owner: %+v %v", owner, err)
	}
	if row, err := store.Settle(ctx, "deleted-delivering", *owner, []string{QueueState}, DeliveringState, "", nil); err != nil || row == nil {
		t.Fatalf("settle: %+v %v", row, err)
	}
	if marked, err := store.MarkDeliveryAttempt(ctx, "deleted-delivering", *owner); err != nil || !marked {
		t.Fatalf("mark in-flight delivery: %t %v", marked, err)
	}
	// Two Dispatcher replicas must both receive the committed terminal snapshots:
	// the rows are deleted in the same transaction and cannot be read afterward.
	subscribers := make([]*watchSubscription, 0, 2)
	for replica := range 2 {
		hub := NewWatchHub(databaseURL)
		listenContext, cancel := context.WithCancel(ctx)
		ended := make(chan error, 1)
		go func() { ended <- hub.Run(listenContext) }()
		t.Cleanup(func() {
			cancel()
			select {
			case <-ended:
			case <-time.After(5 * time.Second):
				t.Errorf("replica %d PostgreSQL listener did not stop", replica)
			}
		})
		ready := time.After(5 * time.Second)
		for !hub.Connected() {
			select {
			case err := <-ended:
				t.Fatalf("replica %d listener stopped before subscription: %v", replica, err)
			case <-ready:
				t.Fatalf("replica %d listener did not subscribe", replica)
			case <-time.After(10 * time.Millisecond):
			}
		}
		sub, unsubscribe, err := hub.subscribe()
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
		for _, want := range events {
			select {
			case got := <-sub.terminal:
				if got.ID != want.ID || !proto.Equal(got.Dispatch, want.Dispatch) {
					t.Fatalf("replica %d terminal snapshot: got %+v, want %+v", replica, got, want)
				}
			case failure := <-sub.failure:
				t.Fatalf("replica %d terminal watch failed: %s", replica, failure)
			case <-time.After(5 * time.Second):
				t.Fatalf("replica %d missed terminal snapshot %s", replica, want.ID)
			}
		}
	}
	if _, err := store.GetDispatch(ctx, "deleted-queued"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleted row visible: %v", err)
	}
	if ok, err := store.IsCurrentOwner(ctx, *owner); err != nil || ok {
		t.Fatalf("deleted owner: %v %v", ok, err)
	}
}

func TestDeletedSandboxRetainsSessionsAndSettlesUnfinishedMaterialization(t *testing.T) {
	ctx := t.Context()
	store := testStore(t, 3)
	uid := uuid.NewString()
	states := []struct{ before, after string }{
		{SessionWaitingState, SessionFailedState},
		{SessionInProgressState, SessionUnknownState},
		{SessionAcceptedState, SessionAcceptedState},
		{SessionFailedState, SessionFailedState},
		{SessionUnknownState, SessionUnknownState},
	}
	ids := make([]string, len(states))
	for i, state := range states {
		ids[i] = uuid.NewString()
		if _, err := store.CreateSession(ctx, ids[i], "deleted-sandbox", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
			t.Fatal(err)
		}
		if _, err := store.DB.ExecContext(ctx, "UPDATE session SET state=$2,reason='prior evidence' WHERE session_id=$1", ids[i], state.before); err != nil {
			t.Fatal(err)
		}
	}
	if events, err := store.EndSandbox(ctx, uid); err != nil || len(events) != 0 {
		t.Fatalf("cleanup: %+v %v", events, err)
	}
	if uids, err := store.ListSandboxUIDs(ctx); err != nil || len(uids) != 0 {
		t.Fatalf("retained Sessions re-entered cleanup inventory: %+v %v", uids, err)
	}
	for i, state := range states {
		row, err := store.GetSession(ctx, ids[i])
		if err != nil || row.EnvironmentDeletedAt == nil || row.SandboxUID != uid || row.State != state.after {
			t.Fatalf("retained Session %d: %+v %v", i, row, err)
		}
		if state.before == SessionWaitingState || state.before == SessionInProgressState {
			if row.Reason != "sandbox_deleted" {
				t.Fatalf("unfinished Session reason %d = %q", i, row.Reason)
			}
		} else if row.Reason != "prior evidence" {
			t.Fatalf("terminal Session evidence %d changed: %q", i, row.Reason)
		}
		projection, err := DescribeSession(row)
		if err != nil || projection.GetStatus().GetRequiredEnvironment() != dispatcherpb.SessionStatus_REQUIRED_ENVIRONMENT_DELETED {
			t.Fatalf("retained Session availability %d: %+v %v", i, projection, err)
		}
		if events, err := store.EndSandbox(ctx, uid); err != nil || len(events) != 0 {
			t.Fatalf("repeated cleanup: %+v %v", events, err)
		}
		after, err := store.GetSession(ctx, ids[i])
		if err != nil || !after.UpdatedAt.Equal(row.UpdatedAt) || !after.EnvironmentDeletedAt.Equal(*row.EnvironmentDeletedAt) {
			t.Fatalf("repeated cleanup rewrote Session %d: %+v %v", i, after, err)
		}
	}
}

func TestTerminalPayloadRoundTripForWatchers(t *testing.T) {
	hub := &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true}
	sub, unsubscribe, err := hub.subscribe()
	if err != nil {
		t.Fatal(err)
	}
	defer unsubscribe()
	reason := "sandbox_deleted"
	event := TerminalEvent{ID: "00000000-0000-4000-8000-000000000123", Dispatch: &dispatcherpb.Dispatch{
		CommandId: "terminal", Sandbox: strings.Repeat("sandbox", 1200),
		State: dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED, Reason: &reason,
	}}
	payloads, err := terminalPayloads(event)
	if err != nil {
		t.Fatal(err)
	}
	if len(payloads) < 2 {
		t.Fatalf("large terminal snapshot did not span multiple NOTIFY payloads: %d", len(payloads))
	}
	// LISTEN preserves order on one connection, but separate writers and reconnects
	// make reassembly independent of arrival order.
	for i := len(payloads) - 1; i >= 0; i-- {
		if len(payloads[i]) > terminalMaxNotifyBytes {
			t.Fatalf("part %d exceeds PostgreSQL NOTIFY limit", i)
		}
		if err := hub.terminalPart(payloads[i]); err != nil {
			t.Fatal(err)
		}
	}
	select {
	case received := <-sub.terminal:
		if received.ID != event.ID || !proto.Equal(received.Dispatch, event.Dispatch) {
			t.Fatalf("wrong terminal event %+v", received)
		}
	default:
		t.Fatal("watcher did not receive terminal event")
	}
}

func TestRPCAdmissionReturnsSameQueuedReceiptAndConflicts(t *testing.T) {
	store := testStore(t, 3)
	service := &Service{
		Store: store, DispatcherCallers: configuredDispatcherCallers(), Sandboxes: oneSandbox{},
	}
	ctx := admitted(t)
	if _, err := store.CreateSession(ctx, "api-session", "sandbox-api", sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	request := &dispatcherpb.SubmitInputRequest{CommandId: "api-a", SessionId: "api-session", Text: "hello"}
	first, err := service.SubmitInput(ctx, request)
	if err != nil || first.GetDispatch().GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER || first.GetDispatch().GetSandboxUid() != sandboxUID {
		t.Fatalf("first receipt: %+v %v", first, err)
	}
	again, err := service.SubmitInput(ctx, request)
	if err != nil || again.GetDispatch().GetCreatedAt().AsTime() != first.GetDispatch().GetCreatedAt().AsTime() {
		t.Fatalf("retry: %+v %v", again, err)
	}
	_, err = service.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{CommandId: "api-a", SessionId: "api-session", ExpectedSandboxUid: sandboxUID, Text: "different"})
	if status.Code(err) != codes.AlreadyExists {
		t.Fatalf("conflict status: %v", err)
	}
	_, err = service.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{CommandId: "api-b", SessionId: "missing", ExpectedSandboxUid: sandboxUID})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("missing Sandbox status: %v", err)
	}
	_, err = service.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
		CommandId: "api-replacement", SessionId: "api-session", ExpectedSandboxUid: "old-sandbox-uid", Text: "must not cross",
	})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("replacement Sandbox status: %v", err)
	}
}

type collectedWatch struct {
	grpc.ServerStream
	ctx    context.Context
	frames []*dispatcherpb.WatchDispatchesResponse
}

func (w *collectedWatch) Context() context.Context { return w.ctx }

func (w *collectedWatch) Send(frame *dispatcherpb.WatchDispatchesResponse) error {
	w.frames = append(w.frames, frame)
	return nil
}

type channelWatch struct {
	grpc.ServerStream
	ctx    context.Context
	frames chan *dispatcherpb.WatchDispatchesResponse
}

func (w *channelWatch) Context() context.Context { return w.ctx }

func (w *channelWatch) Send(frame *dispatcherpb.WatchDispatchesResponse) error {
	select {
	case w.frames <- frame:
		return nil
	case <-w.ctx.Done():
		return w.ctx.Err()
	}
}

type blockedDispatchWatch struct {
	grpc.ServerStream
	ctx          context.Context
	started      chan struct{}
	finished     chan struct{}
	release      <-chan struct{}
	startOnce    sync.Once
	finishedOnce sync.Once
}

func (w *blockedDispatchWatch) Context() context.Context { return w.ctx }

func (w *blockedDispatchWatch) Send(*dispatcherpb.WatchDispatchesResponse) error {
	w.startOnce.Do(func() { close(w.started) })
	<-w.release
	w.finishedOnce.Do(func() { close(w.finished) })
	return nil
}

func TestWatchSendsMatchingDispatchesThenCaughtUp(t *testing.T) {
	ctx := admitted(t)
	store := testStore(t, 5)
	for _, id := range []string{"watch-a", "watch-b", "watch-c"} {
		if _, err := enqueueWithSession(t, store, ctx, "watch-session", "sandbox-api", sandboxUID, input(id, "x"), nil); err != nil {
			t.Fatal(err)
		}
	}
	service := &Service{
		Store: store, DispatcherCallers: configuredDispatcherCallers(), Hub: &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true},
		WatchLease: 100 * time.Millisecond,
	}
	watch := &collectedWatch{ctx: ctx}
	err := service.WatchDispatches(&dispatcherpb.WatchDispatchesRequest{Filter: &dispatcherpb.DispatchFilter{
		Scope:      &dispatcherpb.DispatchFilter_SessionId{SessionId: "watch-session"},
		CommandIds: []string{"watch-a", "watch-c", "watch-absent"},
	}}, watch)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, frame := range watch.frames {
		switch frame := frame.GetFrame().(type) {
		case *dispatcherpb.WatchDispatchesResponse_Dispatch:
			got = append(got, frame.Dispatch.GetCommandId())
		case *dispatcherpb.WatchDispatchesResponse_CaughtUp:
			got = append(got, "caught-up")
		}
	}
	if want := []string{"watch-a", "watch-c", "caught-up"}; !slices.Equal(got, want) {
		t.Fatalf("frames %v, want %v", got, want)
	}
}

func TestWatchDispatchesPublishesWakeOnlyReceiptChanges(t *testing.T) {
	base := admitted(t)
	store, databaseURL := testStoreWithURL(t, 3)
	uid := uuid.NewString()
	dispatch, err := enqueueWithSession(t, store, base, "watch-wake-session", "sandbox-wake-watch", uid,
		input("watch-wake-command", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := store.ClaimSandboxOwner(base, uid, "watch-wake-lease", 1, "watch-wake-replica", 0)
	if err != nil || owner == nil {
		t.Fatalf("claim Sandbox owner: %+v %v", owner, err)
	}
	hub := NewWatchHub(databaseURL)
	listenerCtx, cancelListener := context.WithCancel(context.Background())
	listenerDone := make(chan error, 1)
	go func() { listenerDone <- hub.Run(listenerCtx) }()
	t.Cleanup(func() {
		cancelListener()
		select {
		case <-listenerDone:
		case <-time.After(5 * time.Second):
			t.Error("PostgreSQL watch listener did not stop")
		}
	})
	deadline := time.After(5 * time.Second)
	for !hub.Connected() {
		select {
		case err := <-listenerDone:
			t.Fatalf("listener stopped before subscription: %v", err)
		case <-deadline:
			t.Fatal("listener did not subscribe")
		case <-time.After(10 * time.Millisecond):
		}
	}
	watchCtx, cancelWatch := context.WithCancel(base)
	defer cancelWatch()
	service := &Service{
		Store: store, DispatcherCallers: configuredDispatcherCallers(), Hub: hub, WatchLease: time.Minute,
	}
	stream := &channelWatch{ctx: watchCtx, frames: make(chan *dispatcherpb.WatchDispatchesResponse, 8)}
	watchDone := make(chan error, 1)
	go func() {
		watchDone <- service.WatchDispatches(&dispatcherpb.WatchDispatchesRequest{Filter: &dispatcherpb.DispatchFilter{
			Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: dispatch.SessionID},
		}}, stream)
	}()
	receive := func() *dispatcherpb.WatchDispatchesResponse {
		t.Helper()
		select {
		case frame := <-stream.frames:
			return frame
		case err := <-watchDone:
			t.Fatalf("watch stopped before expected frame: %v", err)
			return nil
		case <-time.After(5 * time.Second):
			t.Fatal("timed out waiting for watch frame")
			return nil
		}
	}
	first := receive().GetDispatch()
	if first == nil || first.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER ||
		first.GetWakeAssessment().GetState() != environmentpb.WakeState_WAKE_STATE_NOT_EVALUATED {
		t.Fatalf("initial watch receipt: %+v", first)
	}
	if receive().GetCaughtUp() == nil {
		t.Fatal("watch did not send CaughtUp after initial receipt")
	}
	blocker := &environmentpb.DeliveryBlocker{Kind: environmentpb.DeliveryBlockerKind_DELIVERY_BLOCKER_KIND_ENVIRONMENT_SUSPENDED}
	assessment := &environmentpb.WakeAssessment{
		State:      environmentpb.WakeState_WAKE_STATE_DENIED,
		Reason:     environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED,
		ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(11),
	}
	if err := store.SetDeliveryAssessment(base, dispatch.CommandID, *owner, false, blocker, assessment); err != nil {
		t.Fatal(err)
	}
	updated := receive().GetDispatch()
	if updated == nil || updated.GetState() != first.GetState() || updated.GetReason() != first.GetReason() ||
		!proto.Equal(updated.GetDeliveryBlocker(), blocker) || !proto.Equal(updated.GetWakeAssessment(), assessment) {
		t.Fatalf("wake-only watch update: %+v", updated)
	}
	cancelWatch()
	select {
	case err := <-watchDone:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("WatchDispatches() = %v, want cancellation", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("watch did not stop after caller cancellation")
	}
}

func TestWatchDispatchesEndsBlockedWriteOnLeaseOrCancellation(t *testing.T) {
	for _, tc := range []struct {
		name    string
		cancel  bool
		wantErr bool
	}{
		{name: "lease"},
		{name: "caller cancellation", cancel: true, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			base := admitted(t)
			ctx, cancel := context.WithCancel(base)
			defer cancel()
			release := make(chan struct{})
			stream := &blockedDispatchWatch{
				ctx: ctx, started: make(chan struct{}), finished: make(chan struct{}), release: release,
			}
			done := make(chan error, 1)
			leaseCtx, endLease := context.WithCancel(ctx)
			defer endLease()
			if tc.cancel {
				service := &Service{
					Store: testStore(t, 5), DispatcherCallers: configuredDispatcherCallers(), Hub: &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true},
					WatchLease: time.Hour,
				}
				request := &dispatcherpb.WatchDispatchesRequest{Filter: &dispatcherpb.DispatchFilter{
					Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: "blocked-watch-session"},
				}}
				go func() { done <- service.WatchDispatches(request, stream) }()
			} else {
				// End the send's lease after Send starts, independent of how long a
				// PostgreSQL connection or initial snapshot would take to acquire.
				go func() {
					ended, err := sendWatchDispatch(leaseCtx, ctx, stream, &dispatcherpb.WatchDispatchesResponse{
						Frame: &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}},
					})
					if !ended && err == nil {
						err = errors.New("send did not end at its lease boundary")
					}
					done <- err
				}()
			}
			select {
			case <-stream.started:
			case <-time.After(5 * time.Second):
				t.Fatal("WatchDispatches did not reach its caught-up write")
			}
			if tc.cancel {
				cancel()
			} else {
				endLease()
			}
			select {
			case err := <-done:
				if tc.wantErr {
					if !errors.Is(err, context.Canceled) {
						t.Fatalf("WatchDispatches() = %v, want context.Canceled", err)
					}
				} else if err != nil {
					t.Fatalf("WatchDispatches() = %v, want clean lease renewal", err)
				}
			case <-time.After(5 * time.Second):
				t.Fatal("lease or caller cancellation did not return the handler")
			}

			close(release)
			select {
			case <-stream.finished:
			case <-time.After(5 * time.Second):
				t.Fatal("the blocked Send did not finish after release")
			}
		})
	}
}

func TestInterruptTurnQueuesAndReconcilesExactTurn(t *testing.T) {
	ctx := admitted(t)
	store := testStore(t, 3)
	service := &Service{Store: store, DispatcherCallers: configuredDispatcherCallers(), Sandboxes: oneSandbox{}}
	if _, err := store.CreateSession(ctx, "api-session", "sandbox-api", sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	request := &dispatcherpb.InterruptTurnRequest{CommandId: "interrupt-stale-turn", SessionId: "api-session", ExpectedSandboxUid: sandboxUID, TurnId: "turn-stale"}
	empty := proto.Clone(request).(*dispatcherpb.InterruptTurnRequest)
	empty.TurnId = ""
	if _, err := service.InterruptTurn(ctx, empty); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("empty turn_id status = %v, want InvalidArgument", err)
	}
	first, err := service.InterruptTurn(ctx, request)
	if err != nil || first.GetDispatch().GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER || first.GetDispatch().GetOperation() != commandpb.Operation_OPERATION_INTERRUPT_TURN {
		t.Fatalf("queued interrupt: %v %v", first, err)
	}
	retry, err := service.InterruptTurn(ctx, request)
	if err != nil || !proto.Equal(first, retry) {
		t.Fatalf("interrupt retry: %v %v", retry, err)
	}
	conflict := proto.Clone(request).(*dispatcherpb.InterruptTurnRequest)
	conflict.TurnId = "other-turn"
	if _, err := service.InterruptTurn(ctx, conflict); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("conflicting target: %v", err)
	}
	head, err := store.GetDispatch(ctx, request.CommandId)
	if err != nil || head.Command.GetInterruptTurn().GetTurnId() != request.TurnId {
		t.Fatalf("stored exact turn: %+v %v", head, err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, sandboxUID, "interrupt-lease", 1, "replica", 0)
	if err != nil || owner == nil {
		t.Fatalf("owner: %+v %v", owner, err)
	}
	runner := &rejectingRunner{err: status.Error(codes.FailedPrecondition, "turn is no longer current")}
	worker := &DeliveryWorker{Store: store, Runner: runner}
	worker.init()
	defer worker.queue.ShutDown()
	if !worker.deliverCommand(ctx, head, SandboxSnapshot{UID: sandboxUID}, *owner, nil) {
		t.Fatal("worker did not attempt interrupt")
	}
	row, err := store.GetDispatch(ctx, request.CommandId)
	if err != nil || row.State != UnknownState || runner.command.GetInterruptTurn().GetTurnId() != request.TurnId {
		t.Fatalf("stale interrupt result: %+v %v", row, err)
	}
}

type testCatalog struct{ sandbox SandboxSnapshot }

func (c testCatalog) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	return c.sandbox, name == c.sandbox.Name, nil
}

func (c testCatalog) ListSandboxes(context.Context) ([]SandboxSnapshot, error) {
	return []SandboxSnapshot{c.sandbox}, nil
}

func (c testCatalog) CurrentSandboxUIDs(context.Context) (map[string]struct{}, error) {
	return map[string]struct{}{c.sandbox.UID: {}}, nil
}
func (testCatalog) Changes() <-chan struct{}        { return nil }
func (testCatalog) WaitSynced(context.Context) bool { return true }

type growingCatalog struct{ sandboxes []SandboxSnapshot }

func (c *growingCatalog) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	for _, sandbox := range c.sandboxes {
		if sandbox.Name == name {
			return sandbox, true, nil
		}
	}
	return SandboxSnapshot{}, false, nil
}

func (c *growingCatalog) ListSandboxes(context.Context) ([]SandboxSnapshot, error) {
	return c.sandboxes, nil
}

func (c *growingCatalog) CurrentSandboxUIDs(context.Context) (map[string]struct{}, error) {
	result := make(map[string]struct{}, len(c.sandboxes))
	for _, sandbox := range c.sandboxes {
		result[sandbox.UID] = struct{}{}
	}
	return result, nil
}
func (*growingCatalog) Changes() <-chan struct{}        { return nil }
func (*growingCatalog) WaitSynced(context.Context) bool { return true }

type testWake struct{}

func (testWake) RequestWake(_ context.Context, _ *lifecycle.Target, _ *lifecycle.ServiceAccountIdentity) (*environmentpb.WakeAssessment, error) {
	return &environmentpb.WakeAssessment{
		State:      environmentpb.WakeState_WAKE_STATE_NOT_NEEDED,
		ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(0),
	}, nil
}

type rateLimitedWake struct {
	calls int
}

func (w *rateLimitedWake) RequestWake(_ context.Context, _ *lifecycle.Target, _ *lifecycle.ServiceAccountIdentity) (*environmentpb.WakeAssessment, error) {
	w.calls++
	return &environmentpb.WakeAssessment{
		State:      environmentpb.WakeState_WAKE_STATE_RATE_LIMITED,
		Reason:     environmentpb.WakeReason_WAKE_REASON_WAKE_BUDGET_EXHAUSTED,
		ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(1),
		RetryAt: timestamppb.New(time.Now().Add(time.Minute)),
	}, nil
}

type scriptedWake struct {
	assessments []*environmentpb.WakeAssessment
	callers     []*lifecycle.ServiceAccountIdentity
	targets     []*lifecycle.Target
}

func (w *scriptedWake) RequestWake(_ context.Context, target *lifecycle.Target, caller *lifecycle.ServiceAccountIdentity) (*environmentpb.WakeAssessment, error) {
	w.callers = append(w.callers, caller)
	w.targets = append(w.targets, target)
	if len(w.assessments) == 0 {
		return nil, errors.New("unexpected lifecycle wake request")
	}
	assessment := w.assessments[0]
	w.assessments = w.assessments[1:]
	return assessment, nil
}

type wakeWorkerFixture struct {
	store    *Store
	opening  *Session
	dispatch *Dispatch
	owner    Owner
	sandbox  SandboxSnapshot
	caller   *lifecycle.ServiceAccountIdentity
	worker   *DeliveryWorker
}

func newWakeWorkerFixture(t *testing.T, spec *sessionpb.SessionSpec, wake WakeRequester) wakeWorkerFixture {
	t.Helper()
	ctx := context.Background()
	store := testStore(t, 3)
	sandboxName, sandboxUID := "sandbox-"+uuid.NewString()[:8], uuid.NewString()
	if spec.GetPlacement().GetCentralSamplerV1() != nil {
		spec.GetPlacement().GetCentralSamplerV1().ExecutorEnvironment = &sessionpb.ExactEnvironment{Name: sandboxName, Uid: sandboxUID}
	}
	sessionID, commandID := uuid.NewString(), uuid.NewString()
	caller := &lifecycle.ServiceAccountIdentity{Account: &identitypb.ServiceAccount{Namespace: "agentplane", Name: sandboxName}, Uid: uuid.NewString()}
	actor := &lifecycle.ServiceAccountIdentity{Account: &identitypb.ServiceAccount{Namespace: "agentplane", Name: "action-broker"}, Uid: uuid.NewString()}
	callerIdentity := proto.Clone(caller).(*ServiceAccountIdentity)
	actorIdentity := proto.Clone(actor).(*ServiceAccountIdentity)
	attribution := &AdmissionAttribution{Actor: actorIdentity, EffectiveCaller: callerIdentity}
	opening, err := store.CreateSessionForCaller(ctx, sessionID, sandboxName, sandboxUID, spec, nil, nil,
		callerIdentity, CallerIdentity{ServiceAccountIdentity: callerIdentity, Attribution: attribution})
	if err != nil {
		t.Fatal(err)
	}
	dispatch, err := store.EnqueueForCaller(ctx, sessionID, sandboxName, sandboxUID, input(commandID, "hello"), nil,
		CallerIdentity{ServiceAccountIdentity: callerIdentity, Attribution: attribution})
	if err != nil {
		t.Fatal(err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, sandboxUID, "lease-"+sandboxName, 1, "wake-test-worker", 0)
	if err != nil || owner == nil {
		t.Fatalf("claim Sandbox owner: %+v %v", owner, err)
	}
	worker := &DeliveryWorker{Store: store, Wake: wake, Leases: testLease{store: store}}
	worker.init()
	t.Cleanup(worker.queue.ShutDown)
	return wakeWorkerFixture{
		store: store, opening: opening, dispatch: dispatch, owner: *owner, caller: caller,
		sandbox: SandboxSnapshot{
			Name: sandboxName, Namespace: "agentplane", UID: sandboxUID, Incarnation: "before-wake",
			Blocker: "sandbox_suspended", WakeOnSend: true, WakeGeneration: 7,
		},
		worker: worker,
	}
}

type testLease struct{ store *Store }

func (l testLease) Acquire(ctx context.Context, _, uid string) (*Owner, error) {
	generation, err := l.store.OwnershipGeneration(ctx, uid)
	if err != nil {
		return nil, err
	}
	return l.store.ClaimSandboxOwner(ctx, uid, "lease-test", 1, "replica-test", generation)
}

func (l testLease) StillOwns(ctx context.Context, owner Owner) (bool, error) {
	return l.store.IsCurrentOwner(ctx, owner)
}

type countingLease struct {
	testLease
	calls int
}

type unreliableLease struct {
	testLease
	failRenew bool
	live      bool
}

func (l *unreliableLease) Acquire(ctx context.Context, name, uid string) (*Owner, error) {
	if l.failRenew {
		return nil, errors.New("Lease API unavailable")
	}
	return l.testLease.Acquire(ctx, name, uid)
}

func (l *unreliableLease) StillOwns(ctx context.Context, owner Owner) (bool, error) {
	if !l.live {
		return false, nil
	}
	return l.testLease.StillOwns(ctx, owner)
}

func (l *countingLease) Acquire(ctx context.Context, name, uid string) (*Owner, error) {
	l.calls++
	return l.testLease.Acquire(ctx, name, uid)
}

func TestWorkerAcquiresNewSandboxWithoutRenewingExistingOwner(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	newUID := "00000000-0000-4000-8000-000000000071"
	catalog := &growingCatalog{sandboxes: []SandboxSnapshot{{Name: "sandbox-existing", UID: sandboxUID}}}
	leases := &countingLease{testLease: testLease{store: store}}
	worker := &DeliveryWorker{Store: store, Catalog: catalog, Leases: leases}
	worker.init()
	defer worker.queue.ShutDown()
	if _, err := enqueueWithSession(t, store, ctx, "existing-session", "sandbox-existing", sandboxUID, input("existing-input", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	if err := worker.reconcile(ctx, true); err != nil {
		t.Fatal(err)
	}
	if leases.calls != 1 {
		t.Fatalf("initial lease acquisitions = %d, want 1", leases.calls)
	}
	catalog.sandboxes = append(catalog.sandboxes, SandboxSnapshot{Name: "sandbox-new", UID: newUID})
	if _, err := enqueueWithSession(t, store, ctx, "new-session", "sandbox-new", newUID, input("new-input", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	if err := worker.reconcile(ctx, false); err != nil {
		t.Fatal(err)
	}
	if _, owned := worker.ownerFor(newUID); !owned {
		t.Fatal("new Sandbox has no owner after catalog reconciliation")
	}
	if leases.calls != 2 {
		t.Fatalf("catalog reconciliation made %d lease acquisitions, want 2", leases.calls)
	}
	if err := worker.reconcile(ctx, false); err != nil {
		t.Fatal(err)
	}
	if leases.calls != 2 {
		t.Fatalf("unchanged catalog renewed a lease: %d acquisitions", leases.calls)
	}
}

func TestWorkerLeasesOnlySandboxesWithQueuedOrDeliveringWork(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	const busyUID, idleUID = "00000000-0000-4000-8000-000000000081", "00000000-0000-4000-8000-000000000082"
	catalog := &growingCatalog{sandboxes: []SandboxSnapshot{
		{Name: "sandbox-lease-busy", UID: busyUID}, {Name: "sandbox-lease-idle", UID: idleUID},
	}}
	leases := &countingLease{testLease: testLease{store: store}}
	worker := &DeliveryWorker{Store: store, Catalog: catalog, Leases: leases}
	worker.init()
	defer worker.queue.ShutDown()
	reconcile := func(want int) {
		t.Helper()
		if err := worker.reconcile(ctx, true); err != nil {
			t.Fatal(err)
		}
		if leases.calls != want {
			t.Fatalf("lease acquisitions = %d, want %d", leases.calls, want)
		}
	}
	owned := func(uid string) bool {
		_, okay := worker.ownerFor(uid)
		return okay
	}
	reconcile(0)
	if _, err := enqueueWithSession(t, store, ctx, "lease-busy-session", "sandbox-lease-busy", busyUID, input("lease-busy-input", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	reconcile(1)
	if !owned(busyUID) || owned(idleUID) {
		t.Fatalf("owned busy=%v idle=%v, want only the Sandbox with queued work", owned(busyUID), owned(idleUID))
	}
	owner, _ := worker.ownerFor(busyUID)
	if row, err := store.Settle(ctx, "lease-busy-input", owner, []string{QueueState}, DeliveringState, "", nil); err != nil || row == nil {
		t.Fatalf("claim: %+v %v", row, err)
	}
	reconcile(2)
	if !owned(busyUID) {
		t.Fatal("a Sandbox with a delivering command lost its owner")
	}
	if row, err := store.Settle(ctx, "lease-busy-input", owner, []string{DeliveringState}, AdmittedState, "", nil); err != nil || row == nil {
		t.Fatalf("settle: %+v %v", row, err)
	}
	reconcile(2)
	if owned(busyUID) {
		t.Fatal("a Sandbox with only settled work kept its owner")
	}
	if _, err := enqueueWithSession(t, store, ctx, "lease-busy-session", "sandbox-lease-busy", busyUID, input("lease-busy-again", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	reconcile(3)
	if !owned(busyUID) {
		t.Fatal("new work did not reacquire the Sandbox")
	}
}

// A dispatch claimed by an owner that died before settling is DELIVERING, and
// neither Heads nor SessionHeads lists it, yet its successor must still take
// the Sandbox to requeue it.
func TestWorkerAdoptsDeliveringWorkOfDeadOwner(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000083"
	if _, err := enqueueWithSession(t, store, ctx, "dead-owner-session", "sandbox-dead-owner", uid, input("dead-owner-input", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	dead, err := store.ClaimSandboxOwner(ctx, uid, "dead-lease", 1, "replica-dead", 0)
	if err != nil || dead == nil {
		t.Fatalf("dead owner: %+v %v", dead, err)
	}
	if row, err := store.Settle(ctx, "dead-owner-input", *dead, []string{QueueState}, DeliveringState, "", nil); err != nil || row == nil {
		t.Fatalf("claim: %+v %v", row, err)
	}
	worker := &DeliveryWorker{
		Store: store, Catalog: testCatalog{SandboxSnapshot{Name: "sandbox-dead-owner", UID: uid}},
		Leases: testLease{store},
	}
	worker.init()
	defer worker.queue.ShutDown()
	if err := worker.reconcile(ctx, true); err != nil {
		t.Fatal(err)
	}
	if row, err := store.GetDispatch(ctx, "dead-owner-input"); err != nil || row.State != QueueState || row.Reason != "dispatcher_restarted" {
		t.Fatalf("successor did not requeue the dead owner's command: %+v %v", row, err)
	}
}

type retryRunner struct {
	mu        sync.Mutex
	calls     int
	commandID string
}

type recordingDelayQueue struct {
	workqueue.TypedRateLimitingInterface[string]
	delays []time.Duration
	added  []string
}

func (q *recordingDelayQueue) AddAfter(_ string, delay time.Duration) {
	q.delays = append(q.delays, delay)
}

func (q *recordingDelayQueue) Add(item string) {
	q.added = append(q.added, item)
}

func TestWorkerRetainsBackoffAcrossUncertainReplies(t *testing.T) {
	worker := &DeliveryWorker{}
	worker.init()
	defer worker.queue.ShutDown()
	recorded := &recordingDelayQueue{TypedRateLimitingInterface: worker.queue}
	worker.queue = recorded
	for attempt, delay := range []time.Duration{500 * time.Millisecond, time.Second, 2 * time.Second} {
		state, reason := worker.deliveryFailure("session", "command", sandboxUID, "pod-1",
			status.Error(codes.Unavailable, "uncertain reply"))
		if state != QueueState || reason != "runner_unreachable: Unavailable: uncertain reply" {
			t.Fatalf("attempt %d: %s %s", attempt+1, state, reason)
		}
		if got := worker.limiter.NumRequeues("session"); got != attempt+1 {
			t.Fatalf("attempt %d: limiter count %d", attempt+1, got)
		}
		if got := recorded.delays[attempt]; got != delay {
			t.Fatalf("attempt %d: queued delay %s, want %s", attempt+1, got, delay)
		}
	}
	worker.clearRetry("session")
	if got := worker.limiter.NumRequeues("session"); got != 0 {
		t.Fatalf("terminal settlement retained %d attempts", got)
	}
}

type acceptingRunner struct{ calls chan string }

func (r *acceptingRunner) Deliver(_ context.Context, _ SandboxSnapshot, _ string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	r.calls <- command.GetCommandId()
	return &eventlogpb.EventEntry{Cursor: 1}, nil
}

func (r *acceptingRunner) CreateSession(_ context.Context, _ SandboxSnapshot, sessionID string, _ *sessionpb.SessionSpec, _ *string, _ SessionExecution) error {
	r.calls <- "opening:" + sessionID
	return nil
}

func TestWorkerKeepsWakeBlockedWorkQueuedAndObservable(t *testing.T) {
	cases := []struct {
		name       string
		wakeOnSend bool
		manualStop bool
		assessment *environmentpb.WakeAssessment
		wantState  environmentpb.WakeState
		wantReason environmentpb.WakeReason
		wantCalls  int
	}{
		{name: "automatic wake disabled", wantState: environmentpb.WakeState_WAKE_STATE_AUTOMATIC_WAKE_DISABLED},
		{
			name: "caller denied", wakeOnSend: true,
			assessment: &environmentpb.WakeAssessment{
				State:  environmentpb.WakeState_WAKE_STATE_DENIED,
				Reason: environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED,
			},
			wantState:  environmentpb.WakeState_WAKE_STATE_DENIED,
			wantReason: environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED,
			wantCalls:  1,
		},
		{
			name: "manual stop is sticky", wakeOnSend: true, manualStop: true,
			wantState:  environmentpb.WakeState_WAKE_STATE_DENIED,
			wantReason: environmentpb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP,
		},
		{
			name: "known backend failure", wakeOnSend: true,
			assessment: &environmentpb.WakeAssessment{
				State:  environmentpb.WakeState_WAKE_STATE_FAILED,
				Reason: environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE,
				Error:  status.New(codes.Unavailable, "Sandbox API is unavailable").Proto(),
			},
			wantState:  environmentpb.WakeState_WAKE_STATE_FAILED,
			wantReason: environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE,
			wantCalls:  1,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var wake *scriptedWake
			if tc.assessment != nil {
				assessment := proto.Clone(tc.assessment).(*environmentpb.WakeAssessment)
				assessment.ObservedAt = timestamppb.Now()
				assessment.TargetGeneration = proto.Int64(7)
				wake = &scriptedWake{assessments: []*environmentpb.WakeAssessment{assessment}}
			}
			fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{}, wake)
			fixture.sandbox.WakeOnSend = tc.wakeOnSend
			fixture.sandbox.ManualStop = tc.manualStop
			if ready, err := fixture.worker.prepareDispatch(context.Background(), fixture.dispatch, fixture.sandbox, fixture.owner); err != nil || ready {
				t.Fatalf("blocked work preparation = %v, %v", ready, err)
			}
			row, err := fixture.store.GetDispatch(context.Background(), fixture.dispatch.CommandID)
			if err != nil || row.State != QueueState || row.WakeAssessment.GetState() != tc.wantState ||
				row.WakeAssessment.GetReason() != tc.wantReason || row.WakeAssessment.GetObservedAt() == nil ||
				row.WakeAssessment.TargetGeneration == nil || row.DeliveryBlocker.GetKind() == 0 {
				t.Fatalf("wake-blocked dispatch was not durably observable: %+v %v", row, err)
			}
			var calls int
			if wake != nil {
				calls = len(wake.callers)
			}
			if calls != tc.wantCalls {
				t.Fatalf("lifecycle calls = %d, want %d", calls, tc.wantCalls)
			}
		})
	}
}

func TestWorkerDoesNotRequestWakeAfterLosingQueueLease(t *testing.T) {
	wake := &scriptedWake{}
	fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{}, wake)
	fixture.worker.Leases = &unreliableLease{testLease: testLease{store: fixture.store}, live: false}
	ready, err := fixture.worker.prepareDispatch(context.Background(), fixture.dispatch, fixture.sandbox, fixture.owner)
	if err != nil || ready || len(wake.callers) != 0 {
		t.Fatalf("wake after lease loss: ready=%v calls=%d err=%v", ready, len(wake.callers), err)
	}
	stored, err := fixture.store.GetDispatch(context.Background(), fixture.dispatch.CommandID)
	if err != nil || stored.State != QueueState || stored.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_NOT_EVALUATED {
		t.Fatalf("lease loss changed queued receipt: %+v %v", stored, err)
	}
}

func TestWorkerPersistsRateLimitForDispatchAndSession(t *testing.T) {
	for _, kind := range []string{"dispatch", "opening"} {
		t.Run(kind, func(t *testing.T) {
			assessment := &environmentpb.WakeAssessment{
				State:      environmentpb.WakeState_WAKE_STATE_RATE_LIMITED,
				Reason:     environmentpb.WakeReason_WAKE_REASON_WAKE_BUDGET_EXHAUSTED,
				ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(7),
				RetryAt: timestamppb.New(time.Now().Add(time.Minute)),
			}
			wake := &scriptedWake{assessments: []*environmentpb.WakeAssessment{assessment}}
			fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{}, wake)
			var ready bool
			var err error
			if kind == "dispatch" {
				ready, err = fixture.worker.prepareDispatch(context.Background(), fixture.dispatch, fixture.sandbox, fixture.owner)
			} else {
				ready, err = fixture.worker.prepareSession(context.Background(), fixture.opening, fixture.sandbox, fixture.owner)
			}
			if err != nil || ready {
				t.Fatalf("rate-limited work preparation = %v, %v", ready, err)
			}
			if kind == "dispatch" {
				row, err := fixture.store.GetDispatch(context.Background(), fixture.dispatch.CommandID)
				if err != nil || row.State != QueueState || row.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_RATE_LIMITED ||
					row.WakeAssessment.GetRetryAt() == nil || row.WakeAssessment.GetObservedAt() == nil {
					t.Fatalf("rate-limited dispatch: %+v %v", row, err)
				}
			} else {
				row, err := fixture.store.GetSession(context.Background(), fixture.opening.SessionID)
				if err != nil || row.State != SessionWaitingState || row.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_RATE_LIMITED ||
					row.WakeAssessment.GetRetryAt() == nil || row.WakeAssessment.GetObservedAt() == nil {
					t.Fatalf("rate-limited opening: %+v %v", row, err)
				}
			}
			if len(wake.callers) != 1 {
				t.Fatalf("lifecycle calls = %d, want one", len(wake.callers))
			}
			if kind == "dispatch" {
				row, err := fixture.store.GetDispatch(context.Background(), fixture.dispatch.CommandID)
				if err != nil {
					t.Fatal(err)
				}
				if _, err := fixture.worker.prepareDispatch(context.Background(), row, fixture.sandbox, fixture.owner); err != nil {
					t.Fatal(err)
				}
			} else {
				row, err := fixture.store.GetSession(context.Background(), fixture.opening.SessionID)
				if err != nil {
					t.Fatal(err)
				}
				if _, err := fixture.worker.prepareSession(context.Background(), row, fixture.sandbox, fixture.owner); err != nil {
					t.Fatal(err)
				}
			}
			if len(wake.callers) != 1 {
				t.Fatalf("rate-limited wake retried before retry_at: %d calls", len(wake.callers))
			}
		})
	}
}

func TestWorkerRetriesAuthorizedWakeAfterDenialUsingEffectiveCaller(t *testing.T) {
	requested := &environmentpb.WakeAssessment{
		State:      environmentpb.WakeState_WAKE_STATE_REQUESTED,
		ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(7),
	}
	wake := &scriptedWake{assessments: []*environmentpb.WakeAssessment{
		{
			State: environmentpb.WakeState_WAKE_STATE_DENIED, Reason: environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED,
			ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(7),
		},
		requested,
	}}
	fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{}, wake)
	prepare := func() *Dispatch {
		t.Helper()
		row, err := fixture.store.GetDispatch(context.Background(), fixture.dispatch.CommandID)
		if err != nil {
			t.Fatal(err)
		}
		if ready, err := fixture.worker.prepareDispatch(context.Background(), row, fixture.sandbox, fixture.owner); err != nil || ready {
			t.Fatalf("wake retry preparation = %v, %v", ready, err)
		}
		row, err = fixture.store.GetDispatch(context.Background(), fixture.dispatch.CommandID)
		if err != nil {
			t.Fatal(err)
		}
		return row
	}
	first := prepare()
	if first.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_DENIED || first.State != QueueState {
		t.Fatalf("first denial failed queued delivery: %+v", first)
	}
	second := prepare()
	if second.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_REQUESTED || second.State != QueueState || second.WakeRequestedAt == nil {
		t.Fatalf("later authorized wake was not recorded while queued: %+v", second)
	}
	if len(wake.callers) != 2 || !sameServiceAccountIdentity(wake.callers[0], fixture.caller) ||
		!sameServiceAccountIdentity(wake.callers[1], fixture.caller) {
		t.Fatalf("wake used caller identities %v, want retained effective caller %+v", wake.callers, fixture.caller)
	}
	if wake.targets[0].Uid != fixture.sandbox.UID || wake.targets[0].Namespace != fixture.sandbox.Namespace || wake.targets[0].Name != fixture.sandbox.Name {
		t.Fatalf("wake target was not UID-fenced: %+v", wake.targets[0])
	}
	prepare()
	if len(wake.callers) != 2 {
		t.Fatalf("requested wake was retried before readiness: %d calls", len(wake.callers))
	}
}

func TestWorkerDoesNotRepeatUnknownWakeInSameGeneration(t *testing.T) {
	wake := &scriptedWake{assessments: []*environmentpb.WakeAssessment{
		{
			State: environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN, Reason: environmentpb.WakeReason_WAKE_REASON_OUTCOME_UNKNOWN,
			ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(7),
			Error: status.New(codes.Unavailable, "patch reply was lost").Proto(),
		},
		{State: environmentpb.WakeState_WAKE_STATE_DENIED, Reason: environmentpb.WakeReason_WAKE_REASON_TARGET_CHANGED},
	}}
	fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{}, wake)
	prepare := func(sandbox SandboxSnapshot) *Dispatch {
		t.Helper()
		row, err := fixture.store.GetDispatch(context.Background(), fixture.dispatch.CommandID)
		if err != nil {
			t.Fatal(err)
		}
		if ready, err := fixture.worker.prepareDispatch(context.Background(), row, sandbox, fixture.owner); err != nil || ready {
			t.Fatalf("unknown wake preparation = %v, %v", ready, err)
		}
		row, err = fixture.store.GetDispatch(context.Background(), fixture.dispatch.CommandID)
		if err != nil {
			t.Fatal(err)
		}
		return row
	}
	unknown := prepare(fixture.sandbox)
	if unknown.State != QueueState || unknown.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN ||
		unknown.WakeAssessment.GetObservedAt() == nil || unknown.WakeAssessment.TargetGeneration == nil ||
		unknown.WakeAssessment.GetError().GetCode() != int32(codes.Unavailable) {
		t.Fatalf("unknown wake outcome was not retained as a structured queued observation: %+v", unknown)
	}
	prepare(fixture.sandbox)
	if len(wake.callers) != 1 {
		t.Fatalf("unknown wake was repeated in the same generation: %d calls", len(wake.callers))
	}
	changedGeneration := fixture.sandbox
	changedGeneration.WakeGeneration++
	changed := prepare(changedGeneration)
	if len(wake.callers) != 2 || changed.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_DENIED || changed.State != QueueState {
		t.Fatalf("changed generation did not trigger a fresh assessment: calls=%d receipt=%+v", len(wake.callers), changed)
	}
}

func TestCentralSessionAndInputBypassSuspendedExecutorWake(t *testing.T) {
	fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{}}}}, nil)
	if ready, err := fixture.worker.prepareSession(context.Background(), fixture.opening, fixture.sandbox, fixture.owner); err != nil || !ready {
		t.Fatalf("central opening required executor wake: ready=%v err=%v", ready, err)
	}
	if ready, err := fixture.worker.prepareDispatch(context.Background(), fixture.dispatch, fixture.sandbox, fixture.owner); err != nil || !ready {
		t.Fatalf("central input required executor wake: ready=%v err=%v", ready, err)
	}
	dispatch, err := fixture.store.GetDispatch(context.Background(), fixture.dispatch.CommandID)
	if err != nil || dispatch.State != QueueState || dispatch.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_NOT_NEEDED || dispatch.DeliveryBlocker != nil {
		t.Fatalf("central input did not retain a deliverable queued receipt: %+v %v", dispatch, err)
	}
	opening, err := fixture.store.GetSession(context.Background(), fixture.opening.SessionID)
	if err != nil || opening.State != SessionWaitingState || opening.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_NOT_NEEDED || opening.DeliveryBlocker != nil {
		t.Fatalf("central opening did not retain a deliverable queued receipt: %+v %v", opening, err)
	}
}

func TestWorkerNotificationPreservesFailureBackoff(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000073"
	const sessionID = "rpc-backoff-session"
	const commandID = "rpc-backoff-command"
	if _, err := enqueueWithSession(t, store, ctx, sessionID, "sandbox-rpc-backoff", uid, input(commandID, "hello"), nil); err != nil {
		t.Fatal(err)
	}
	runner := &acceptingRunner{calls: make(chan string, 1)}
	leases := testLease{store: store}
	worker := &DeliveryWorker{Store: store, Catalog: testCatalog{SandboxSnapshot{
		Name: "sandbox-rpc-backoff", Namespace: "agentplane", UID: uid, Incarnation: "pod-1",
	}}, Wake: testWake{}, Leases: leases, Runner: runner}
	worker.init()
	defer worker.queue.ShutDown()
	recorded := &recordingDelayQueue{TypedRateLimitingInterface: worker.queue}
	worker.queue = recorded
	owner, err := leases.Acquire(ctx, "sandbox-rpc-backoff", uid)
	if err != nil || owner == nil {
		t.Fatalf("acquire owner: %+v %v", owner, err)
	}
	worker.owners[uid] = *owner
	worker.setRetry(sessionID, commandID, time.Minute)
	before := len(recorded.delays)
	if err := worker.reconcile(ctx, false); err != nil {
		t.Fatal(err)
	}
	if len(recorded.added) != 0 || len(recorded.delays) <= before {
		t.Fatalf("notification failed to preserve delayed RPC retry: immediate %v, delayed %v", recorded.added, recorded.delays)
	}
	for _, delay := range recorded.delays[before:] {
		if delay <= 0 || delay > time.Until(worker.retryFor(sessionID).until)+time.Second {
			t.Fatalf("notification scheduled RPC retry outside due time: %s", delay)
		}
	}
	if err := worker.processSession(ctx, sessionID); err != nil {
		t.Fatal(err)
	}
	select {
	case got := <-runner.calls:
		t.Fatalf("RPC retry called Runner early: %s", got)
	default:
	}
}

func TestWorkerRequiresFreshLeaseBeforeDelivery(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	if _, err := enqueueWithSession(t, store, ctx, "fenced-session", "sandbox-fenced", sandboxUID, input("fenced-input", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	runner := &retryRunner{commandID: "fenced-input"}
	leases := &unreliableLease{testLease: testLease{store: store}, live: true}
	worker := &DeliveryWorker{Store: store, Catalog: testCatalog{SandboxSnapshot{
		Name: "sandbox-fenced", Namespace: "agentplane", UID: sandboxUID, Incarnation: "pod-1",
	}}, Wake: testWake{}, Leases: leases, Runner: runner}
	worker.init()
	defer worker.queue.ShutDown()
	if err := worker.reconcile(ctx, true); err != nil {
		t.Fatal(err)
	}
	leases.failRenew = true
	if err := worker.reconcile(ctx, true); err == nil {
		t.Fatal("renewal should fail")
	}
	if err := worker.processSession(ctx, "fenced-session"); err != nil {
		t.Fatal(err)
	}
	leases.failRenew = false
	if err := worker.reconcile(ctx, true); err != nil {
		t.Fatal(err)
	}
	leases.live = false
	if err := worker.processSession(ctx, "fenced-session"); err != nil {
		t.Fatal(err)
	}
	runner.mu.Lock()
	calls := runner.calls
	runner.mu.Unlock()
	if calls != 0 {
		t.Fatalf("stale Lease allowed %d Runner calls", calls)
	}
	leases.live = true
	if err := worker.reconcile(ctx, true); err != nil {
		t.Fatal(err)
	}
	if err := worker.processSession(ctx, "fenced-session"); err != nil {
		t.Fatal(err)
	}
	runner.mu.Lock()
	calls = runner.calls
	runner.mu.Unlock()
	if calls != 1 {
		t.Fatalf("fresh Lease made %d Runner calls, want 1", calls)
	}
}

func (r *retryRunner) Deliver(_ context.Context, sandbox SandboxSnapshot, _ string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	wantID := r.commandID
	if wantID == "" {
		wantID = "worker-input"
	}
	if sandbox.UID != sandboxUID || command.GetCommandId() != wantID {
		return nil, errors.New("wrong delivery identity")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls++
	if r.calls == 1 {
		return nil, status.Error(codes.Unavailable, "lost runner reply")
	}
	return &eventlogpb.EventEntry{Cursor: 1}, nil
}

func (*retryRunner) CreateSession(context.Context, SandboxSnapshot, string, *sessionpb.SessionSpec, *string, SessionExecution) error {
	return errors.New("unexpected opening")
}

func TestWorkerRetriesUncertainRunnerReplyUnderCurrentOwner(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	if _, err := enqueueWithSession(t, store, ctx, "worker-session", "sandbox-worker", sandboxUID, input("worker-input", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	runner := &retryRunner{}
	worker := &DeliveryWorker{Store: store, Catalog: testCatalog{SandboxSnapshot{
		Name: "sandbox-worker", Namespace: "agentplane", UID: sandboxUID, Incarnation: "pod-1",
	}}, Wake: testWake{}, Leases: testLease{store}, Runner: runner}
	worker.init()
	workerCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	defer worker.queue.ShutDown()
	go worker.runQueue(workerCtx)
	if err := worker.reconcile(ctx, true); err != nil {
		t.Fatal(err)
	}
	waitState := func(state string) *Dispatch {
		t.Helper()
		deadline := time.Now().Add(5 * time.Second)
		for time.Now().Before(deadline) {
			row, err := store.GetDispatch(ctx, "worker-input")
			if err == nil && row.State == state && (state != QueueState || row.Reason == "runner_unreachable: Unavailable: lost runner reply") {
				return row
			}
			time.Sleep(10 * time.Millisecond)
		}
		t.Fatalf("dispatch did not reach %s", state)
		return nil
	}
	queued := waitState(QueueState)
	if queued.Reason != "runner_unreachable: Unavailable: lost runner reply" {
		t.Fatalf("uncertain reply should retry: %+v", queued)
	}
	if time.Until(worker.retryFor("worker-session").until) <= 0 {
		t.Fatal("missing retry backoff")
	}
	admitted := waitState(AdmittedState)
	if admitted.Admission == nil {
		t.Fatalf("retry was not admitted: %+v", admitted)
	}
	runner.mu.Lock()
	calls := runner.calls
	runner.mu.Unlock()
	if calls != 2 {
		t.Fatalf("runner calls = %d, want 2", calls)
	}
}

type orderedRunner struct {
	entered chan string
	release chan struct{}
	mu      sync.Mutex
	active  int
	peak    int
}

func (r *orderedRunner) Deliver(_ context.Context, _ SandboxSnapshot, _ string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	r.mu.Lock()
	r.active++
	if r.active > r.peak {
		r.peak = r.active
	}
	r.mu.Unlock()
	r.entered <- command.GetCommandId()
	<-r.release
	r.mu.Lock()
	r.active--
	r.mu.Unlock()
	return &eventlogpb.EventEntry{Cursor: 1}, nil
}

func (*orderedRunner) CreateSession(context.Context, SandboxSnapshot, string, *sessionpb.SessionSpec, *string, SessionExecution) error {
	return errors.New("unexpected opening")
}

func TestWorkerSerializesCommandsPerSession(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store := testStore(t, 3)
	for _, id := range []string{"ordered-1", "ordered-2"} {
		if _, err := enqueueWithSession(t, store, ctx, "ordered-session", "sandbox-ordered", sandboxUID, input(id, id), nil); err != nil {
			t.Fatal(err)
		}
	}
	runner := &orderedRunner{entered: make(chan string, 2), release: make(chan struct{}, 2)}
	leases := &countingLease{testLease: testLease{store: store}}
	worker := &DeliveryWorker{Store: store, Catalog: testCatalog{SandboxSnapshot{
		Name: "sandbox-ordered", Namespace: "agentplane", UID: sandboxUID, Incarnation: "pod-1",
	}}, Wake: testWake{}, Leases: leases, Runner: runner}
	worker.init()
	defer worker.queue.ShutDown()
	go worker.runQueue(ctx)
	go worker.runQueue(ctx)
	if err := worker.reconcile(ctx, true); err != nil {
		t.Fatal(err)
	}
	if leases.calls != 1 {
		t.Fatalf("initial lease acquisitions = %d", leases.calls)
	}
	for _, want := range []string{"ordered-1", "ordered-2"} {
		select {
		case got := <-runner.entered:
			if got != want {
				t.Fatalf("delivery order = %s, want %s", got, want)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("missing %s delivery", want)
		}
		if err := worker.reconcile(ctx, false); err != nil {
			t.Fatal(err)
		}
		if leases.calls != 1 {
			t.Fatalf("delivery notification renewed lease %d times", leases.calls)
		}
		runner.release <- struct{}{}
	}
	runner.mu.Lock()
	peak := runner.peak
	runner.mu.Unlock()
	if peak != 1 {
		t.Fatalf("parallel deliveries for one Session: %d", peak)
	}
}

func TestCentralCommandsAndInterruptIgnoreExecutorReadiness(t *testing.T) {
	ctx := admitted(t)
	store := testStore(t, 3)
	spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: "sandbox-api", Uid: sandboxUID}}}}}
	opening, err := store.CreateSession(ctx, "central-unready-session", "sandbox-api", sandboxUID, spec, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	row, err := enqueueWithSession(t, store, ctx, opening.SessionID, "sandbox-api", sandboxUID, input("central-unready-input", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	sandbox, _, _ := (blockedSandbox{}).GetSandbox(ctx, "sandbox-api")
	worker := &DeliveryWorker{Store: store}
	worker.init()
	defer worker.queue.ShutDown()
	generation, err := store.OwnershipGeneration(ctx, sandboxUID)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, sandboxUID, "central-"+uuid.NewString(), 1, "central-worker", generation)
	if err != nil || owner == nil {
		t.Fatalf("claim central Sandbox owner: %+v %v", owner, err)
	}
	if ready, err := worker.prepareSession(ctx, opening, sandbox, *owner); err != nil || !ready {
		t.Fatalf("central opening readiness %v %v", ready, err)
	}
	if ready, err := worker.prepareDispatch(ctx, row, sandbox, *owner); err != nil || !ready {
		t.Fatalf("central command readiness %v %v", ready, err)
	}
	service := &Service{Store: store, DispatcherCallers: configuredDispatcherCallers(), Sandboxes: blockedSandbox{}}
	result, err := service.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{CommandId: "central-interrupt", SessionId: opening.SessionID, ExpectedSandboxUid: sandboxUID, TurnId: "active-turn"})
	if err != nil || result.GetDispatch().GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER {
		t.Fatalf("central interrupt: %v %v", result, err)
	}
}

func TestCentralSessionUsesConfiguredSampler(t *testing.T) {
	ctx := admitted(t)
	store := testStore(t, 3)
	service := &Service{Store: store, DispatcherCallers: configuredDispatcherCallers(), Sandboxes: blockedSandbox{}, CentralTarget: &CentralTarget{Target: "configured", TokenFile: "configured"}}
	request := &dispatcherpb.CreateSessionRequest{SessionId: uuid.NewString(), Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: "sandbox-api", Uid: sandboxUID}}}}, Model: "test-model", Cwd: "/test"}}
	opened, err := service.CreateSession(ctx, request)
	if err != nil {
		t.Fatal(err)
	}
	if opened.GetSession().GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment().GetUid() != sandboxUID {
		t.Fatalf("central opening lost its owner or initial executor: %v", opened.GetSession())
	}
	// Simulate a pre-upgrade stored spec containing retired field 6.
	legacy := proto.Clone(request.Spec).(*sessionpb.SessionSpec)
	legacy.ProtoReflect().SetUnknown([]byte{0x32, 0x00})
	encoded, err := wire(legacy)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB.ExecContext(ctx, "UPDATE session SET spec=$1 WHERE session_id=$2", encoded, request.SessionId); err != nil {
		t.Fatal(err)
	}
	retried, err := service.CreateSession(ctx, request)
	if err != nil {
		t.Fatalf("retry with retired metadata: %v", err)
	}
	if retried.GetSession().GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment().GetUid() != sandboxUID {
		t.Fatalf("central retry changed placement: %v", retried.GetSession())
	}
	service.CentralTarget = nil
	request.SessionId = uuid.NewString()
	if _, err := service.CreateSession(ctx, request); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("disabled sampler accepted: %v", err)
	}
}

func TestCreateSessionRejectsNonCanonicalUUIDBeforeRecording(t *testing.T) {
	ctx := admitted(t)
	store := testStore(t, 3)
	service := &Service{Store: store}
	canonical := "abcdefab-cdef-4abc-8def-abcdefabcdef"
	for _, sessionID := range []string{
		"not-a-uuid",
		strings.ToUpper(canonical),
		strings.ReplaceAll(canonical, "-", ""),
		"{" + canonical + "}",
		"urn:uuid:" + canonical,
	} {
		t.Run(sessionID, func(t *testing.T) {
			_, err := service.CreateSession(ctx, &dispatcherpb.CreateSessionRequest{SessionId: sessionID})
			if status.Code(err) != codes.InvalidArgument || status.Convert(err).Message() != "session_id must be a canonical UUID" {
				t.Fatalf("CreateSession(%q) = %v, want canonical UUID InvalidArgument", sessionID, err)
			}
			var recorded bool
			if err := store.DB.QueryRowContext(ctx, "SELECT EXISTS (SELECT 1 FROM session WHERE session_id=$1)", sessionID).Scan(&recorded); err != nil {
				t.Fatal(err)
			}
			if recorded {
				t.Fatalf("CreateSession(%q) created a durable opening", sessionID)
			}
		})
	}
}

type deletedDuringLookup struct {
	snapshot SandboxSnapshot
	cleanup  func()
}

func (s deletedDuringLookup) GetSandbox(context.Context, string) (SandboxSnapshot, bool, error) {
	// The inventory read preceded cleanup, but its caller resumes afterward.
	s.cleanup()
	return s.snapshot, true, nil
}

func (deletedDuringLookup) GetExecutionIdentity(context.Context, SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return nil, true, nil
}

func (deletedDuringLookup) GetExecutionAccount(context.Context, SandboxSnapshot) (*ServiceAccountIdentity, bool, error) {
	return nil, false, nil
}

func TestSandboxCleanupFencesStaleAdmissionAcrossReplicas(t *testing.T) {
	for _, existing := range []bool{false, true} {
		t.Run(fmt.Sprintf("existing=%t", existing), func(t *testing.T) {
			store, databaseURL := testStoreWithURL(t, 3)
			ctx := admitted(t)
			uid, sessionID := uuid.NewString(), uuid.NewString()
			if existing {
				// Keep existing work distinct from this fresh admission. An exact
				// retained retry does not consult inventory or trigger its cleanup hook.
				if _, err := store.CreateSession(ctx, uuid.NewString(), "sandbox-api", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
					t.Fatal(err)
				}
			}
			// Independent connections and Store instances represent another replica or a restart.
			db, err := sql.Open("postgres", databaseURL)
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			restarted := NewStore(db, 3)
			service := &Service{
				Store: restarted, DispatcherCallers: configuredDispatcherCallers(),
				Sandboxes: deletedDuringLookup{
					snapshot: SandboxSnapshot{Name: "sandbox-api", Namespace: caller.Namespace, UID: uid},
					cleanup: func() {
						if _, err := store.EndSandbox(ctx, uid); err != nil {
							t.Fatal(err)
						}
					},
				},
			}
			_, err = service.CreateSession(ctx, &dispatcherpb.CreateSessionRequest{SessionId: sessionID, Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox-api", Uid: uid}, Harness: sessionpb.Harness_HARNESS_CODEX}}}}})
			if status.Code(err) != codes.FailedPrecondition {
				t.Fatalf("opening after cleanup: %v", err)
			}
			_, err = service.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
				CommandId: uuid.NewString(), SessionId: sessionID, ExpectedSandboxUid: uid, Text: "hello",
			})
			if status.Code(err) != codes.NotFound {
				t.Fatalf("privileged command after cleanup: %v", err)
			}
			if owner, err := restarted.ClaimSandboxOwner(ctx, uid, "stale-lease", 1, "stale-replica", 0); !errors.Is(err, ErrSandboxDeleted) || owner != nil {
				t.Fatalf("ownership after cleanup: %+v %v", owner, err)
			}
			if _, err := restarted.GetSession(ctx, sessionID); !errors.Is(err, ErrNotFound) {
				t.Fatalf("opening resurrected: %v", err)
			}
			if existing {
				retained, err := restarted.ListSessionsForCaller(ctx, "", 10, CallerIdentity{Privileged: true})
				if err != nil || len(retained) != 1 || retained[0].EnvironmentDeletedAt == nil || retained[0].State != SessionFailedState {
					t.Fatalf("Session lost during cleanup: %+v %v", retained, err)
				}
			}
			if uids, err := restarted.ListSandboxUIDs(ctx); err != nil || len(uids) != 0 {
				t.Fatalf("live queue/ownership state resurrected: %v %v", uids, err)
			}
			// There is no UUID tombstone. This deliberately reuses the ID to test the
			// limit of the caller contract; retained History may reject the new writer.
			if _, err := restarted.CreateSession(ctx, sessionID, "sandbox-api", uuid.NewString(), &sessionpb.SessionSpec{}, nil, nil); err != nil {
				t.Fatalf("forgotten Session ID unexpectedly retained after cleanup: %v", err)
			}
		})
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
		_, err := openingStore.CreateSession(ctx, sessionID, "sandbox-race", uid, &sessionpb.SessionSpec{}, nil, nil)
		opened <- err
	}()
	waitBlocked := func(waiter, blocker int) {
		t.Helper()
		ticker := time.NewTicker(10 * time.Millisecond)
		defer ticker.Stop()
		for {
			var blocked bool
			if err := store.DB.QueryRowContext(ctx, "SELECT $2 = ANY(pg_blocking_pids($1))", waiter, blocker).Scan(&blocked); err != nil {
				t.Fatal(err)
			}
			if blocked {
				return
			}
			select {
			case <-ctx.Done():
				t.Fatal(ctx.Err())
			case <-ticker.C:
			}
		}
	}
	waitBlocked(openingPID, gatePID)
	cleaned := make(chan error, 1)
	go func() {
		_, err := cleanupStore.EndSandbox(ctx, uid)
		cleaned <- err
	}()
	// Cleanup must wait for the transaction whose opening has not yet been inserted.
	waitBlocked(cleanupPID, openingPID)
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

type deletionCheckCatalog struct {
	growingCatalog
	afterSnapshot func()
	currentError  error
}

func (c *deletionCheckCatalog) ListSandboxes(context.Context) ([]SandboxSnapshot, error) {
	before := slices.Clone(c.sandboxes)
	c.afterSnapshot()
	return before, nil
}

func (c *deletionCheckCatalog) CurrentSandboxUIDs(ctx context.Context) (map[string]struct{}, error) {
	if c.currentError != nil {
		return nil, c.currentError
	}
	return c.growingCatalog.CurrentSandboxUIDs(ctx)
}

func TestCleanupConfirmsAbsenceAfterReadingStoredWork(t *testing.T) {
	for _, currentState := range []string{"live", "absent", "unavailable"} {
		t.Run(currentState, func(t *testing.T) {
			store := testStore(t, 3)
			ctx := t.Context()
			uid, sessionID := uuid.NewString(), uuid.NewString()
			catalog := &deletionCheckCatalog{}
			catalog.afterSnapshot = func() {
				// A live environment and its opening appear after the worker's first
				// inventory snapshot, before it enumerates the database's stored UIDs.
				if currentState == "live" {
					catalog.sandboxes = []SandboxSnapshot{{Name: "new-sandbox", UID: uid}}
				}
				if _, err := store.CreateSession(ctx, sessionID, "new-sandbox", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
					t.Fatal(err)
				}
			}
			if currentState == "unavailable" {
				catalog.currentError = errors.New("inventory unavailable")
			}
			worker := &DeliveryWorker{Store: store, Catalog: catalog}
			worker.init()
			defer worker.queue.ShutDown()
			err := worker.reconcile(ctx, false)
			if !errors.Is(err, catalog.currentError) {
				t.Fatalf("reconcile: got %v, want %v", err, catalog.currentError)
			}
			if currentState == "absent" {
				row, err := store.GetSession(ctx, sessionID)
				if err != nil || row.EnvironmentDeletedAt == nil || row.State != SessionFailedState {
					t.Fatalf("absent environment Session not retained and settled: %+v %v", row, err)
				}
				if _, err := store.CreateSession(ctx, uuid.NewString(), "new-sandbox", uid, &sessionpb.SessionSpec{}, nil, nil); !errors.Is(err, ErrSandboxDeleted) {
					t.Fatalf("absent environment not fenced: %v", err)
				}
				return
			}
			if _, err := store.GetSession(ctx, sessionID); err != nil {
				t.Fatalf("live opening removed: %v", err)
			}
			if _, err := store.CreateSession(ctx, uuid.NewString(), "new-sandbox", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
				t.Fatalf("live environment admission fenced: %v", err)
			}
		})
	}
}

type openingOutcomeRunner struct {
	acceptingRunner
	err error
}

func (r *openingOutcomeRunner) CreateSession(_ context.Context, _ SandboxSnapshot, id string, _ *sessionpb.SessionSpec, _ *string, _ SessionExecution) error {
	r.calls <- "opening:" + id
	return r.err
}

func TestNativeSessionOutcomesFenceQueuedWork(t *testing.T) {
	cases := []struct {
		name         string
		code         codes.Code
		openingState string
		commandState string
	}{
		{"invalid request", codes.InvalidArgument, SessionFailedState, FailedState},
		{"conflicting request", codes.AlreadyExists, SessionFailedState, FailedState},
		{"unknown response", codes.Unknown, SessionUnknownState, QueueState},
		{"internal response", codes.Internal, SessionUnknownState, QueueState},
		{"resource exhaustion", codes.ResourceExhausted, SessionUnknownState, QueueState},
		{"unproven precondition", codes.FailedPrecondition, SessionUnknownState, QueueState},
		{"unavailable reservation", codes.Unavailable, SessionWaitingState, QueueState},
		{"lost reservation response", codes.DeadlineExceeded, SessionWaitingState, QueueState},
		{"admitted", codes.OK, SessionAcceptedState, AdmittedState},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			ctx := t.Context()
			store := testStore(t, 3)
			sessionID, uid, commandID := uuid.NewString(), uuid.NewString(), uuid.NewString()
			sandbox := SandboxSnapshot{Name: "test-materialization", Namespace: "test-namespace", UID: uid, Incarnation: "test-incarnation"}
			spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: uid}, Harness: sessionpb.Harness_HARNESS_CODEX}}}}
			if _, err := store.CreateSession(ctx, sessionID, sandbox.Name, uid, spec, nil, nil); err != nil {
				t.Fatal(err)
			}
			command := input(commandID, "queued before owner response")
			if _, err := enqueueWithSession(t, store, ctx, sessionID, sandbox.Name, uid, command, nil); err != nil {
				t.Fatal(err)
			}
			runner := &openingOutcomeRunner{acceptingRunner: acceptingRunner{calls: make(chan string, 3)}, err: status.Error(test.code, "test owner response")}
			worker := &DeliveryWorker{Store: store, Catalog: testCatalog{sandbox: sandbox}, Leases: testLease{store: store}, Runner: runner}
			worker.init()
			defer worker.queue.ShutDown()
			if err := worker.reconcile(ctx, true); err != nil {
				t.Fatal(err)
			}
			if err := worker.processSession(ctx, sessionID); err != nil {
				t.Fatal(err)
			}
			if len(runner.calls) != 1 || <-runner.calls != "opening:"+sessionID {
				t.Fatal("opening did not reach owner exactly once")
			}
			if retry, err := store.CreateSession(ctx, sessionID, sandbox.Name, uid, spec, nil, nil); err != nil || retry.State != test.openingState {
				t.Fatalf("opening receipt retry: %+v %v", retry, err)
			} else if test.openingState == SessionUnknownState && (!strings.HasPrefix(retry.Reason, "session_materialization_unconfirmed: ") || !strings.Contains(retry.Reason, "test owner response")) {
				t.Fatalf("uncertain Session reason lost owner evidence: %q", retry.Reason)
			}
			if test.openingState == FailedState || test.openingState == UnknownState {
				if _, err := enqueueWithSession(t, store, ctx, sessionID, sandbox.Name, uid, input(uuid.NewString(), "after owner response"), nil); !errors.Is(err, ErrSessionUnconfirmed) || status.Code(apiError(err)) != codes.FailedPrecondition {
					t.Fatalf("new command accepted after terminal opening: %v", err)
				}
				owner, ok := worker.ownerFor(uid)
				if !ok {
					t.Fatal("worker lost owner")
				}
				if _, err := store.RecoverSandbox(ctx, owner); err != nil {
					t.Fatal(err)
				}
				// A later suspended environment must not cause wake or redelivery.
				sandbox.Blocker, sandbox.WakeOnSend = "sandbox_suspended", true
				worker.Catalog = testCatalog{sandbox: sandbox}
			}
			wake := &scriptedWake{}
			worker.Wake = wake
			if err := worker.processSession(ctx, sessionID); err != nil {
				t.Fatal(err)
			}
			row, err := store.GetDispatch(ctx, commandID)
			if err != nil || row.State != test.commandState {
				t.Fatalf("dependent command: %+v %v", row, err)
			}
			if retry, err := enqueueWithSession(t, store, ctx, sessionID, sandbox.Name, uid, command, nil); err != nil || retry.State != row.State || retry.Position != row.Position {
				t.Fatalf("existing command receipt retry: %+v %v", retry, err)
			}
			if test.commandState == FailedState && row.Reason != "session_rejected" {
				t.Fatalf("unsent rejection reason: %s", row.Reason)
			}
			if test.openingState == UnknownState && row.Reason != "session_unconfirmed" {
				t.Fatalf("uncertain opening blocker: %s", row.Reason)
			}
			if len(wake.targets) != 0 {
				t.Fatal("unconfirmed opening attempted wake")
			}
			if test.commandState == AdmittedState {
				if len(runner.calls) != 1 || <-runner.calls != commandID || row.Admission == nil {
					t.Fatal("admitted opening did not release its command")
				}
			} else if len(runner.calls) != 0 || row.Admission != nil {
				t.Fatal("unconfirmed opening delivered or replayed work")
			}
		})
	}
}

func TestUnprovenSessionFailureHoldsCommandsAndPreservesOtherPaths(t *testing.T) {
	for _, kind := range []string{"native", "central"} {
		t.Run(kind, func(t *testing.T) {
			ctx := t.Context()
			store := testStore(t, 3)
			sessionID, uid, commandID := uuid.NewString(), uuid.NewString(), uuid.NewString()
			sandbox := SandboxSnapshot{Name: "test-unproven", Namespace: "test-namespace", UID: uid, Incarnation: "test-incarnation"}
			spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: uid}, Harness: sessionpb.Harness_HARNESS_CODEX}}}}
			if kind == "central" {
				spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: uid}}}}
			}
			if _, err := store.CreateSession(ctx, sessionID, sandbox.Name, uid, spec, nil, nil); err != nil {
				t.Fatal(err)
			}
			if _, err := enqueueWithSession(t, store, ctx, sessionID, sandbox.Name, uid, input(commandID, "pending"), nil); err != nil {
				t.Fatal(err)
			}
			runner := &acceptingRunner{calls: make(chan string, 1)}
			worker := &DeliveryWorker{Store: store, Catalog: testCatalog{sandbox: sandbox}, Leases: testLease{store: store}, Runner: runner}
			worker.init()
			defer worker.queue.ShutDown()
			if err := worker.reconcile(ctx, true); err != nil {
				t.Fatal(err)
			}
			owner, ok := worker.ownerFor(uid)
			if !ok {
				t.Fatal("worker has no owner")
			}
			if _, err := store.SettleSession(ctx, sessionID, owner, []string{SessionWaitingState}, SessionFailedState, "old unproven outcome"); err != nil {
				t.Fatal(err)
			}
			if err := worker.processSession(ctx, sessionID); err != nil {
				t.Fatal(err)
			}
			row, err := store.GetDispatch(ctx, commandID)
			if err != nil {
				t.Fatal(err)
			}
			if kind == "native" {
				if row.State != QueueState || row.Reason != "session_unconfirmed" || len(runner.calls) != 0 {
					t.Fatalf("unproven failure did not hold work: %+v", row)
				}
				if _, err := store.Cancel(ctx, commandID); err != nil {
					t.Fatalf("held command cannot be cancelled: %v", err)
				}
			} else if row.State != AdmittedState || len(runner.calls) != 1 || <-runner.calls != commandID {
				t.Fatalf("existing %s delivery behavior changed: %+v", kind, row)
			}
		})
	}
}

func TestSessionRejectionSerializesWithAdmissionAndFencesOwners(t *testing.T) {
	store, databaseURL := testStoreWithURL(t, 3)
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	sessionID, uid, commandID := uuid.NewString(), uuid.NewString(), uuid.NewString()
	if _, err := store.CreateSession(ctx, sessionID, "test-rejection-race", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	oldOwner, err := store.ClaimSandboxOwner(ctx, uid, "test-old-lease", 1, "test-old-owner", 0)
	if err != nil || oldOwner == nil {
		t.Fatalf("old owner: %+v %v", oldOwner, err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, uid, "test-new-lease", 1, "test-new-owner", oldOwner.Generation)
	if err != nil || owner == nil {
		t.Fatalf("new owner: %+v %v", owner, err)
	}
	if row, err := store.RejectSession(ctx, sessionID, *oldOwner, []string{SessionWaitingState}, "stale rejection"); err != nil || row != nil {
		t.Fatalf("stale owner settled opening: %+v %v", row, err)
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
	waitBlocked := func(waiter, blocker int) {
		t.Helper()
		ticker := time.NewTicker(10 * time.Millisecond)
		defer ticker.Stop()
		for {
			var blocked bool
			if err := store.DB.QueryRowContext(ctx, "SELECT $2 = ANY(pg_blocking_pids($1))", waiter, blocker).Scan(&blocked); err != nil {
				t.Fatal(err)
			}
			if blocked {
				return
			}
			select {
			case <-ctx.Done():
				t.Fatal(ctx.Err())
			case <-ticker.C:
			}
		}
	}
	waitBlocked(admitterPID, gatePID)
	rejected := make(chan error, 1)
	go func() {
		_, err := rejecter.RejectSession(ctx, sessionID, *owner, []string{SessionWaitingState}, "proven rejection")
		rejected <- err
	}()
	// Admission has the Session lock but has not inserted the command yet.
	waitBlocked(rejecterPID, admitterPID)
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
	if row, err := store.RejectSession(ctx, sessionID, *owner, []string{FailedState}, "late rejection"); err != nil || row != nil {
		t.Fatalf("rejection resurrected cleaned state: %+v %v", row, err)
	}
}

func TestEveryCommandAdmissionRequiresSession(t *testing.T) {
	ctx := admitted(t)
	store := testStore(t, 3)
	service := &Service{Store: store, DispatcherCallers: configuredDispatcherCallers(), Sandboxes: oneSandbox{}}
	for _, invoke := range []func() error{
		func() error {
			_, err := service.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{CommandId: "missing-input", SessionId: "missing", ExpectedSandboxUid: sandboxUID, Text: "input"})
			return err
		},
		func() error {
			_, err := service.ChangeModel(ctx, &dispatcherpb.ChangeModelRequest{CommandId: "missing-model", SessionId: "missing", ExpectedSandboxUid: sandboxUID, Model: "model"})
			return err
		},
		func() error {
			_, err := service.ChangeReasoningEffort(ctx, &dispatcherpb.ChangeReasoningEffortRequest{CommandId: "missing-effort", SessionId: "missing", ExpectedSandboxUid: sandboxUID, Effort: "high"})
			return err
		},
		func() error {
			_, err := service.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{CommandId: "missing-interrupt", SessionId: "missing", ExpectedSandboxUid: sandboxUID, TurnId: "turn"})
			return err
		},
	} {
		if err := invoke(); status.Code(err) != codes.NotFound {
			t.Fatalf("missing Session command: %v", err)
		}
	}
	for _, identity := range []*CallerIdentity{nil, {Privileged: true}, {ServiceAccountIdentity: serviceAccountIdentityFor(Account(caller), "test-account-uid")}} {
		if _, err := store.enqueue(ctx, "missing", "sandbox-api", sandboxUID, input(uuid.NewString(), "input"), nil, identity); !errors.Is(err, ErrNotFound) {
			t.Fatalf("store missing Session: %v", err)
		}
	}
	if rows, err := store.ListDispatches(ctx, "missing", "", nil); err != nil || len(rows) != 0 {
		t.Fatalf("missing Session left work: %+v %v", rows, err)
	}
}

func TestPrivilegedCommandsCannotCrossSessionPlacement(t *testing.T) {
	ctx := admitted(t)
	store := testStore(t, 3)
	sessionID := uuid.NewString()
	if _, err := store.CreateSession(ctx, sessionID, "different-sandbox", uuid.NewString(), &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	service := &Service{Store: store, DispatcherCallers: configuredDispatcherCallers(), Sandboxes: oneSandbox{}}
	for _, invoke := range []func() error{
		func() error {
			_, err := service.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{CommandId: uuid.NewString(), SessionId: sessionID, ExpectedSandboxUid: sandboxUID, Text: "input"})
			return err
		},
		func() error {
			_, err := service.ChangeModel(ctx, &dispatcherpb.ChangeModelRequest{CommandId: uuid.NewString(), SessionId: sessionID, ExpectedSandboxUid: sandboxUID, Model: "model"})
			return err
		},
		func() error {
			_, err := service.ChangeReasoningEffort(ctx, &dispatcherpb.ChangeReasoningEffortRequest{CommandId: uuid.NewString(), SessionId: sessionID, ExpectedSandboxUid: sandboxUID, Effort: "high"})
			return err
		},
		func() error {
			_, err := service.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{CommandId: uuid.NewString(), SessionId: sessionID, ExpectedSandboxUid: sandboxUID, TurnId: "turn"})
			return err
		},
	} {
		if err := invoke(); status.Code(err) != codes.PermissionDenied {
			t.Fatalf("cross-placement command: %v", err)
		}
	}
	if rows, err := store.ListDispatches(ctx, sessionID, "", nil); err != nil || len(rows) != 0 {
		t.Fatalf("cross-placement command left work: %+v %v", rows, err)
	}
}

func TestCommandAdmissionRechecksSessionAfterConcurrentDeletion(t *testing.T) {
	for _, privileged := range []bool{false, true} {
		t.Run(fmt.Sprint(privileged), func(t *testing.T) {
			store, databaseURL := testStoreWithURL(t, 3)
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			identity := serviceAccountIdentityFor(Account(caller), "test-owner-uid")
			admission := CallerIdentity{Privileged: privileged, ServiceAccountIdentity: identity, Attribution: &AdmissionAttribution{Actor: identity, EffectiveCaller: identity}}
			sessionID := uuid.NewString()
			if _, err := store.CreateSessionForCaller(ctx, sessionID, "test-deletion", sandboxUID, &sessionpb.SessionSpec{}, nil, nil, identity, admission); err != nil {
				t.Fatal(err)
			}
			// Simulate the API's successful read before the concurrent delete.
			if _, err := store.GetSessionForCaller(ctx, sessionID, admission); err != nil {
				t.Fatal(err)
			}
			deletion, err := store.DB.BeginTx(ctx, nil)
			if err != nil {
				t.Fatal(err)
			}
			defer deletion.Rollback()
			if _, err := deletion.ExecContext(ctx, "DELETE FROM session WHERE session_id=$1", sessionID); err != nil {
				t.Fatal(err)
			}
			var deletionPID int
			if err := deletion.QueryRowContext(ctx, "SELECT pg_backend_pid()").Scan(&deletionPID); err != nil {
				t.Fatal(err)
			}
			db, err := sql.Open("postgres", databaseURL)
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			db.SetMaxOpenConns(1)
			var admissionPID int
			if err := db.QueryRowContext(ctx, "SELECT pg_backend_pid()").Scan(&admissionPID); err != nil {
				t.Fatal(err)
			}
			result := make(chan error, 1)
			go func() {
				_, err := NewStore(db, 3).EnqueueForCaller(ctx, sessionID, "test-deletion", sandboxUID, input(uuid.NewString(), "input"), nil, admission)
				result <- err
			}()
			ticker := time.NewTicker(10 * time.Millisecond)
			defer ticker.Stop()
			for {
				var blocked bool
				if err := store.DB.QueryRowContext(ctx, "SELECT $2 = ANY(pg_blocking_pids($1))", admissionPID, deletionPID).Scan(&blocked); err != nil {
					t.Fatal(err)
				}
				if blocked {
					break
				}
				select {
				case <-ctx.Done():
					t.Fatal(ctx.Err())
				case <-ticker.C:
				}
			}
			if err := deletion.Commit(); err != nil {
				t.Fatal(err)
			}
			if err := <-result; !errors.Is(err, ErrNotFound) {
				t.Fatalf("deleted Session admitted command: %v", err)
			}
			if rows, err := store.ListDispatches(ctx, sessionID, "", nil); err != nil || len(rows) != 0 {
				t.Fatalf("deleted Session left work: %+v %v", rows, err)
			}
		})
	}
}

type deletingSessionInventory struct {
	oneSandbox
	store     *Store
	sessionID string
}

func (deletingSessionInventory) GetExecutionIdentity(context.Context, SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return nil, true, nil
}

func (i deletingSessionInventory) GetExecutionAccount(ctx context.Context, _ SandboxSnapshot) (*ServiceAccountIdentity, bool, error) {
	_, err := i.store.DB.ExecContext(ctx, "DELETE FROM session WHERE session_id=$1", i.sessionID)
	return serviceAccountIdentityFor(Account(caller), "test-account-uid"), true, err
}

func TestInterruptRechecksSessionAfterLiveAuthorization(t *testing.T) {
	ctx := admitted(t)
	store := testStore(t, 3)
	identity := serviceAccountIdentityFor(Account(caller), "test-account-uid")
	admission := CallerIdentity{ServiceAccountIdentity: identity, Attribution: &AdmissionAttribution{Actor: identity, EffectiveCaller: identity}}
	sessionID := uuid.NewString()
	if _, err := store.CreateSessionForCaller(ctx, sessionID, "sandbox-api", sandboxUID, &sessionpb.SessionSpec{}, nil, nil, identity, admission); err != nil {
		t.Fatal(err)
	}
	service := &Service{Store: store, Sandboxes: deletingSessionInventory{store: store, sessionID: sessionID}}
	if _, err := service.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{SessionId: sessionID, ExpectedSandboxUid: sandboxUID, CommandId: "deleted-interrupt", TurnId: "turn"}); status.Code(err) != codes.NotFound {
		t.Fatalf("deleted Session interrupt: %v", err)
	}
}

func TestQueuedSessionAllowsNotificationWakeWithoutBorrowingCaller(t *testing.T) {
	for _, attributedSession := range []bool{false, true} {
		t.Run(fmt.Sprint(attributedSession), func(t *testing.T) {
			ctx := t.Context()
			wake := &scriptedWake{}
			fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{}, wake)
			if attributedSession {
				wake.assessments = append(wake.assessments, &environmentpb.WakeAssessment{State: environmentpb.WakeState_WAKE_STATE_DENIED, Reason: environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED})
			} else {
				if _, err := fixture.store.DB.ExecContext(ctx, "UPDATE session SET creator_account_namespace=NULL,creator_account_name=NULL,creator_account_uid=NULL,effective_caller_account_namespace=NULL,effective_caller_account_name=NULL,effective_caller_account_uid=NULL WHERE session_id=$1", fixture.opening.SessionID); err != nil {
					t.Fatal(err)
				}
			}
			notification := serviceAccountIdentityFor(Account{Namespace: "test-notifications", Name: "notification-service"}, "notification-uid")
			if _, err := fixture.store.DB.ExecContext(ctx, "UPDATE dispatch SET actor_account_namespace=$2,actor_account_name=$3,actor_account_uid=$4,effective_caller_account_namespace=$2,effective_caller_account_name=$3,effective_caller_account_uid=$4 WHERE command_id=$1", fixture.dispatch.CommandID, notification.GetAccount().GetNamespace(), notification.GetAccount().GetName(), notification.GetUid()); err != nil {
				t.Fatal(err)
			}
			wake.assessments = append(wake.assessments, &environmentpb.WakeAssessment{State: environmentpb.WakeState_WAKE_STATE_REQUESTED, ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(fixture.sandbox.WakeGeneration + 1)})
			catalog := &executionAccountCatalog{growingCatalog: &growingCatalog{sandboxes: []SandboxSnapshot{fixture.sandbox}}, accountUID: fixture.caller.GetUid()}
			runner := &acceptingRunner{calls: make(chan string, 2)}
			worker := &DeliveryWorker{Store: fixture.store, Wake: wake, Catalog: catalog, Leases: testLease{store: fixture.store}, Runner: runner}
			worker.init()
			defer worker.queue.ShutDown()
			if err := worker.reconcile(ctx, true); err != nil {
				t.Fatal(err)
			}
			if err := worker.processSession(ctx, fixture.opening.SessionID); err != nil {
				t.Fatal(err)
			}
			if len(wake.callers) == 0 || !sameServiceAccountIdentity(wake.callers[len(wake.callers)-1], notification) {
				t.Fatalf("downstream wake used wrong caller: %+v", wake.callers)
			}
			row, err := fixture.store.GetDispatch(ctx, fixture.dispatch.CommandID)
			if err != nil || row.State != QueueState || row.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_REQUESTED || len(runner.calls) != 0 {
				t.Fatalf("wake changed delivery ordering: %+v %v", row, err)
			}
			catalog.growingCatalog.sandboxes[0].Blocker = ""
			for range 2 {
				if err := worker.processSession(ctx, fixture.opening.SessionID); err != nil {
					t.Fatal(err)
				}
			}
			if len(runner.calls) != 2 || <-runner.calls != "opening:"+fixture.opening.SessionID || <-runner.calls != fixture.dispatch.CommandID {
				t.Fatal("command did not follow opening admission")
			}
		})
	}
}

func TestSessionWakeCannotBypassCancelledCommandAssessmentAfterRestart(t *testing.T) {
	for _, state := range []environmentpb.WakeState{environmentpb.WakeState_WAKE_STATE_REQUESTED, environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN, environmentpb.WakeState_WAKE_STATE_RATE_LIMITED} {
		t.Run(state.String(), func(t *testing.T) {
			ctx := t.Context()
			wake := &scriptedWake{}
			fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{}, wake)
			assessment := &environmentpb.WakeAssessment{State: state, TargetGeneration: proto.Int64(fixture.sandbox.WakeGeneration), ObservedAt: timestamppb.Now(), RetryAt: timestamppb.New(time.Now().Add(time.Hour))}
			if queued, err := fixture.store.NoteWaking(ctx, fixture.dispatch.CommandID, fixture.owner, nil, false, assessment); err != nil || !queued {
				t.Fatalf("record wake ownership: %v %v", queued, err)
			}
			if err := fixture.store.SetDeliveryAssessment(ctx, fixture.dispatch.CommandID, fixture.owner, false, nil, assessment); err != nil {
				t.Fatal(err)
			}
			if _, err := fixture.store.Cancel(ctx, fixture.dispatch.CommandID); err != nil {
				t.Fatal(err)
			}
			if _, err := fixture.store.EnqueueForCaller(ctx, fixture.opening.SessionID, fixture.sandbox.Name, fixture.sandbox.UID, input(uuid.NewString(), "later authorized notice"), nil, CallerIdentity{ServiceAccountIdentity: fixture.caller, Attribution: fixture.dispatch.Attribution}); err != nil {
				t.Fatal(err)
			}
			for range 2 {
				worker := &DeliveryWorker{Store: NewStore(fixture.store.DB, 3), Wake: wake, Leases: testLease{store: fixture.store}}
				worker.init()
				opening, err := worker.Store.GetSession(ctx, fixture.opening.SessionID)
				if err != nil {
					t.Fatal(err)
				}
				if ready, err := worker.prepareSession(ctx, opening, fixture.sandbox, fixture.owner); err != nil || ready {
					t.Fatalf("pending wake bypassed: %v %v", ready, err)
				}
				worker.queue.ShutDown()
			}
			if len(wake.callers) != 0 {
				t.Fatalf("replayed wake through another caller: %+v", wake.callers)
			}
		})
	}
}

type cancelDuringWake struct {
	store     *Store
	commandID string
	calls     int
}

func (w *cancelDuringWake) RequestWake(ctx context.Context, _ *lifecycle.Target, _ *lifecycle.ServiceAccountIdentity) (*environmentpb.WakeAssessment, error) {
	w.calls++
	if _, err := w.store.Cancel(ctx, w.commandID); err != nil {
		return nil, err
	}
	return nil, status.Error(codes.Unavailable, "uncertain wake result")
}

func TestCancellationDuringWakeRetainsUncertaintyForSession(t *testing.T) {
	ctx := t.Context()
	wake := &cancelDuringWake{}
	fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{}, wake)
	wake.store, wake.commandID = fixture.store, fixture.dispatch.CommandID
	if ready, err := fixture.worker.prepareDispatch(ctx, fixture.dispatch, fixture.sandbox, fixture.owner); err != nil || ready {
		t.Fatalf("cancel during wake: %v %v", ready, err)
	}
	row, err := fixture.store.GetDispatch(ctx, fixture.dispatch.CommandID)
	if err != nil || row.State != CancelledState || row.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN || row.WakeAssessment.GetTargetGeneration() != fixture.sandbox.WakeGeneration {
		t.Fatalf("lost in-flight wake marker: %+v %v", row, err)
	}
	if _, err := fixture.store.EnqueueForCaller(ctx, fixture.opening.SessionID, fixture.sandbox.Name, fixture.sandbox.UID, input(uuid.NewString(), "later notice"), nil, CallerIdentity{ServiceAccountIdentity: fixture.caller, Attribution: fixture.dispatch.Attribution}); err != nil {
		t.Fatal(err)
	}
	worker := &DeliveryWorker{Store: NewStore(fixture.store.DB, 3), Wake: wake, Leases: testLease{store: fixture.store}}
	worker.init()
	defer worker.queue.ShutDown()
	if ready, err := worker.prepareSession(ctx, fixture.opening, fixture.sandbox, fixture.owner); err != nil || ready {
		t.Fatalf("cancelled wake bypassed: %v %v", ready, err)
	}
	if wake.calls != 1 {
		t.Fatalf("uncertain wake replayed %d times", wake.calls)
	}
}

type changingWakeGrant struct {
	cancelDuringWake
	opener  *ServiceAccountIdentity
	allowed bool
	callers []*ServiceAccountIdentity
}

func (w *changingWakeGrant) RequestWake(ctx context.Context, target *lifecycle.Target, caller *lifecycle.ServiceAccountIdentity) (*environmentpb.WakeAssessment, error) {
	w.callers = append(w.callers, caller)
	if sameServiceAccountIdentity(caller, w.opener) {
		if w.allowed {
			return &environmentpb.WakeAssessment{State: environmentpb.WakeState_WAKE_STATE_REQUESTED}, nil
		}
		return &environmentpb.WakeAssessment{State: environmentpb.WakeState_WAKE_STATE_DENIED, Reason: environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED}, nil
	}
	w.allowed = true
	return w.cancelDuringWake.RequestWake(ctx, target, caller)
}

func TestNewSessionWakeGrantCannotBypassCancelledNotificationUncertainty(t *testing.T) {
	ctx := t.Context()
	wake := &changingWakeGrant{}
	fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{}, wake)
	wake.store, wake.commandID, wake.opener = fixture.store, fixture.dispatch.CommandID, fixture.caller
	notification := serviceAccountIdentityFor(Account{Namespace: "test-notifications", Name: "notification-service"}, "notification-uid")
	if _, err := fixture.store.DB.ExecContext(ctx, "UPDATE dispatch SET actor_account_namespace=$2,actor_account_name=$3,actor_account_uid=$4,effective_caller_account_namespace=$2,effective_caller_account_name=$3,effective_caller_account_uid=$4 WHERE command_id=$1", fixture.dispatch.CommandID, notification.GetAccount().GetNamespace(), notification.GetAccount().GetName(), notification.GetUid()); err != nil {
		t.Fatal(err)
	}
	if ready, err := fixture.worker.prepareSession(ctx, fixture.opening, fixture.sandbox, fixture.owner); err != nil || ready {
		t.Fatalf("initial denied opening/notice wake: %v %v", ready, err)
	}
	if len(wake.callers) != 2 || !sameServiceAccountIdentity(wake.callers[0], fixture.caller) || !sameServiceAccountIdentity(wake.callers[1], notification) || !wake.allowed {
		t.Fatalf("wake attribution/grant transition: %+v", wake.callers)
	}
	for range 2 {
		worker := &DeliveryWorker{Store: NewStore(fixture.store.DB, 3), Wake: wake, Leases: testLease{store: fixture.store}}
		worker.init()
		opening, err := worker.Store.GetSession(ctx, fixture.opening.SessionID)
		if err != nil {
			t.Fatal(err)
		}
		if ready, err := worker.prepareSession(ctx, opening, fixture.sandbox, fixture.owner); err != nil || ready {
			t.Fatalf("new opening grant bypassed uncertainty: %v %v", ready, err)
		}
		worker.queue.ShutDown()
	}
	if len(wake.callers) != 2 {
		t.Fatalf("newly authorized opener replayed notice wake: %+v", wake.callers)
	}
	fixture.sandbox.WakeGeneration++
	opening, err := fixture.store.GetSession(ctx, fixture.opening.SessionID)
	if err != nil {
		t.Fatal(err)
	}
	if ready, err := fixture.worker.prepareSession(ctx, opening, fixture.sandbox, fixture.owner); err != nil || ready {
		t.Fatalf("reassess after observed generation change: %v %v", ready, err)
	}
	if len(wake.callers) != 3 || !sameServiceAccountIdentity(wake.callers[2], fixture.caller) {
		t.Fatalf("later generation did not reassess opening caller: %+v", wake.callers)
	}
}

// The external boundary observes the committed marker before returning a lost
// admission reply. Requeue and recovery cannot turn that attempt into unsent work.
type attemptProbeRunner struct {
	*retryRunner
	before func()
}

func (r *attemptProbeRunner) Deliver(ctx context.Context, sandbox SandboxSnapshot, sessionID string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	r.before()
	return r.retryRunner.Deliver(ctx, sandbox, sessionID, command)
}

func TestDeletionAfterLostCommandReplyRemainsUnknown(t *testing.T) {
	ctx := t.Context()
	store := testStore(t, 3)
	head, err := enqueueWithSession(t, store, ctx, "lost-reply-session", "sandbox-worker", sandboxUID, input("worker-input", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, sandboxUID, "lost-reply-lease", 1, "replica-a", 0)
	if err != nil || owner == nil {
		t.Fatalf("owner: %+v %v", owner, err)
	}
	worker := &DeliveryWorker{Store: store, Runner: &attemptProbeRunner{retryRunner: &retryRunner{}, before: func() {
		row, err := store.GetDispatch(ctx, head.CommandID)
		if err != nil || !row.DeliveryAttempted || row.State != DeliveringState {
			t.Fatalf("RPC preceded durable marker: %+v, %v", row, err)
		}
	}}}
	worker.init()
	defer worker.queue.ShutDown()
	if !worker.deliverCommand(ctx, head, SandboxSnapshot{Name: "sandbox-worker", UID: sandboxUID, Incarnation: "pod-1"}, *owner, nil) {
		t.Fatal("worker did not attempt delivery")
	}
	queued, err := store.GetDispatch(ctx, head.CommandID)
	if err != nil || queued.State != QueueState || !queued.DeliveryAttempted {
		t.Fatalf("lost reply requeue: %+v, %v", queued, err)
	}
	if _, err := store.Cancel(ctx, head.CommandID); !errors.Is(err, ErrNotCancellable) {
		t.Fatalf("uncertain attempt cancelled as unsent: %v", err)
	}
	if _, err := store.RecoverSandbox(ctx, *owner); err != nil {
		t.Fatal(err)
	}
	events, err := store.EndSandbox(ctx, sandboxUID)
	if err != nil || len(events) != 1 || events[0].Dispatch.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN {
		t.Fatalf("deletion after lost reply: %+v, %v", events, err)
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
			owner, err := store.ClaimSandboxOwner(ctx, sandboxUID, "fence-order-lease", 1, "replica-a", 0)
			if err != nil || owner == nil {
				t.Fatalf("owner: %+v %v", owner, err)
			}
			if _, err := store.Settle(ctx, head.CommandID, *owner, []string{QueueState}, DeliveringState, "", nil); err != nil {
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
			if err := gate.QueryRowContext(ctx, "SELECT pg_backend_pid() FROM sandbox_ownership WHERE sandbox_uid=$1 FOR UPDATE", sandboxUID).Scan(&gatePID); err != nil {
				t.Fatal(err)
			}
			waitBlocked := func(waiter, blocker int) {
				t.Helper()
				ticker := time.NewTicker(10 * time.Millisecond)
				defer ticker.Stop()
				for {
					var blocked bool
					if err := store.DB.QueryRowContext(ctx, "SELECT $2 = ANY(pg_blocking_pids($1))", waiter, blocker).Scan(&blocked); err != nil {
						t.Fatal(err)
					}
					if blocked {
						return
					}
					select {
					case <-ctx.Done():
						t.Fatal(ctx.Err())
					case <-ticker.C:
					}
				}
			}
			marked := make(chan bool, 1)
			deleted := make(chan []TerminalEvent, 1)
			errors := make(chan error, 2)
			mark := func() {
				value, err := marker.MarkDeliveryAttempt(ctx, head.CommandID, *owner)
				marked <- value
				errors <- err
			}
			deleteEnvironment := func() { events, err := deleter.EndSandbox(ctx, sandboxUID); deleted <- events; errors <- err }
			// Queue the real transactions in both lock orders before opening the gate.
			if markFirst {
				go mark()
				waitBlocked(markerPID, gatePID)
				go deleteEnvironment()
				waitBlocked(deleterPID, markerPID)
			} else {
				go deleteEnvironment()
				waitBlocked(deleterPID, gatePID)
				go mark()
				waitBlocked(markerPID, deleterPID)
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
			if marked, err := store.MarkDeliveryAttempt(ctx, head.CommandID, *owner); err != nil || marked {
				t.Fatalf("deleted worker gained permission to forward: %t, %v", marked, err)
			}
		})
	}
}

func TestUncertainAttemptCannotBecomeKnownFailedOnRetryRefusal(t *testing.T) {
	ctx := t.Context()
	store := testStore(t, 3)
	head, err := enqueueWithSession(t, store, ctx, "retry-refusal-session", "sandbox-worker", sandboxUID, input("retry-refusal-input", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, sandboxUID, "retry-refusal-lease", 1, "replica-a", 0)
	if err != nil || owner == nil {
		t.Fatalf("owner: %+v %v", owner, err)
	}
	if _, err := store.Settle(ctx, head.CommandID, *owner, []string{QueueState}, DeliveringState, "", nil); err != nil {
		t.Fatal(err)
	}
	if marked, err := store.MarkDeliveryAttempt(ctx, head.CommandID, *owner); err != nil || !marked {
		t.Fatalf("marker: %t, %v", marked, err)
	}
	if _, err := store.Settle(ctx, head.CommandID, *owner, []string{DeliveringState}, QueueState, "lost_reply", nil); err != nil {
		t.Fatal(err)
	}
	head, err = store.GetDispatch(ctx, head.CommandID)
	if err != nil {
		t.Fatal(err)
	}
	worker := &DeliveryWorker{Store: store, Runner: &rejectingRunner{err: status.Error(codes.FailedPrecondition, "receiver sealed")}}
	worker.init()
	defer worker.queue.ShutDown()
	if !worker.deliverCommand(ctx, head, SandboxSnapshot{UID: sandboxUID}, *owner, nil) {
		t.Fatal("retry did not run")
	}
	row, err := store.GetDispatch(ctx, head.CommandID)
	if err != nil || row == nil || row.State != UnknownState {
		t.Fatalf("retry refusal overwrote uncertain result: %+v, %v", row, err)
	}
}

func TestFirstCommandAttemptDoesNotInferNonAdmissionFromRPCCode(t *testing.T) {
	for _, test := range []struct {
		code codes.Code
		want string
	}{
		{codes.Canceled, UnknownState},
		{codes.Unknown, UnknownState},
		{codes.Internal, UnknownState},
		{codes.ResourceExhausted, UnknownState},
		{codes.FailedPrecondition, UnknownState},
	} {
		t.Run(test.code.String(), func(t *testing.T) {
			ctx := t.Context()
			store := testStore(t, 3)
			head, err := enqueueWithSession(t, store, ctx, "first-attempt-session", "sandbox-worker", sandboxUID, input("first-attempt-input", "hello"), nil)
			if err != nil {
				t.Fatal(err)
			}
			owner, err := store.ClaimSandboxOwner(ctx, sandboxUID, "first-attempt-lease", 1, "replica-a", 0)
			if err != nil || owner == nil {
				t.Fatalf("owner: %+v %v", owner, err)
			}
			// The fake external owner commits before returning an error code.
			runner := &rejectingRunner{err: status.Error(test.code, "response unavailable"), admitted: map[string]bool{}}
			worker := &DeliveryWorker{Store: store, Runner: runner}
			worker.init()
			defer worker.queue.ShutDown()
			if !worker.deliverCommand(ctx, head, SandboxSnapshot{UID: sandboxUID}, *owner, nil) {
				t.Fatal("worker did not attempt delivery")
			}
			if !runner.admitted[head.CommandID] {
				t.Fatal("external admission was not exercised")
			}
			row, err := store.GetDispatch(ctx, head.CommandID)
			if err != nil || row.State != test.want {
				t.Fatalf("first attempt result: %+v, %v; want %s", row, err, test.want)
			}
		})
	}
}

func TestDeletedAdmittedInputKeepsKnownAdmissionInUnknownEffectSnapshot(t *testing.T) {
	ctx := t.Context()
	store := testStore(t, 3)
	head, err := enqueueWithSession(t, store, ctx, "admitted-effect-session", "sandbox-worker", sandboxUID, input("admitted-effect-input", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, sandboxUID, "admitted-effect-lease", 1, "replica-a", 0)
	if err != nil || owner == nil {
		t.Fatalf("owner: %+v %v", owner, err)
	}
	admission := &eventlogpb.EventEntry{Cursor: 17}
	if _, err := store.Settle(ctx, head.CommandID, *owner, []string{QueueState}, AdmittedState, "", admission); err != nil {
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
