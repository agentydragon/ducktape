package dispatcher

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	_ "github.com/lib/pq"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/util/workqueue"
)

const sandboxUID = "00000000-0000-4000-8000-000000000001"

const testAudience = "test-dispatcher"

var (
	api    *kubeapiserver.Server
	caller = auth.Account{Namespace: "test-callers", Name: "test-app"}
	// callerToken proves caller to the Service's resolver.
	callerToken string
)

func useAPIServer(server *kubeapiserver.Server) (err error) {
	api = server
	callerToken, err = server.WorkloadToken(context.Background(), caller, testAudience)
	return err
}

func callerResolver(t *testing.T) *auth.Resolver {
	t.Helper()
	resolver, err := auth.NewResolver(api.TokenReviews(), testAudience, []string{caller.Namespace})
	if err != nil {
		t.Fatal(err)
	}
	return resolver
}

type oneSandbox struct{}

func (oneSandbox) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	if name != "sandbox-api" {
		return SandboxSnapshot{}, false, nil
	}
	return SandboxSnapshot{Name: name, UID: sandboxUID}, true, nil
}

type blockedSandbox struct{}

func (blockedSandbox) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	if name != "sandbox-api" {
		return SandboxSnapshot{}, false, nil
	}
	return SandboxSnapshot{Name: name, UID: sandboxUID, Blocker: "sandbox_suspended"}, true, nil
}

type rejectingRunner struct {
	command *commandpb.Command
	err     error
}

func (r *rejectingRunner) Deliver(_ context.Context, _ SandboxSnapshot, _ string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	r.command = command
	return nil, r.err
}

func testStore(t *testing.T, cap int) *Store {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return NewStore(db, cap)
}

func input(id, value string) *commandpb.Command {
	return &commandpb.Command{CommandId: id, Operation: &commandpb.Command_SubmitInput{
		SubmitInput: &commandpb.SubmitInput{Text: value},
	}}
}

func TestAdmissionIsDurableAndIdempotent(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	first, err := store.Enqueue(ctx, "session-a", "sandbox-a", sandboxUID, input("a", "hello"), &Account{"agentplane", "app"})
	if err != nil {
		t.Fatal(err)
	}
	if first.State != QueueState || !first.WakeEligible || first.WakeCaller == nil {
		t.Fatalf("unexpected accepted row: %+v", first)
	}
	// A different store instance models a retry after a lost reply/restart.
	restarted := NewStore(store.DB, 3)
	again, err := restarted.Enqueue(ctx, "session-a", "sandbox-a", sandboxUID, input("a", "hello"), nil)
	if err != nil || again.Position != first.Position {
		t.Fatalf("retry: row=%+v err=%v", again, err)
	}
	if _, err := restarted.Enqueue(ctx, "session-a", "sandbox-a", sandboxUID, input("a", "other"), nil); !errors.Is(err, ErrCommandConflict) {
		t.Fatalf("payload conflict: %v", err)
	}
	for i := 1; i < 3; i++ {
		if _, err := store.Enqueue(ctx, "session-a", "sandbox-a", sandboxUID, input(fmt.Sprintf("input-%d", i), "x"), nil); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := store.Enqueue(ctx, "session-a", "sandbox-a", sandboxUID, input("overflow", "x"), nil); !errors.Is(err, ErrQueueFull) {
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

func TestOpeningAndOwnershipFence(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	spec := &runnerpb.SessionSpec{}
	opened, err := store.OpenSession(ctx, "session-b", "sandbox-b", sandboxUID, spec, nil, nil)
	if err != nil || opened.State != QueueState {
		t.Fatalf("open: %+v %v", opened, err)
	}
	again, err := store.OpenSession(ctx, "session-b", "sandbox-b", sandboxUID, spec, nil, nil)
	if err != nil || again.CreatedAt != opened.CreatedAt {
		t.Fatalf("opening retry: %+v %v", again, err)
	}
	if _, err := store.OpenSession(ctx, "session-b", "sandbox-b", "00000000-0000-4000-8000-000000000002", spec, nil, nil); !errors.Is(err, ErrOpeningConflict) {
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
	if row, err := store.SettleOpening(ctx, "session-b", *owner, []string{QueueState}, DeliveringState, ""); err != nil || row != nil {
		t.Fatalf("old owner settled: %+v %v", row, err)
	}
	if row, err := store.SettleOpening(ctx, "session-b", *successor, []string{QueueState}, DeliveringState, ""); err != nil || row == nil {
		t.Fatalf("new owner could not settle: %+v %v", row, err)
	}
	if ok, err := store.RecoverSandbox(ctx, *successor); err != nil || !ok {
		t.Fatalf("recovery: %v %v", ok, err)
	}
	if row, err := store.GetOpening(ctx, "session-b"); err != nil || row.State != QueueState || row.Reason != "dispatcher_restarted" {
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
			_, err := store.Enqueue(ctx, "session-c", "sandbox-c", sandboxUID, input(fmt.Sprintf("c-%d", i), "x"), nil)
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
	caller := Account{"agentplane", "app"}
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
}

func TestOnlyRunnerAdmissionRecordsAdmission(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000085"
	if _, err := store.Enqueue(ctx, "admission-session", "sandbox-admission", uid, input("admission-input", "hello"), nil); err != nil {
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
	store := testStore(t, 3)
	uid := "00000000-0000-4000-8000-000000000099"
	if _, err := store.Enqueue(ctx, "session-deleted", "sandbox-deleted", uid, input("deleted-queued", "q"), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Enqueue(ctx, "session-deleted", "sandbox-deleted", uid, input("deleted-delivering", "d"), nil); err != nil {
		t.Fatal(err)
	}
	owner, err := store.ClaimSandboxOwner(ctx, uid, "lease-deleted", 1, "replica-a", 0)
	if err != nil || owner == nil {
		t.Fatalf("owner: %+v %v", owner, err)
	}
	if row, err := store.Settle(ctx, "deleted-delivering", *owner, []string{QueueState}, DeliveringState, "", nil); err != nil || row == nil {
		t.Fatalf("settle: %+v %v", row, err)
	}
	// Two Dispatcher replicas must both receive the committed terminal snapshots:
	// the rows are deleted in the same transaction and cannot be read afterward.
	subscribers := make([]*watchSubscription, 0, 2)
	for replica := range 2 {
		hub := NewWatchHub(integrationDatabase.URL)
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
	resolver := callerResolver(t)
	service := &Service{
		Store: store, Auth: resolver, Callers: map[auth.Account]struct{}{caller: {}},
		WakeCallers: map[auth.Account]struct{}{caller: {}}, Sandboxes: oneSandbox{},
	}
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+callerToken))
	request := &dispatcherpb.SubmitInputRequest{CommandId: "api-a", SessionId: "api-session", Sandbox: "sandbox-api", SandboxUid: sandboxUID, Text: "hello"}
	first, err := service.SubmitInput(ctx, request)
	if err != nil || first.GetDispatch().GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER {
		t.Fatalf("first receipt: %+v %v", first, err)
	}
	again, err := service.SubmitInput(ctx, request)
	if err != nil || again.GetDispatch().GetCreatedAt().AsTime() != first.GetDispatch().GetCreatedAt().AsTime() {
		t.Fatalf("retry: %+v %v", again, err)
	}
	_, err = service.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{CommandId: "api-a", SessionId: "api-session", Sandbox: "sandbox-api", SandboxUid: sandboxUID, Text: "different"})
	if status.Code(err) != codes.AlreadyExists {
		t.Fatalf("conflict status: %v", err)
	}
	_, err = service.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{CommandId: "api-b", SessionId: "api-session", Sandbox: "deleted", SandboxUid: sandboxUID})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("missing Sandbox status: %v", err)
	}
	_, err = service.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
		CommandId: "api-replacement", SessionId: "api-session", Sandbox: "sandbox-api", SandboxUid: "old-sandbox-uid", Text: "must not cross",
	})
	if status.Code(err) != codes.FailedPrecondition {
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
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+callerToken))
	store := testStore(t, 5)
	for _, id := range []string{"watch-a", "watch-b", "watch-c"} {
		if _, err := store.Enqueue(ctx, "watch-session", "sandbox-api", sandboxUID, input(id, "x"), nil); err != nil {
			t.Fatal(err)
		}
	}
	resolver := callerResolver(t)
	service := &Service{
		Store: store, Auth: resolver, Callers: map[auth.Account]struct{}{caller: {}},
		Hub:        &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true},
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

func TestWatchDispatchesEndsBlockedWriteOnLeaseOrCancellation(t *testing.T) {
	for _, tc := range []struct {
		name    string
		lease   time.Duration
		cancel  bool
		wantErr bool
	}{
		{name: "lease", lease: 30 * time.Millisecond},
		{name: "caller cancellation", lease: time.Hour, cancel: true, wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			base := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+callerToken))
			ctx, cancel := context.WithCancel(base)
			defer cancel()
			release := make(chan struct{})
			stream := &blockedDispatchWatch{
				ctx: ctx, started: make(chan struct{}), finished: make(chan struct{}), release: release,
			}
			service := &Service{
				Store: testStore(t, 5), Auth: callerResolver(t), Callers: map[auth.Account]struct{}{caller: {}},
				Hub:        &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true},
				WatchLease: tc.lease,
			}
			request := &dispatcherpb.WatchDispatchesRequest{Filter: &dispatcherpb.DispatchFilter{
				Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: "blocked-watch-session"},
			}}
			done := make(chan error, 1)
			go func() { done <- service.WatchDispatches(request, stream) }()
			select {
			case <-stream.started:
			case <-time.After(5 * time.Second):
				t.Fatal("WatchDispatches did not reach its caught-up write")
			}
			if tc.cancel {
				cancel()
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

func TestInterruptTurnRequiresTurnAndPropagatesRunnerRejection(t *testing.T) {
	resolver := callerResolver(t)
	callers := map[auth.Account]struct{}{caller: {}}
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer "+callerToken))

	blocked := &Service{
		Auth: resolver, Callers: callers, WakeCallers: callers, Sandboxes: blockedSandbox{},
	}
	_, err := blocked.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{
		CommandId: "interrupt-empty-turn", SessionId: "api-session", Sandbox: "sandbox-api", SandboxUid: sandboxUID,
	})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("empty turn_id status = %v, want InvalidArgument", err)
	}

	rejection := status.Error(codes.FailedPrecondition, "turn is no longer current")
	runner := &rejectingRunner{err: rejection}
	service := &Service{
		Auth: resolver, Callers: callers, WakeCallers: callers, Sandboxes: oneSandbox{}, Runner: runner,
	}
	_, err = service.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{
		CommandId: "interrupt-stale-turn", SessionId: "api-session", Sandbox: "sandbox-api", SandboxUid: sandboxUID, TurnId: "turn-stale",
	})
	if status.Code(err) != codes.FailedPrecondition || status.Convert(err).Message() != "runner: turn is no longer current" {
		t.Fatalf("runner rejection = %v, want propagated FailedPrecondition", err)
	}
	if got := runner.command.GetInterruptTurn().GetTurnId(); got != "turn-stale" {
		t.Fatalf("runner received turn_id %q, want turn-stale", got)
	}
}

type testCatalog struct{ sandbox SandboxSnapshot }

func (c testCatalog) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	return c.sandbox, name == c.sandbox.Name, nil
}

func (c testCatalog) ListSandboxes(context.Context) ([]SandboxSnapshot, error) {
	return []SandboxSnapshot{c.sandbox}, nil
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
func (*growingCatalog) Changes() <-chan struct{}        { return nil }
func (*growingCatalog) WaitSynced(context.Context) bool { return true }

type testWake struct{}

func (testWake) WakeForSend(context.Context, string, string, func(int64) error) (bool, error) {
	return false, errors.New("unexpected Sandbox wake")
}

type exhaustedWakeBudget struct{ calls int }

func (w *exhaustedWakeBudget) WakeForSend(_ context.Context, _, _ string, beforeResume func(int64) error) (bool, error) {
	w.calls++
	return false, beforeResume(WakeLimit + 1)
}

type failingWake struct{ err error }

func (w failingWake) WakeForSend(context.Context, string, string, func(int64) error) (bool, error) {
	return false, w.err
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
	if _, err := store.Enqueue(ctx, "existing-session", "sandbox-existing", sandboxUID, input("existing-input", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	if err := worker.reconcile(ctx, true); err != nil {
		t.Fatal(err)
	}
	if leases.calls != 1 {
		t.Fatalf("initial lease acquisitions = %d, want 1", leases.calls)
	}
	catalog.sandboxes = append(catalog.sandboxes, SandboxSnapshot{Name: "sandbox-new", UID: newUID})
	if _, err := store.Enqueue(ctx, "new-session", "sandbox-new", newUID, input("new-input", "hello"), nil); err != nil {
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
	if _, err := store.Enqueue(ctx, "lease-busy-session", "sandbox-lease-busy", busyUID, input("lease-busy-input", "hello"), nil); err != nil {
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
	if _, err := store.Enqueue(ctx, "lease-busy-session", "sandbox-lease-busy", busyUID, input("lease-busy-again", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	reconcile(3)
	if !owned(busyUID) {
		t.Fatal("new work did not reacquire the Sandbox")
	}
}

// A dispatch claimed by an owner that died before settling is DELIVERING, and
// neither Heads nor OpeningHeads lists it, yet its successor must still take
// the Sandbox to requeue it.
func TestWorkerAdoptsDeliveringWorkOfDeadOwner(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000083"
	if _, err := store.Enqueue(ctx, "dead-owner-session", "sandbox-dead-owner", uid, input("dead-owner-input", "hello"), nil); err != nil {
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

func (r *acceptingRunner) OpenSession(_ context.Context, _ SandboxSnapshot, sessionID string, _ *runnerpb.SessionSpec, _ *string) error {
	r.calls <- "opening:" + sessionID
	return nil
}

func TestWorkerDeliversWakeLimitedWorkAfterSandboxBecomesReady(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000072"
	const sandboxName = "sandbox-manually-resumed"
	caller := Account{Namespace: "agentplane", Name: "wake-budget-ready-test"}
	for generation := int64(1); generation <= WakeLimit; generation++ {
		if delay, err := store.ReserveWakeAttempt(ctx, caller, uid, generation); err != nil || delay != 0 {
			t.Fatalf("reserve wake generation %d: delay %s, error %v", generation, delay, err)
		}
	}
	if _, err := store.Enqueue(ctx, "wake-ready-command-session", sandboxName, uid,
		input("wake-ready-command", "hello"), &caller); err != nil {
		t.Fatal(err)
	}
	if _, err := store.OpenSession(ctx, "wake-ready-opening-session", sandboxName, uid,
		&runnerpb.SessionSpec{}, nil, &caller); err != nil {
		t.Fatal(err)
	}
	catalog := &growingCatalog{sandboxes: []SandboxSnapshot{{
		Name: sandboxName, Namespace: "agentplane", UID: uid, Incarnation: "old-pod",
		Blocker: "sandbox_suspended", WakeOnSend: true,
	}}}
	runner := &acceptingRunner{calls: make(chan string, 2)}
	leases := testLease{store: store}
	wake := &exhaustedWakeBudget{}
	worker := &DeliveryWorker{Store: store, Catalog: catalog, Wake: wake, Leases: leases, Runner: runner}
	worker.init()
	defer worker.queue.ShutDown()
	owner, err := leases.Acquire(ctx, sandboxName, uid)
	if err != nil || owner == nil {
		t.Fatalf("acquire owner: %+v %v", owner, err)
	}
	worker.owners[uid] = *owner
	for _, sessionID := range []string{"wake-ready-command-session", "wake-ready-opening-session"} {
		if err := worker.processSession(ctx, sessionID); err != nil {
			t.Fatal(err)
		}
		if retry := worker.retryFor(sessionID); retry.kind != retryWakeBudget || time.Until(retry.until) < 20*time.Second {
			t.Fatalf("%s did not retain a long wake-budget delay: %+v", sessionID, retry)
		}
	}
	if row, err := store.GetDispatch(ctx, "wake-ready-command"); err != nil || row.Reason != "wake_rate_limited" {
		t.Fatalf("command was not wake limited: %+v %v", row, err)
	}
	if row, err := store.GetOpening(ctx, "wake-ready-opening-session"); err != nil || row.Reason != "wake_rate_limited" {
		t.Fatalf("opening was not wake limited: %+v %v", row, err)
	}
	if err := worker.reconcile(ctx, false); err != nil {
		t.Fatal(err)
	}
	if got := worker.queue.Len(); got != 2 {
		t.Fatalf("notification queued %d wake-limited sessions, want 2", got)
	}
	for range 2 {
		sessionID, shutdown := worker.queue.Get()
		if shutdown {
			t.Fatal("queue shut down during notification reconciliation")
		}
		if err := worker.processSession(ctx, sessionID); err != nil {
			t.Fatal(err)
		}
		worker.queue.Done(sessionID)
	}
	if wake.calls != 2 {
		t.Fatalf("suspended notification retried wake %d times, want 2 initial attempts", wake.calls)
	}
	catalog.sandboxes[0].Incarnation = "new-pod"
	catalog.sandboxes[0].Blocker = ""
	if err := worker.reconcile(ctx, false); err != nil {
		t.Fatal(err)
	}
	go worker.runQueue(ctx)
	want := map[string]bool{"wake-ready-command": false, "opening:wake-ready-opening-session": false}
	for range want {
		select {
		case got := <-runner.calls:
			if _, okay := want[got]; !okay || want[got] {
				t.Fatalf("unexpected Runner call %q", got)
			}
			want[got] = true
		case <-time.After(5 * time.Second):
			t.Fatal("ready Sandbox did not receive wake-limited work promptly")
		}
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		command, commandErr := store.GetDispatch(ctx, "wake-ready-command")
		opening, openingErr := store.GetOpening(ctx, "wake-ready-opening-session")
		if commandErr == nil && openingErr == nil && command.State == AdmittedState && opening.State == AdmittedState {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("ready work reached Runner but was not durably admitted")
}

func TestWorkerQueuesWithTheKubernetesFailureThatBlockedAWake(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000074"
	const sandboxName = "sandbox-wake-forbidden"
	caller := Account{Namespace: "agentplane", Name: "wake-forbidden-test"}
	if _, err := store.Enqueue(ctx, "wake-forbidden-session", sandboxName, uid,
		input("wake-forbidden-command", "hello"), &caller); err != nil {
		t.Fatal(err)
	}
	forbidden := apierrors.NewForbidden(
		schema.GroupResource{Group: "agentplane.allegedly.works", Resource: "sandboxes"}, sandboxName,
		errors.New("the Role does not grant this verb"),
	)
	leases := testLease{store: store}
	worker := &DeliveryWorker{Store: store, Catalog: testCatalog{SandboxSnapshot{
		Name: sandboxName, Namespace: "agentplane", UID: uid, Incarnation: "old-pod",
		Blocker: "sandbox_suspended", WakeOnSend: true,
	}}, Wake: failingWake{forbidden}, Leases: leases, Runner: &acceptingRunner{calls: make(chan string, 1)}}
	worker.init()
	defer worker.queue.ShutDown()
	owner, err := leases.Acquire(ctx, sandboxName, uid)
	if err != nil || owner == nil {
		t.Fatalf("acquire owner: %+v %v", owner, err)
	}
	worker.owners[uid] = *owner
	if err := worker.processSession(ctx, "wake-forbidden-session"); err != nil {
		t.Fatal(err)
	}
	row, err := store.GetDispatch(ctx, "wake-forbidden-command")
	if err != nil || row.State != QueueState || row.Reason != "sandbox_wake_failed: "+forbidden.Error() {
		t.Fatalf("the failed wake was not named: %+v %v", row, err)
	}
}

func TestWorkerNotificationPreservesFailureBackoff(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000073"
	const sessionID = "rpc-backoff-session"
	const commandID = "rpc-backoff-command"
	if _, err := store.Enqueue(ctx, sessionID, "sandbox-rpc-backoff", uid, input(commandID, "hello"), nil); err != nil {
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
	worker.setRetry(sessionID, commandID, time.Minute, retryFailure)
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
	if _, err := store.Enqueue(ctx, "fenced-session", "sandbox-fenced", sandboxUID, input("fenced-input", "hello"), nil); err != nil {
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

func (*retryRunner) OpenSession(context.Context, SandboxSnapshot, string, *runnerpb.SessionSpec, *string) error {
	return errors.New("unexpected opening")
}

func TestWorkerRetriesUncertainRunnerReplyUnderCurrentOwner(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	if _, err := store.Enqueue(ctx, "worker-session", "sandbox-worker", sandboxUID, input("worker-input", "hello"), nil); err != nil {
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

func (*orderedRunner) OpenSession(context.Context, SandboxSnapshot, string, *runnerpb.SessionSpec, *string) error {
	return errors.New("unexpected opening")
}

func TestWorkerSerializesCommandsPerSession(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store := testStore(t, 3)
	for _, id := range []string{"ordered-1", "ordered-2"} {
		if _, err := store.Enqueue(ctx, "ordered-session", "sandbox-ordered", sandboxUID, input(id, id), nil); err != nil {
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
