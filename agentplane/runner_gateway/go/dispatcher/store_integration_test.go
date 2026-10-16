package dispatcher

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/proto/session_dispatcher"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/auth"
	_ "github.com/lib/pq"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const sandboxUID = "00000000-0000-4000-8000-000000000001"

type reviewedCaller struct{}

func (reviewedCaller) Create(context.Context, *authenticationv1.TokenReview, metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true, Audiences: []string{"dispatcher-audience"},
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:agentplane:app", UID: "sa-uid",
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {"app-0"},
				"authentication.kubernetes.io/pod-uid":  {"pod-uid"},
			},
		},
	}}, nil
}

type oneSandbox struct{}

func (oneSandbox) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	if name != "sandbox-api" {
		return SandboxSnapshot{}, false, nil
	}
	return SandboxSnapshot{Name: name, UID: sandboxUID}, true, nil
}

func testStore(t *testing.T, cap int) *Store {
	t.Helper()
	url := os.Getenv("AGENTPLANE_TEST_DATABASE_URL")
	if url == "" {
		t.Fatal("AGENTPLANE_TEST_DATABASE_URL is required")
	}
	db, err := sql.Open("postgres", url)
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

func stop(id string) *commandpb.Command {
	return &commandpb.Command{CommandId: id, Operation: &commandpb.Command_StopRunnerSession{
		StopRunnerSession: &commandpb.StopRunnerSession{},
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
	if _, err := store.Enqueue(ctx, "session-a", "sandbox-a", sandboxUID, stop("stop"), nil); err != nil {
		t.Fatal(err)
	}
	rows, err := store.ListDispatches(ctx, "session-a", "")
	if err != nil || len(rows) != 4 {
		t.Fatalf("list: %v, %v", rows, err)
	}
	for _, row := range rows[:3] {
		if row.State != CancelledState || row.Reason != "stopped" {
			t.Fatalf("stop did not cancel %+v", row)
		}
	}
	if rows[3].State != QueueState {
		t.Fatalf("stop was not queued: %+v", rows[3])
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
		hub := NewWatchHub(os.Getenv("AGENTPLANE_TEST_DATABASE_URL"))
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
	resolver, err := auth.NewResolver(reviewedCaller{}, "dispatcher-audience", []string{"agentplane"})
	if err != nil {
		t.Fatal(err)
	}
	account := auth.Account{Namespace: "agentplane", Name: "app"}
	service := &Service{
		Store: store, Auth: resolver, Callers: map[auth.Account]struct{}{account: {}},
		WakeCallers: map[auth.Account]struct{}{account: {}}, Sandboxes: oneSandbox{},
	}
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer caller-token"))
	request := &dispatcherpb.SubmitInputRequest{CommandId: "api-a", SessionId: "api-session", Sandbox: "sandbox-api", Text: "hello"}
	first, err := service.SubmitInput(ctx, request)
	if err != nil || first.GetDispatch().GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER {
		t.Fatalf("first receipt: %+v %v", first, err)
	}
	again, err := service.SubmitInput(ctx, request)
	if err != nil || again.GetDispatch().GetCreatedAt().AsTime() != first.GetDispatch().GetCreatedAt().AsTime() {
		t.Fatalf("retry: %+v %v", again, err)
	}
	_, err = service.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{CommandId: "api-a", SessionId: "api-session", Sandbox: "sandbox-api", Text: "different"})
	if status.Code(err) != codes.AlreadyExists {
		t.Fatalf("conflict status: %v", err)
	}
	_, err = service.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{CommandId: "api-b", SessionId: "api-session", Sandbox: "deleted"})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("missing Sandbox status: %v", err)
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

type testWake struct{}

func (testWake) WakeForSend(context.Context, string, string, func(int64) error) (bool, error) {
	return false, errors.New("unexpected Sandbox wake")
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

type retryRunner struct {
	mu    sync.Mutex
	calls int
}

func (r *retryRunner) Deliver(_ context.Context, sandbox SandboxSnapshot, _ string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	if sandbox.UID != sandboxUID || command.GetCommandId() != "worker-input" {
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
	if err := worker.reconcile(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case <-worker.done:
	case <-time.After(5 * time.Second):
		t.Fatal("first runner attempt did not settle")
	}
	queued, err := store.GetDispatch(ctx, "worker-input")
	if err != nil || queued.State != QueueState || queued.Reason != "delivery_unavailable" {
		t.Fatalf("uncertain reply should retry: %+v %v", queued, err)
	}
	if !worker.retryPending("worker-input") {
		t.Fatal("missing retry backoff")
	}
	worker.setRetry("worker-input", 0)
	if err := worker.reconcile(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case <-worker.done:
	case <-time.After(5 * time.Second):
		t.Fatal("retry did not settle")
	}
	admitted, err := store.GetDispatch(ctx, "worker-input")
	if err != nil || admitted.State != AdmittedState || admitted.Admission == nil {
		t.Fatalf("retry was not admitted: %+v %v", admitted, err)
	}
	runner.mu.Lock()
	calls := runner.calls
	runner.mu.Unlock()
	if calls != 2 {
		t.Fatalf("runner calls = %d, want 2", calls)
	}
}
