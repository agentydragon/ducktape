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

	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/wake"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/client-go/util/workqueue"
)

const sandboxUID = "00000000-0000-4000-8000-000000000001"

// caller supplies the legacy account provenance and namespace used by retained-row fixtures.
var caller = auth.Account{Namespace: "test-callers", Name: "test-app"}

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
	rule, err := auth.Services([]string{peertls.AppURI})
	if err != nil {
		t.Fatal(err)
	}
	return captureAdmission(t, rule, peertls.AppURI)
}

func captureAdmission(t *testing.T, rule auth.Rule, uri string) context.Context {
	t.Helper()
	ca := transporttesting.NewCA(t)
	serverTLS, err := ca.Issue(t, peertls.SessionDispatcherURI).WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	clientTLS, err := ca.Issue(t, uri).ClientTLSConfig(peertls.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	methods := auth.Methods{dispatcherpb.SessionDispatcher_GetDispatch_FullMethodName: rule}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	service := admissionContextCapture{captured: make(chan context.Context, 1), release: make(chan struct{})}
	server := grpc.NewServer(append(methods.ServerOptions(0), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	go func() { _ = server.Serve(listener) }()
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)))
	if err != nil {
		server.Stop()
		t.Fatal(err)
	}
	callContext, cancel := context.WithCancel(context.Background())
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

// oneSandbox is an inventory of the single Sandbox "sandbox-api", with the given delivery blocker.
type oneSandbox struct{ blocker string }

func (oneSandbox) GetExecutionIdentity(context.Context, SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return testExecutionIdentity(), true, nil
}

func (s oneSandbox) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	if name != "sandbox-api" {
		return SandboxSnapshot{}, false, nil
	}
	return SandboxSnapshot{Name: name, Namespace: caller.Namespace, UID: sandboxUID, Blocker: s.blocker}, true, nil
}

type rejectingRuntime struct {
	command  *commandpb.Command
	err      error
	admitted map[string]bool
}

func (r *rejectingRuntime) Deliver(_ context.Context, _ SandboxSnapshot, _ string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	r.command = command
	if r.admitted != nil {
		r.admitted[command.CommandId] = true
	}
	return nil, r.err
}

func (r *rejectingRuntime) CreateSession(context.Context, SandboxSnapshot, string, *sessionpb.SessionSpec, *string, store.SessionExecution) error {
	return r.err
}

func testStore(t *testing.T, cap int) *store.Store {
	t.Helper()
	queueStore, _ := testStoreWithURL(t, cap)
	return queueStore
}

func testStoreWithURL(t *testing.T, cap int) (*store.Store, string) {
	t.Helper()
	databaseURL := integrationDatabase.Clone(t)
	db := postgres.Open(t, databaseURL)
	queueStore := store.NewStore(db, cap)
	testStoreDatabaseURLs.Store(db, databaseURL)
	t.Cleanup(func() { testStoreDatabaseURLs.Delete(db) })
	return queueStore, databaseURL
}

var testStoreDatabaseURLs sync.Map // map[*sql.DB]string

func testWatchHub(t *testing.T, queueStore *store.Store) *wakeups.Hub {
	t.Helper()
	value, ok := testStoreDatabaseURLs.Load(queueStore.DB)
	databaseURL, isString := value.(string)
	if !ok || !isString {
		t.Fatal("test store has no registered PostgreSQL URL")
	}
	hub, err := wakeups.Listen(t.Context(), databaseURL, store.NotifyChannel)
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

func claimSessionOwner(t *testing.T, queueStore *store.Store, sessionID string) store.SessionOwner {
	t.Helper()
	owner, err := queueStore.AcquireSessionLease(t.Context(), sessionID)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := queueStore.ReleaseSessionLease(context.Background(), *owner); err != nil {
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
func enqueueWithSession(t *testing.T, queueStore *store.Store, ctx context.Context, sessionID, sandboxName, sandboxUID string, command *commandpb.Command, wakeCaller *auth.Account) (*store.Dispatch, error) {
	t.Helper()
	if _, err := queueStore.GetSession(ctx, sessionID); errors.Is(err, store.ErrNotFound) {
		if _, err := createSession(queueStore, ctx, sessionID, sandboxName, sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
			return nil, err
		}
		if _, err := queueStore.DB.ExecContext(ctx, "UPDATE session SET state=$2 WHERE session_id=$1", sessionID, store.SessionAcceptedState); err != nil {
			return nil, err
		}
	} else if err != nil {
		return nil, err
	}
	return queueStore.EnqueueForCaller(ctx, sessionID, sandboxName, sandboxUID, command, wakeCaller, testStoreCaller(wakeCaller))
}

// wakeAccount is the ServiceAccount a wake caller is, for the tests whose callers are all accounts.
func wakeAccount(caller lifecycle.WakeCaller) *identitypb.ServiceAccountIdentity {
	return caller.(lifecycle.AccountWakeCaller).Account
}

// wakeKey is the budget key the lifecycle manager derives for a ServiceAccount caller.
func wakeKey(caller *identitypb.ServiceAccountIdentity) wake.WakeBudgetKey {
	return wake.WakeBudgetKey{Namespace: caller.GetAccount().GetNamespace(), Name: caller.GetAccount().GetName(), UID: caller.GetUid()}
}

func TestDeletedSandboxRetainsSessionsAndSettlesUnfinishedMaterialization(t *testing.T) {
	ctx := t.Context()
	queueStore := testStore(t, 3)
	uid := uuid.NewString()
	states := []struct{ before, after string }{
		{store.SessionWaitingState, store.SessionFailedState},
		{store.SessionInProgressState, store.SessionUnknownState},
		{store.SessionAcceptedState, store.SessionAcceptedState},
		{store.SessionFailedState, store.SessionFailedState},
		{store.SessionUnknownState, store.SessionUnknownState},
	}
	ids := make([]string, len(states))
	for i, state := range states {
		ids[i] = uuid.NewString()
		if _, err := createSession(queueStore, ctx, ids[i], "deleted-sandbox", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
			t.Fatal(err)
		}
		if _, err := queueStore.DB.ExecContext(ctx, "UPDATE session SET state=$2,reason='prior evidence' WHERE session_id=$1", ids[i], state.before); err != nil {
			t.Fatal(err)
		}
	}
	if err := queueStore.EndSandbox(ctx, uid); err != nil {
		t.Fatal(err)
	}
	if uids, err := queueStore.ListSandboxUIDs(ctx); err != nil || len(uids) != 0 {
		t.Fatalf("retained Sessions re-entered cleanup inventory: %+v %v", uids, err)
	}
	for i, state := range states {
		row, err := queueStore.GetSession(ctx, ids[i])
		if err != nil || row.EnvironmentDeletedAt == nil || row.SandboxUID != uid || row.State != state.after {
			t.Fatalf("retained Session %d: %+v %v", i, row, err)
		}
		if state.before == store.SessionWaitingState || state.before == store.SessionInProgressState {
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
		if err := queueStore.EndSandbox(ctx, uid); err != nil {
			t.Fatal(err)
		}
		after, err := queueStore.GetSession(ctx, ids[i])
		if err != nil || !after.UpdatedAt.Equal(row.UpdatedAt) || !after.EnvironmentDeletedAt.Equal(*row.EnvironmentDeletedAt) {
			t.Fatalf("repeated cleanup rewrote Session %d: %+v %v", i, after, err)
		}
	}
}

func TestRPCAdmissionReturnsSameQueuedReceiptAndConflicts(t *testing.T) {
	queueStore := testStore(t, 3)
	service := &Service{
		Store: queueStore, DispatcherServices: sets.New(peertls.AppURI), Sandboxes: oneSandbox{},
	}
	ctx := admitted(t)
	if _, err := createSession(queueStore, ctx, "api-session", "sandbox-api", sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
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
	queueStore := testStore(t, 5)
	for _, id := range []string{"watch-a", "watch-b", "watch-c"} {
		if _, err := enqueueWithSession(t, queueStore, ctx, "watch-session", "sandbox-api", sandboxUID, input(id, "x"), nil); err != nil {
			t.Fatal(err)
		}
	}
	service := &Service{
		Store: queueStore, DispatcherServices: sets.New(peertls.AppURI), Hub: testWatchHub(t, queueStore),
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
	queueStore := testStore(t, 3)
	uid := uuid.NewString()
	dispatch, err := enqueueWithSession(t, queueStore, base, "watch-wake-session", "sandbox-wake-watch", uid,
		input("watch-wake-command", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	deliveryOwner := claimSessionOwner(t, queueStore, dispatch.SessionID)
	hub := testWatchHub(t, queueStore)
	watchCtx, cancelWatch := context.WithCancel(base)
	defer cancelWatch()
	service := &Service{
		Store: queueStore, DispatcherServices: sets.New(peertls.AppURI), Hub: hub, WatchLease: time.Minute,
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
	if err := queueStore.SetDeliveryAssessment(base, dispatch.CommandID, deliveryOwner, false, blocker, assessment); err != nil {
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
				queueStore := testStore(t, 5)
				service := &Service{
					Store: queueStore, DispatcherServices: sets.New(peertls.AppURI), Hub: testWatchHub(t, queueStore),
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
	queueStore := testStore(t, 3)
	service := &Service{Store: queueStore, DispatcherServices: sets.New(peertls.AppURI), Sandboxes: oneSandbox{}}
	if _, err := createSession(queueStore, ctx, "api-session", "sandbox-api", sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
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
	head, err := queueStore.GetDispatch(ctx, request.CommandId)
	if err != nil || head.Command.GetInterruptTurn().GetTurnId() != request.TurnId {
		t.Fatalf("stored exact turn: %+v %v", head, err)
	}
	deliveryOwner := claimSessionOwner(t, queueStore, request.SessionId)
	runtime := &rejectingRuntime{err: status.Error(codes.FailedPrecondition, "turn is no longer current")}
	session, err := queueStore.GetSession(ctx, request.SessionId)
	if err != nil {
		t.Fatal(err)
	}
	worker := &DeliveryWorker{Store: queueStore, Delivery: runtime, Catalog: testCatalog{}}
	worker.init()
	defer worker.queue.ShutDown()
	if !worker.deliverCommand(ctx, head, SandboxSnapshot{UID: sandboxUID}, deliveryOwner, session) {
		t.Fatal("worker did not attempt interrupt")
	}
	row, err := queueStore.GetDispatch(ctx, request.CommandId)
	if err != nil || row.State != store.UnknownState || runtime.command.GetInterruptTurn().GetTurnId() != request.TurnId {
		t.Fatalf("stale interrupt result: %+v %v", row, err)
	}
}

type testCatalog struct{ sandbox SandboxSnapshot }

func (testCatalog) GetExecutionIdentity(context.Context, SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return testExecutionIdentity(), true, nil
}

func (c testCatalog) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	return c.sandbox, name == c.sandbox.Name, nil
}

func (c testCatalog) ListSandboxes(context.Context) ([]SandboxSnapshot, error) {
	return []SandboxSnapshot{c.sandbox}, nil
}

func (c testCatalog) CurrentSandboxUIDs(context.Context) (map[string]struct{}, error) {
	return map[string]struct{}{c.sandbox.UID: {}}, nil
}
func (testCatalog) WatchSandboxes(func(SandboxKey)) (func(), error) { return func() {}, nil }
func (c testCatalog) SandboxExists(_ context.Context, key SandboxKey) (bool, error) {
	return c.sandbox.UID == key.UID, nil
}
func (testCatalog) WaitSynced(context.Context) bool { return true }

type growingCatalog struct{ sandboxes []SandboxSnapshot }

func (*growingCatalog) GetExecutionIdentity(context.Context, SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return testExecutionIdentity(), true, nil
}

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
func (*growingCatalog) WatchSandboxes(func(SandboxKey)) (func(), error) { return func() {}, nil }
func (c *growingCatalog) SandboxExists(_ context.Context, key SandboxKey) (bool, error) {
	for _, s := range c.sandboxes {
		if s.UID == key.UID {
			return true, nil
		}
	}
	return false, nil
}
func (*growingCatalog) WaitSynced(context.Context) bool { return true }

type testWake struct{}

func (testWake) RequestWake(_ context.Context, _ *lifecycle.Target, _ lifecycle.WakeCaller) (*environmentpb.WakeAssessment, error) {
	return &environmentpb.WakeAssessment{
		State:      environmentpb.WakeState_WAKE_STATE_NOT_NEEDED,
		ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(0),
	}, nil
}

type rateLimitedWake struct {
	calls int
}

func (w *rateLimitedWake) RequestWake(_ context.Context, _ *lifecycle.Target, _ lifecycle.WakeCaller) (*environmentpb.WakeAssessment, error) {
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

func (w *scriptedWake) RequestWake(_ context.Context, target *lifecycle.Target, caller lifecycle.WakeCaller) (*environmentpb.WakeAssessment, error) {
	w.callers = append(w.callers, wakeAccount(caller))
	w.targets = append(w.targets, target)
	if len(w.assessments) == 0 {
		return nil, errors.New("unexpected lifecycle wake request")
	}
	assessment := w.assessments[0]
	w.assessments = w.assessments[1:]
	return assessment, nil
}

type wakeWorkerFixture struct {
	queueStore *store.Store
	opening    *store.Session
	dispatch   *store.Dispatch
	owner      store.SessionOwner
	sandbox    SandboxSnapshot
	caller     *lifecycle.ServiceAccountIdentity
	worker     *DeliveryWorker
}

func newWakeWorkerFixture(t *testing.T, spec *sessionpb.SessionSpec, wake WakeRequester) wakeWorkerFixture {
	t.Helper()
	ctx := context.Background()
	queueStore := testStore(t, 3)
	sandboxName, sandboxUID := "sandbox-"+uuid.NewString()[:8], uuid.NewString()
	if spec.GetPlacement().GetCentralSamplerV1() != nil {
		spec.GetPlacement().GetCentralSamplerV1().ExecutorEnvironment = &sessionpb.ExactEnvironment{Name: sandboxName, Uid: sandboxUID}
	}
	sessionID, commandID := uuid.NewString(), uuid.NewString()
	caller := &lifecycle.ServiceAccountIdentity{Account: &identitypb.ServiceAccount{Namespace: "agentplane", Name: sandboxName}, Uid: uuid.NewString()}
	actor := &lifecycle.ServiceAccountIdentity{Account: &identitypb.ServiceAccount{Namespace: "agentplane", Name: "action-broker"}, Uid: uuid.NewString()}
	callerIdentity := proto.Clone(caller).(*identitypb.ServiceAccountIdentity)
	actorIdentity := proto.Clone(actor).(*identitypb.ServiceAccountIdentity)
	attribution := &store.AdmissionAttribution{Actor: actorIdentity, EffectiveCaller: callerIdentity}
	opening, err := createSessionForCaller(queueStore, ctx, sessionID, sandboxName, sandboxUID, spec, nil, nil,
		testExecutionIdentity(), store.ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: callerIdentity, Attribution: attribution})
	if err != nil {
		t.Fatal(err)
	}
	dispatch, err := queueStore.EnqueueForCaller(ctx, sessionID, sandboxName, sandboxUID, input(commandID, "hello"), nil,
		store.ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: callerIdentity, Attribution: attribution})
	if err != nil {
		t.Fatal(err)
	}
	worker := &DeliveryWorker{Store: queueStore, Wake: wake}
	worker.init()
	t.Cleanup(worker.queue.ShutDown)
	return wakeWorkerFixture{
		queueStore: queueStore, opening: opening, dispatch: dispatch, owner: claimSessionOwner(t, queueStore, sessionID), caller: caller,
		sandbox: SandboxSnapshot{
			Name: sandboxName, Namespace: "agentplane", UID: sandboxUID, Incarnation: "before-wake",
			Blocker: "sandbox_suspended", WakeOnSend: true, WakeGeneration: 7,
		},
		worker: worker,
	}
}

type blockedSessionRuntime struct {
	entered chan string
	release chan struct{}
}

func (r *blockedSessionRuntime) Deliver(ctx context.Context, _ SandboxSnapshot, sessionID string, _ *commandpb.Command) (*eventlogpb.EventEntry, error) {
	r.entered <- sessionID
	select {
	case <-r.release:
		return &eventlogpb.EventEntry{Cursor: 1}, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (*blockedSessionRuntime) CreateSession(context.Context, SandboxSnapshot, string, *sessionpb.SessionSpec, *string, store.SessionExecution) error {
	return nil
}

func TestWorkersProcessDifferentSessionsOnSameSandboxConcurrently(t *testing.T) {
	ctx := context.Background()
	queueStore := testStore(t, 3)
	const sandboxName = "shared-sandbox"
	const sandboxUID = "00000000-0000-4000-8000-000000000084"
	for _, sessionID := range []string{"parallel-session-a", "parallel-session-b"} {
		if _, err := enqueueWithSession(t, queueStore, ctx, sessionID, sandboxName, sandboxUID, input(sessionID+"-input", "hello"), nil); err != nil {
			t.Fatal(err)
		}
	}
	runtime := &blockedSessionRuntime{entered: make(chan string, 2), release: make(chan struct{}, 2)}
	sandbox := SandboxSnapshot{Name: sandboxName, Namespace: "agentplane", UID: sandboxUID, Incarnation: "pod-1"}
	workers := []*DeliveryWorker{
		{Store: queueStore, Catalog: testCatalog{sandbox: sandbox}, Wake: testWake{}, Delivery: runtime},
		{Store: queueStore, Catalog: testCatalog{sandbox: sandbox}, Wake: testWake{}, Delivery: runtime},
	}
	for _, worker := range workers {
		worker.init()
		defer worker.queue.ShutDown()
	}
	results := make(chan error, 2)
	go func() { results <- workers[0].processSession(ctx, "parallel-session-a") }()
	select {
	case got := <-runtime.entered:
		if got != "parallel-session-a" {
			t.Fatalf("first delivery session = %q", got)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("first Session did not reach the Runner")
	}
	go func() { results <- workers[1].processSession(ctx, "parallel-session-b") }()
	select {
	case got := <-runtime.entered:
		if got != "parallel-session-b" {
			t.Fatalf("second delivery session = %q", got)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a held Session lease blocked different work on the same Sandbox")
	}
	runtime.release <- struct{}{}
	runtime.release <- struct{}{}
	for range 2 {
		select {
		case err := <-results:
			if err != nil {
				t.Fatal(err)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("Session delivery did not finish")
		}
	}
	for _, commandID := range []string{"parallel-session-a-input", "parallel-session-b-input"} {
		row, err := queueStore.GetDispatch(ctx, commandID)
		if err != nil || row.State != store.AdmittedState {
			t.Fatalf("parallel command %s: %+v %v", commandID, row, err)
		}
	}
}

type retryRuntime struct {
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
		if state != store.QueueState || reason != "runner_unreachable: Unavailable: uncertain reply" {
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

type acceptingRuntime struct{ calls chan string }

func (r *acceptingRuntime) Deliver(_ context.Context, _ SandboxSnapshot, _ string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	r.calls <- command.GetCommandId()
	return &eventlogpb.EventEntry{Cursor: 1}, nil
}

func (r *acceptingRuntime) CreateSession(_ context.Context, _ SandboxSnapshot, sessionID string, _ *sessionpb.SessionSpec, _ *string, _ store.SessionExecution) error {
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
			row, err := fixture.queueStore.GetDispatch(context.Background(), fixture.dispatch.CommandID)
			if err != nil || row.State != store.QueueState || row.WakeAssessment.GetState() != tc.wantState ||
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
	expireSessionLease(t, fixture.queueStore, fixture.owner)
	claimSessionOwner(t, fixture.queueStore, fixture.dispatch.SessionID)
	ready, err := fixture.worker.prepareDispatch(context.Background(), fixture.dispatch, fixture.sandbox, fixture.owner)
	if err != nil || ready || len(wake.callers) != 0 {
		t.Fatalf("wake after lease loss: ready=%v calls=%d err=%v", ready, len(wake.callers), err)
	}
	stored, err := fixture.queueStore.GetDispatch(context.Background(), fixture.dispatch.CommandID)
	if err != nil || stored.State != store.QueueState || stored.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_NOT_EVALUATED {
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
				row, err := fixture.queueStore.GetDispatch(context.Background(), fixture.dispatch.CommandID)
				if err != nil || row.State != store.QueueState || row.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_RATE_LIMITED ||
					row.WakeAssessment.GetRetryAt() == nil || row.WakeAssessment.GetObservedAt() == nil {
					t.Fatalf("rate-limited dispatch: %+v %v", row, err)
				}
			} else {
				row, err := fixture.queueStore.GetSession(context.Background(), fixture.opening.SessionID)
				if err != nil || row.State != store.SessionWaitingState || row.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_RATE_LIMITED ||
					row.WakeAssessment.GetRetryAt() == nil || row.WakeAssessment.GetObservedAt() == nil {
					t.Fatalf("rate-limited opening: %+v %v", row, err)
				}
			}
			if len(wake.callers) != 1 {
				t.Fatalf("lifecycle calls = %d, want one", len(wake.callers))
			}
			if kind == "dispatch" {
				row, err := fixture.queueStore.GetDispatch(context.Background(), fixture.dispatch.CommandID)
				if err != nil {
					t.Fatal(err)
				}
				if _, err := fixture.worker.prepareDispatch(context.Background(), row, fixture.sandbox, fixture.owner); err != nil {
					t.Fatal(err)
				}
			} else {
				row, err := fixture.queueStore.GetSession(context.Background(), fixture.opening.SessionID)
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
	prepare := func() *store.Dispatch {
		t.Helper()
		row, err := fixture.queueStore.GetDispatch(context.Background(), fixture.dispatch.CommandID)
		if err != nil {
			t.Fatal(err)
		}
		if ready, err := fixture.worker.prepareDispatch(context.Background(), row, fixture.sandbox, fixture.owner); err != nil || ready {
			t.Fatalf("wake retry preparation = %v, %v", ready, err)
		}
		row, err = fixture.queueStore.GetDispatch(context.Background(), fixture.dispatch.CommandID)
		if err != nil {
			t.Fatal(err)
		}
		return row
	}
	first := prepare()
	if first.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_DENIED || first.State != store.QueueState {
		t.Fatalf("first denial failed queued delivery: %+v", first)
	}
	second := prepare()
	if second.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_REQUESTED || second.State != store.QueueState || second.WakeRequestedAt == nil {
		t.Fatalf("later authorized wake was not recorded while queued: %+v", second)
	}
	if len(wake.callers) != 2 || !proto.Equal(wake.callers[0], fixture.caller) ||
		!proto.Equal(wake.callers[1], fixture.caller) {
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
	prepare := func(sandbox SandboxSnapshot) *store.Dispatch {
		t.Helper()
		row, err := fixture.queueStore.GetDispatch(context.Background(), fixture.dispatch.CommandID)
		if err != nil {
			t.Fatal(err)
		}
		if ready, err := fixture.worker.prepareDispatch(context.Background(), row, sandbox, fixture.owner); err != nil || ready {
			t.Fatalf("unknown wake preparation = %v, %v", ready, err)
		}
		row, err = fixture.queueStore.GetDispatch(context.Background(), fixture.dispatch.CommandID)
		if err != nil {
			t.Fatal(err)
		}
		return row
	}
	unknown := prepare(fixture.sandbox)
	if unknown.State != store.QueueState || unknown.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN ||
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
	if len(wake.callers) != 2 || changed.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_DENIED || changed.State != store.QueueState {
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
	dispatch, err := fixture.queueStore.GetDispatch(context.Background(), fixture.dispatch.CommandID)
	if err != nil || dispatch.State != store.QueueState || dispatch.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_NOT_NEEDED || dispatch.DeliveryBlocker != nil {
		t.Fatalf("central input did not retain a deliverable queued receipt: %+v %v", dispatch, err)
	}
	opening, err := fixture.queueStore.GetSession(context.Background(), fixture.opening.SessionID)
	if err != nil || opening.State != store.SessionWaitingState || opening.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_NOT_NEEDED || opening.DeliveryBlocker != nil {
		t.Fatalf("central opening did not retain a deliverable queued receipt: %+v %v", opening, err)
	}
}

func TestWorkerNotificationPreservesFailureBackoff(t *testing.T) {
	ctx := context.Background()
	queueStore := testStore(t, 3)
	const uid = "00000000-0000-4000-8000-000000000073"
	const sessionID = "rpc-backoff-session"
	const commandID = "rpc-backoff-command"
	if _, err := enqueueWithSession(t, queueStore, ctx, sessionID, "sandbox-rpc-backoff", uid, input(commandID, "hello"), nil); err != nil {
		t.Fatal(err)
	}
	runtime := &acceptingRuntime{calls: make(chan string, 1)}
	worker := &DeliveryWorker{Store: queueStore, Catalog: testCatalog{SandboxSnapshot{
		Name: "sandbox-rpc-backoff", Namespace: "agentplane", UID: uid, Incarnation: "pod-1",
	}}, Wake: testWake{}, Delivery: runtime}
	worker.init()
	defer worker.queue.ShutDown()
	recorded := &recordingDelayQueue{TypedRateLimitingInterface: worker.queue}
	worker.queue = recorded
	worker.setRetry(sessionID, commandID, time.Minute)
	before := len(recorded.delays)
	if err := worker.reconcile(ctx); err != nil {
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
	case got := <-runtime.calls:
		t.Fatalf("RPC retry called Runner early: %s", got)
	default:
	}
}

func TestWorkerRequiresFreshLeaseBeforeDelivery(t *testing.T) {
	ctx := context.Background()
	queueStore := testStore(t, 3)
	if _, err := enqueueWithSession(t, queueStore, ctx, "fenced-session", "sandbox-fenced", sandboxUID, input("fenced-input", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	runtime := &retryRuntime{commandID: "fenced-input"}
	worker := &DeliveryWorker{Store: queueStore, Catalog: testCatalog{SandboxSnapshot{
		Name: "sandbox-fenced", Namespace: "agentplane", UID: sandboxUID, Incarnation: "pod-1",
	}}, Wake: testWake{}, Delivery: runtime}
	worker.init()
	defer worker.queue.ShutDown()
	stale := claimSessionOwner(t, queueStore, "fenced-session")
	expireSessionLease(t, queueStore, stale)
	successor := claimSessionOwner(t, queueStore, "fenced-session")
	session, err := queueStore.GetSession(ctx, "fenced-session")
	if err != nil {
		t.Fatal(err)
	}
	head, err := queueStore.GetDispatch(ctx, "fenced-input")
	if err != nil {
		t.Fatal(err)
	}
	if worker.deliverCommand(ctx, head, SandboxSnapshot{UID: sandboxUID}, stale, session) {
		t.Fatal("stale Session lease attempted delivery")
	}
	runtime.mu.Lock()
	calls := runtime.calls
	runtime.mu.Unlock()
	if calls != 0 {
		t.Fatalf("stale Session lease allowed %d Runner calls", calls)
	}
	if !worker.deliverCommand(ctx, head, SandboxSnapshot{UID: sandboxUID}, successor, session) {
		t.Fatal("current Session lease did not attempt delivery")
	}
	runtime.mu.Lock()
	calls = runtime.calls
	runtime.mu.Unlock()
	if calls != 1 {
		t.Fatalf("current Session lease made %d Runner calls, want 1", calls)
	}
}

func (r *retryRuntime) Deliver(_ context.Context, sandbox SandboxSnapshot, _ string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
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

func (*retryRuntime) CreateSession(context.Context, SandboxSnapshot, string, *sessionpb.SessionSpec, *string, store.SessionExecution) error {
	return errors.New("unexpected opening")
}

func TestWorkerRetriesUncertainRunnerReplyUnderCurrentOwner(t *testing.T) {
	ctx := context.Background()
	queueStore := testStore(t, 3)
	if _, err := enqueueWithSession(t, queueStore, ctx, "worker-session", "sandbox-worker", sandboxUID, input("worker-input", "hello"), nil); err != nil {
		t.Fatal(err)
	}
	runtime := &retryRuntime{}
	worker := &DeliveryWorker{Store: queueStore, Catalog: testCatalog{SandboxSnapshot{
		Name: "sandbox-worker", Namespace: "agentplane", UID: sandboxUID, Incarnation: "pod-1",
	}}, Wake: testWake{}, Delivery: runtime}
	worker.init()
	workerCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	defer worker.queue.ShutDown()
	go worker.runQueue(workerCtx)
	if err := worker.reconcile(ctx); err != nil {
		t.Fatal(err)
	}
	waitState := func(state string) *store.Dispatch {
		t.Helper()
		deadline := time.Now().Add(5 * time.Second)
		for time.Now().Before(deadline) {
			row, err := queueStore.GetDispatch(ctx, "worker-input")
			if err == nil && row.State == state && (state != store.QueueState || row.Reason == "runner_unreachable: Unavailable: lost runner reply") {
				return row
			}
			time.Sleep(10 * time.Millisecond)
		}
		t.Fatalf("dispatch did not reach %s", state)
		return nil
	}
	queued := waitState(store.QueueState)
	if queued.Reason != "runner_unreachable: Unavailable: lost runner reply" {
		t.Fatalf("uncertain reply should retry: %+v", queued)
	}
	if time.Until(worker.retryFor("worker-session").until) <= 0 {
		t.Fatal("missing retry backoff")
	}
	admitted := waitState(store.AdmittedState)
	if admitted.Admission == nil {
		t.Fatalf("retry was not admitted: %+v", admitted)
	}
	runtime.mu.Lock()
	calls := runtime.calls
	runtime.mu.Unlock()
	if calls != 2 {
		t.Fatalf("runner calls = %d, want 2", calls)
	}
}

type orderedRuntime struct {
	entered chan string
	release chan struct{}
	mu      sync.Mutex
	active  int
	peak    int
}

func (r *orderedRuntime) Deliver(_ context.Context, _ SandboxSnapshot, _ string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
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

func (*orderedRuntime) CreateSession(context.Context, SandboxSnapshot, string, *sessionpb.SessionSpec, *string, store.SessionExecution) error {
	return errors.New("unexpected opening")
}

func TestWorkerSerializesCommandsPerSession(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	queueStore := testStore(t, 3)
	for _, id := range []string{"ordered-1", "ordered-2"} {
		if _, err := enqueueWithSession(t, queueStore, ctx, "ordered-session", "sandbox-ordered", sandboxUID, input(id, id), nil); err != nil {
			t.Fatal(err)
		}
	}
	runtime := &orderedRuntime{entered: make(chan string, 2), release: make(chan struct{}, 2)}
	worker := &DeliveryWorker{Store: queueStore, Catalog: testCatalog{SandboxSnapshot{
		Name: "sandbox-ordered", Namespace: "agentplane", UID: sandboxUID, Incarnation: "pod-1",
	}}, Wake: testWake{}, Delivery: runtime}
	worker.init()
	defer worker.queue.ShutDown()
	go worker.runQueue(ctx)
	go worker.runQueue(ctx)
	if err := worker.reconcile(ctx); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"ordered-1", "ordered-2"} {
		select {
		case got := <-runtime.entered:
			if got != want {
				t.Fatalf("delivery order = %s, want %s", got, want)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("missing %s delivery", want)
		}
		if err := worker.reconcile(ctx); err != nil {
			t.Fatal(err)
		}
		runtime.release <- struct{}{}
	}
	runtime.mu.Lock()
	peak := runtime.peak
	runtime.mu.Unlock()
	if peak != 1 {
		t.Fatalf("parallel deliveries for one Session: %d", peak)
	}
}

func TestCentralCommandsAndInterruptIgnoreExecutorReadiness(t *testing.T) {
	ctx := admitted(t)
	queueStore := testStore(t, 3)
	spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: "sandbox-api", Uid: sandboxUID}}}}}
	opening, err := createSession(queueStore, ctx, "central-unready-session", "sandbox-api", sandboxUID, spec, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	row, err := enqueueWithSession(t, queueStore, ctx, opening.SessionID, "sandbox-api", sandboxUID, input("central-unready-input", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	sandbox, _, _ := (oneSandbox{blocker: "sandbox_suspended"}).GetSandbox(ctx, "sandbox-api")
	worker := &DeliveryWorker{Store: queueStore}
	worker.init()
	defer worker.queue.ShutDown()
	deliveryOwner := claimSessionOwner(t, queueStore, opening.SessionID)
	if ready, err := worker.prepareSession(ctx, opening, sandbox, deliveryOwner); err != nil || !ready {
		t.Fatalf("central opening readiness %v %v", ready, err)
	}
	if ready, err := worker.prepareDispatch(ctx, row, sandbox, deliveryOwner); err != nil || !ready {
		t.Fatalf("central command readiness %v %v", ready, err)
	}
	service := &Service{Store: queueStore, DispatcherServices: sets.New(peertls.AppURI), Sandboxes: oneSandbox{blocker: "sandbox_suspended"}}
	result, err := service.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{CommandId: "central-interrupt", SessionId: opening.SessionID, ExpectedSandboxUid: sandboxUID, TurnId: "active-turn"})
	if err != nil || result.GetDispatch().GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER {
		t.Fatalf("central interrupt: %v %v", result, err)
	}
}

func TestCentralSessionUsesConfiguredSampler(t *testing.T) {
	ctx := admitted(t)
	queueStore := testStore(t, 3)
	service := &Service{Store: queueStore, DispatcherServices: sets.New(peertls.AppURI), Sandboxes: oneSandbox{blocker: "sandbox_suspended"}, CentralTarget: &CentralTarget{Target: "configured", TokenFile: "configured"}}
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
	encoded, err := store.Wire(legacy)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := queueStore.DB.ExecContext(ctx, "UPDATE session SET spec=$1 WHERE session_id=$2", encoded, request.SessionId); err != nil {
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
	queueStore := testStore(t, 3)
	service := &Service{Store: queueStore}
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
			if err := queueStore.DB.QueryRowContext(ctx, "SELECT EXISTS (SELECT 1 FROM session WHERE session_id=$1)", sessionID).Scan(&recorded); err != nil {
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
	return testExecutionIdentity(), true, nil
}

func TestSandboxCleanupFencesStaleAdmissionAcrossReplicas(t *testing.T) {
	for _, existing := range []bool{false, true} {
		t.Run(fmt.Sprintf("existing=%t", existing), func(t *testing.T) {
			queueStore, databaseURL := testStoreWithURL(t, 3)
			ctx := admitted(t)
			uid, sessionID := uuid.NewString(), uuid.NewString()
			if existing {
				// Keep existing work distinct from this fresh admission. An exact
				// retained retry does not consult inventory or trigger its cleanup hook.
				if _, err := createSession(queueStore, ctx, uuid.NewString(), "sandbox-api", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
					t.Fatal(err)
				}
			}
			// Independent connections and Store instances represent another replica or a restart.
			db, err := sql.Open("postgres", databaseURL)
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			restarted := store.NewStore(db, 3)
			service := &Service{
				Store: restarted, DispatcherServices: sets.New(peertls.AppURI),
				Sandboxes: deletedDuringLookup{
					snapshot: SandboxSnapshot{Name: "sandbox-api", Namespace: caller.Namespace, UID: uid},
					cleanup: func() {
						if err := queueStore.EndSandbox(ctx, uid); err != nil {
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
			if _, err := restarted.GetSession(ctx, sessionID); !errors.Is(err, store.ErrNotFound) {
				t.Fatalf("opening resurrected: %v", err)
			}
			if existing {
				retained, err := restarted.ListSessionsForCaller(ctx, "", 10, store.ServiceAccountCaller{Privileged: true})
				if err != nil || len(retained) != 1 || retained[0].EnvironmentDeletedAt == nil || retained[0].State != store.SessionFailedState {
					t.Fatalf("Session lost during cleanup: %+v %v", retained, err)
				}
			}
			if uids, err := restarted.ListSandboxUIDs(ctx); err != nil || len(uids) != 0 {
				t.Fatalf("live queue/ownership state resurrected: %v %v", uids, err)
			}
			// There is no UUID tombstone. This deliberately reuses the ID to test the
			// limit of the caller contract; retained History may reject the new writer.
			if _, err := createSession(restarted, ctx, sessionID, "sandbox-api", uuid.NewString(), &sessionpb.SessionSpec{}, nil, nil); err != nil {
				t.Fatalf("forgotten Session ID unexpectedly retained after cleanup: %v", err)
			}
		})
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
			queueStore := testStore(t, 3)
			ctx := t.Context()
			uid, sessionID := uuid.NewString(), uuid.NewString()
			catalog := &deletionCheckCatalog{}
			catalog.afterSnapshot = func() {
				// A live environment and its opening appear after the worker's first
				// inventory snapshot, before it enumerates the database's stored UIDs.
				if currentState == "live" {
					catalog.sandboxes = []SandboxSnapshot{{Name: "new-sandbox", UID: uid}}
				}
				if _, err := createSession(queueStore, ctx, sessionID, "new-sandbox", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
					t.Fatal(err)
				}
			}
			if currentState == "unavailable" {
				catalog.currentError = errors.New("inventory unavailable")
			}
			worker := &DeliveryWorker{Store: queueStore, Catalog: catalog}
			worker.init()
			defer worker.queue.ShutDown()
			err := worker.reconcile(ctx)
			if !errors.Is(err, catalog.currentError) {
				t.Fatalf("reconcile: got %v, want %v", err, catalog.currentError)
			}
			if currentState == "absent" {
				row, err := queueStore.GetSession(ctx, sessionID)
				if err != nil || row.EnvironmentDeletedAt == nil || row.State != store.SessionFailedState {
					t.Fatalf("absent environment Session not retained and settled: %+v %v", row, err)
				}
				if _, err := createSession(queueStore, ctx, uuid.NewString(), "new-sandbox", uid, &sessionpb.SessionSpec{}, nil, nil); !errors.Is(err, store.ErrSandboxDeleted) {
					t.Fatalf("absent environment not fenced: %v", err)
				}
				return
			}
			if _, err := queueStore.GetSession(ctx, sessionID); err != nil {
				t.Fatalf("live opening removed: %v", err)
			}
			if _, err := createSession(queueStore, ctx, uuid.NewString(), "new-sandbox", uid, &sessionpb.SessionSpec{}, nil, nil); err != nil {
				t.Fatalf("live environment admission fenced: %v", err)
			}
		})
	}
}

type openingOutcomeRuntime struct {
	acceptingRuntime
	err error
}

func (r *openingOutcomeRuntime) CreateSession(_ context.Context, _ SandboxSnapshot, id string, _ *sessionpb.SessionSpec, _ *string, _ store.SessionExecution) error {
	r.calls <- "opening:" + id
	return r.err
}

func TestSessionOutcomesFenceQueuedWork(t *testing.T) {
	cases := []struct {
		name         string
		code         codes.Code
		openingState string
		commandState string
	}{
		{"invalid request", codes.InvalidArgument, store.SessionFailedState, store.FailedState},
		{"conflicting request", codes.AlreadyExists, store.SessionFailedState, store.FailedState},
		{"unknown response", codes.Unknown, store.SessionUnknownState, store.QueueState},
		{"internal response", codes.Internal, store.SessionUnknownState, store.QueueState},
		{"resource exhaustion", codes.ResourceExhausted, store.SessionUnknownState, store.QueueState},
		{"unproven precondition", codes.FailedPrecondition, store.SessionUnknownState, store.QueueState},
		{"unavailable reservation", codes.Unavailable, store.SessionWaitingState, store.QueueState},
		{"lost reservation response", codes.DeadlineExceeded, store.SessionWaitingState, store.QueueState},
		{"admitted", codes.OK, store.SessionAcceptedState, store.AdmittedState},
	}
	for _, kind := range []string{"native", "central"} {
		for _, test := range cases {
			t.Run(kind+"/"+test.name, func(t *testing.T) {
				if kind == "central" && (test.code == codes.InvalidArgument || test.code == codes.AlreadyExists) {
					test.openingState, test.commandState = store.SessionUnknownState, store.QueueState
				}
				ctx := t.Context()
				queueStore := testStore(t, 3)
				sessionID, uid, commandID := uuid.NewString(), uuid.NewString(), uuid.NewString()
				sandbox := SandboxSnapshot{Name: "test-materialization", Namespace: "test-namespace", UID: uid, Incarnation: "test-incarnation"}
				spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: uid}, Harness: sessionpb.Harness_HARNESS_CODEX}}}}
				if kind == "central" {
					spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: uid}}}}
				}
				if _, err := createSession(queueStore, ctx, sessionID, sandbox.Name, uid, spec, nil, nil); err != nil {
					t.Fatal(err)
				}
				command := input(commandID, "queued before owner response")
				if _, err := enqueueWithSession(t, queueStore, ctx, sessionID, sandbox.Name, uid, command, nil); err != nil {
					t.Fatal(err)
				}
				runtime := &openingOutcomeRuntime{acceptingRuntime: acceptingRuntime{calls: make(chan string, 3)}, err: status.Error(test.code, "test owner response")}
				worker := &DeliveryWorker{Store: queueStore, Catalog: testCatalog{sandbox: sandbox}, Delivery: runtime}
				worker.init()
				defer worker.queue.ShutDown()
				if kind == "central" && test.code == codes.InvalidArgument {
					// An earlier request may have reserved the Session before its response was lost.
					runtime.err = status.Error(codes.DeadlineExceeded, "lost reservation response")
					if err := worker.processSession(ctx, sessionID); err != nil {
						t.Fatal(err)
					}
					if len(runtime.calls) != 1 || <-runtime.calls != "opening:"+sessionID {
						t.Fatal("first opening did not reach owner")
					}
					worker.clearRetry(sessionID)
					runtime.err = status.Error(test.code, "test owner response")
				}
				if err := worker.reconcile(ctx); err != nil {
					t.Fatal(err)
				}
				if err := worker.processSession(ctx, sessionID); err != nil {
					t.Fatal(err)
				}
				if len(runtime.calls) != 1 || <-runtime.calls != "opening:"+sessionID {
					t.Fatal("opening did not reach owner exactly once")
				}
				if retry, err := createSession(queueStore, ctx, sessionID, sandbox.Name, uid, spec, nil, nil); err != nil || retry.State != test.openingState {
					t.Fatalf("opening receipt retry: %+v %v", retry, err)
				} else if test.openingState == store.SessionUnknownState && (!strings.HasPrefix(retry.Reason, "session_materialization_unconfirmed: ") || !strings.Contains(retry.Reason, "test owner response")) {
					t.Fatalf("uncertain Session reason lost owner evidence: %q", retry.Reason)
				}
				if test.openingState == store.FailedState || test.openingState == store.UnknownState {
					if _, err := enqueueWithSession(t, queueStore, ctx, sessionID, sandbox.Name, uid, input(uuid.NewString(), "after owner response"), nil); !errors.Is(err, store.ErrSessionUnconfirmed) || status.Code(apiError(err)) != codes.FailedPrecondition {
						t.Fatalf("new command accepted after terminal opening: %v", err)
					}
					deliveryOwner := claimSessionOwner(t, queueStore, sessionID)
					if _, err := queueStore.RecoverSession(ctx, deliveryOwner); err != nil {
						t.Fatal(err)
					}
					if err := queueStore.ReleaseSessionLease(ctx, deliveryOwner); err != nil {
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
				row, err := queueStore.GetDispatch(ctx, commandID)
				if err != nil || row.State != test.commandState {
					t.Fatalf("dependent command: %+v %v", row, err)
				}
				if retry, err := enqueueWithSession(t, queueStore, ctx, sessionID, sandbox.Name, uid, command, nil); err != nil || retry.State != row.State || retry.Position != row.Position {
					t.Fatalf("existing command receipt retry: %+v %v", retry, err)
				}
				if test.commandState == store.FailedState && row.Reason != "session_rejected" {
					t.Fatalf("unsent rejection reason: %s", row.Reason)
				}
				if test.openingState == store.UnknownState && row.Reason != "session_unconfirmed" {
					t.Fatalf("uncertain opening blocker: %s", row.Reason)
				}
				if len(wake.targets) != 0 {
					t.Fatal("unconfirmed opening attempted wake")
				}
				if test.commandState == store.AdmittedState {
					if len(runtime.calls) != 1 || <-runtime.calls != commandID || row.Admission == nil {
						t.Fatal("admitted opening did not release its command")
					}
				} else if len(runtime.calls) != 0 || row.Admission != nil {
					t.Fatal("unconfirmed opening delivered or replayed work")
				}
			})
		}
	}
}

func TestUnprovenSessionFailureHoldsCommandsAndPreservesOtherPaths(t *testing.T) {
	for _, kind := range []string{"native", "central"} {
		t.Run(kind, func(t *testing.T) {
			ctx := t.Context()
			queueStore := testStore(t, 3)
			sessionID, uid, commandID := uuid.NewString(), uuid.NewString(), uuid.NewString()
			sandbox := SandboxSnapshot{Name: "test-unproven", Namespace: "test-namespace", UID: uid, Incarnation: "test-incarnation"}
			spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: uid}, Harness: sessionpb.Harness_HARNESS_CODEX}}}}
			if kind == "central" {
				spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: uid}}}}
			}
			if _, err := createSession(queueStore, ctx, sessionID, sandbox.Name, uid, spec, nil, nil); err != nil {
				t.Fatal(err)
			}
			if _, err := enqueueWithSession(t, queueStore, ctx, sessionID, sandbox.Name, uid, input(commandID, "pending"), nil); err != nil {
				t.Fatal(err)
			}
			runtime := &acceptingRuntime{calls: make(chan string, 1)}
			worker := &DeliveryWorker{Store: queueStore, Catalog: testCatalog{sandbox: sandbox}, Delivery: runtime}
			worker.init()
			defer worker.queue.ShutDown()
			if err := worker.reconcile(ctx); err != nil {
				t.Fatal(err)
			}
			deliveryOwner := claimSessionOwner(t, queueStore, sessionID)
			if _, err := queueStore.SettleSession(ctx, sessionID, deliveryOwner, []string{store.SessionWaitingState}, store.SessionFailedState, "old unproven outcome"); err != nil {
				t.Fatal(err)
			}
			if err := queueStore.ReleaseSessionLease(ctx, deliveryOwner); err != nil {
				t.Fatal(err)
			}
			if err := worker.processSession(ctx, sessionID); err != nil {
				t.Fatal(err)
			}
			row, err := queueStore.GetDispatch(ctx, commandID)
			if err != nil {
				t.Fatal(err)
			}
			if row.State != store.QueueState || row.Reason != "session_unconfirmed" || len(runtime.calls) != 0 {
				t.Fatalf("unproven failure did not hold work: %+v", row)
			}
			if _, err := queueStore.CancelForCaller(ctx, commandID, testStoreCaller(nil)); err != nil {
				t.Fatalf("held command cannot be cancelled: %v", err)
			}
		})
	}
}

func TestEveryCommandAdmissionRequiresSession(t *testing.T) {
	ctx := admitted(t)
	queueStore := testStore(t, 3)
	service := &Service{Store: queueStore, DispatcherServices: sets.New(peertls.AppURI), Sandboxes: oneSandbox{}}
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
	identity := serviceAccountIdentityFor(auth.Account(caller), "test-account-uid")
	admission := store.ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: identity, Attribution: &store.AdmissionAttribution{Actor: identity, EffectiveCaller: identity}}
	if _, err := queueStore.EnqueueForCaller(ctx, "missing", "sandbox-api", sandboxUID, input(uuid.NewString(), "input"), nil, admission); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("store missing Session: %v", err)
	}
	if rows, err := queueStore.ListDispatchesForCaller(ctx, testSessionDispatchFilter("missing"), testStoreCaller(nil)); err != nil || len(rows) != 0 {
		t.Fatalf("missing Session left work: %+v %v", rows, err)
	}
}

func TestPrivilegedCommandsCannotCrossSessionPlacement(t *testing.T) {
	ctx := admitted(t)
	queueStore := testStore(t, 3)
	sessionID := uuid.NewString()
	if _, err := createSession(queueStore, ctx, sessionID, "different-sandbox", uuid.NewString(), &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	service := &Service{Store: queueStore, DispatcherServices: sets.New(peertls.AppURI), Sandboxes: oneSandbox{}}
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
	if rows, err := queueStore.ListDispatchesForCaller(ctx, testSessionDispatchFilter(sessionID), testStoreCaller(nil)); err != nil || len(rows) != 0 {
		t.Fatalf("cross-placement command left work: %+v %v", rows, err)
	}
}

func TestCommandAdmissionRechecksSessionAfterConcurrentDeletion(t *testing.T) {
	for _, privileged := range []bool{false, true} {
		t.Run(fmt.Sprint(privileged), func(t *testing.T) {
			queueStore, databaseURL := testStoreWithURL(t, 3)
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			var admission store.Caller
			if privileged {
				identity := serviceAccountIdentityFor(auth.Account(caller), "test-owner-uid")
				admission = store.ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: identity, Attribution: &store.AdmissionAttribution{Actor: identity, EffectiveCaller: identity}}
			} else {
				admission = store.IdentityCaller{Identity: testExecutionIdentity(), Proof: store.IdentitySVID{}}
			}
			sessionID := uuid.NewString()
			if _, err := createSession(queueStore, ctx, sessionID, "test-deletion", sandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
				t.Fatal(err)
			}
			// Simulate the API's successful read before the concurrent delete.
			if _, err := queueStore.GetSessionForCaller(ctx, sessionID, admission); err != nil {
				t.Fatal(err)
			}
			deletion, err := queueStore.DB.BeginTx(ctx, nil)
			if err != nil {
				t.Fatal(err)
			}
			defer deletion.Rollback()
			if _, err := deletion.ExecContext(ctx, "DELETE FROM session WHERE session_id=$1", sessionID); err != nil {
				t.Fatal(err)
			}
			deletionPID := postgres.BackendPID(t, ctx, deletion)
			db, err := sql.Open("postgres", databaseURL)
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			db.SetMaxOpenConns(1)
			admissionPID := postgres.BackendPID(t, ctx, db)
			result := make(chan error, 1)
			go func() {
				_, err := store.NewStore(db, 3).EnqueueForCaller(ctx, sessionID, "test-deletion", sandboxUID, input(uuid.NewString(), "input"), nil, admission)
				result <- err
			}()
			postgres.WaitForBlocker(t, ctx, queueStore.DB, admissionPID, deletionPID)
			if err := deletion.Commit(); err != nil {
				t.Fatal(err)
			}
			if err := <-result; !errors.Is(err, store.ErrNotFound) {
				t.Fatalf("deleted Session admitted command: %v", err)
			}
			if rows, err := queueStore.ListDispatchesForCaller(ctx, testSessionDispatchFilter(sessionID), testStoreCaller(nil)); err != nil || len(rows) != 0 {
				t.Fatalf("deleted Session left work: %+v %v", rows, err)
			}
		})
	}
}

type deletingSessionInventory struct {
	oneSandbox
	queueStore *store.Store
	sessionID  string
	identity   *identitypb.Identity
	calls      int
}

func (i *deletingSessionInventory) GetExecutionIdentity(ctx context.Context, _ SandboxSnapshot) (*identitypb.Identity, bool, error) {
	i.calls++
	_, err := i.queueStore.DB.ExecContext(ctx, "DELETE FROM session WHERE session_id=$1", i.sessionID)
	return i.identity, true, err
}

func TestInterruptRechecksSessionAfterLiveAuthorization(t *testing.T) {
	api := kubeapiserver.StartForTest(t)
	const policyNamespace = "interrupt-recheck-policies"
	workload := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, "interrupt-recheck", policyNamespace)
	svids, err := auth.NewSVIDResolver(workload.Objects, []string{caller.Namespace}, &resolver.Identities{PolicyNamespace: policyNamespace})
	if err != nil {
		t.Fatal(err)
	}
	ctx := captureAdmission(t, auth.AnySubject(svids), workload.URI)
	queueStore := testStore(t, 3)
	identity := &identitypb.Identity{Name: workload.Identity.Name, Uid: string(workload.Identity.UID)}
	admission := store.IdentityCaller{Identity: identity, Proof: store.IdentitySVID{}}
	sessionID := uuid.NewString()
	spec := &sessionpb.SessionSpec{}
	testPlacement(spec, "sandbox-api", sandboxUID)
	if _, err := queueStore.CreateSessionWithPlacementForCaller(ctx, sessionID, "sandbox-api", sandboxUID, spec, nil, nil, store.SessionExecution{Identity: identity}, admission); err != nil {
		t.Fatal(err)
	}
	inventory := &deletingSessionInventory{queueStore: queueStore, sessionID: sessionID, identity: identity}
	service := &Service{Store: queueStore, Sandboxes: inventory}
	if _, err := service.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{SessionId: sessionID, ExpectedSandboxUid: sandboxUID, CommandId: "deleted-interrupt", TurnId: "turn"}); status.Code(err) != codes.NotFound {
		t.Fatalf("deleted Session interrupt: %v", err)
	}
	if inventory.calls != 1 {
		t.Fatalf("live Identity checks = %d, want one", inventory.calls)
	}
	if _, err := queueStore.GetDispatch(ctx, "deleted-interrupt"); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("deleted Session admitted interrupt: %v", err)
	}
}

func TestQueuedSessionAllowsNotificationWakeWithoutBorrowingCaller(t *testing.T) {
	ctx := t.Context()
	wake := &scriptedWake{}
	fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{}, wake)
	wake.assessments = append(wake.assessments, &environmentpb.WakeAssessment{State: environmentpb.WakeState_WAKE_STATE_DENIED, Reason: environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED})
	notification := serviceAccountIdentityFor(auth.Account{Namespace: "test-notifications", Name: "notification-service"}, "notification-uid")
	if _, err := fixture.queueStore.DB.ExecContext(ctx, "UPDATE dispatch SET actor_account_namespace=$2,actor_account_name=$3,actor_account_uid=$4,effective_caller_account_namespace=$2,effective_caller_account_name=$3,effective_caller_account_uid=$4 WHERE command_id=$1", fixture.dispatch.CommandID, notification.GetAccount().GetNamespace(), notification.GetAccount().GetName(), notification.GetUid()); err != nil {
		t.Fatal(err)
	}
	wake.assessments = append(wake.assessments, &environmentpb.WakeAssessment{State: environmentpb.WakeState_WAKE_STATE_REQUESTED, ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(fixture.sandbox.WakeGeneration + 1)})
	catalog := &growingCatalog{sandboxes: []SandboxSnapshot{fixture.sandbox}}
	if err := fixture.queueStore.ReleaseSessionLease(ctx, fixture.owner); err != nil {
		t.Fatal(err)
	}
	runtime := &acceptingRuntime{calls: make(chan string, 2)}
	worker := &DeliveryWorker{Store: fixture.queueStore, Wake: wake, Catalog: catalog, Delivery: runtime}
	worker.init()
	defer worker.queue.ShutDown()
	if err := worker.reconcile(ctx); err != nil {
		t.Fatal(err)
	}
	if err := worker.processSession(ctx, fixture.opening.SessionID); err != nil {
		t.Fatal(err)
	}
	if len(wake.callers) == 0 || !proto.Equal(wake.callers[len(wake.callers)-1], notification) {
		t.Fatalf("downstream wake used wrong caller: %+v", wake.callers)
	}
	row, err := fixture.queueStore.GetDispatch(ctx, fixture.dispatch.CommandID)
	if err != nil || row.State != store.QueueState || row.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_REQUESTED || len(runtime.calls) != 0 {
		t.Fatalf("wake changed delivery ordering: %+v %v", row, err)
	}
	catalog.sandboxes[0].Blocker = ""
	for range 2 {
		if err := worker.processSession(ctx, fixture.opening.SessionID); err != nil {
			t.Fatal(err)
		}
	}
	if len(runtime.calls) != 2 || <-runtime.calls != "opening:"+fixture.opening.SessionID || <-runtime.calls != fixture.dispatch.CommandID {
		t.Fatal("command did not follow opening admission")
	}
}

func TestSessionWakeCannotBypassCancelledCommandAssessmentAfterRestart(t *testing.T) {
	for _, state := range []environmentpb.WakeState{environmentpb.WakeState_WAKE_STATE_REQUESTED, environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN, environmentpb.WakeState_WAKE_STATE_RATE_LIMITED} {
		t.Run(state.String(), func(t *testing.T) {
			ctx := t.Context()
			wake := &scriptedWake{}
			fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{}, wake)
			assessment := &environmentpb.WakeAssessment{State: state, TargetGeneration: proto.Int64(fixture.sandbox.WakeGeneration), ObservedAt: timestamppb.Now(), RetryAt: timestamppb.New(time.Now().Add(time.Hour))}
			if queued, err := fixture.queueStore.NoteWaking(ctx, fixture.dispatch.CommandID, fixture.owner, nil, false, assessment); err != nil || !queued {
				t.Fatalf("record wake ownership: %v %v", queued, err)
			}
			if err := fixture.queueStore.SetDeliveryAssessment(ctx, fixture.dispatch.CommandID, fixture.owner, false, nil, assessment); err != nil {
				t.Fatal(err)
			}
			if _, err := fixture.queueStore.CancelForCaller(ctx, fixture.dispatch.CommandID, testStoreCaller(nil)); err != nil {
				t.Fatal(err)
			}
			if _, err := fixture.queueStore.EnqueueForCaller(ctx, fixture.opening.SessionID, fixture.sandbox.Name, fixture.sandbox.UID, input(uuid.NewString(), "later authorized notice"), nil, store.ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: fixture.caller, Attribution: admissionAttribution(fixture.dispatch.Attribution)}); err != nil {
				t.Fatal(err)
			}
			for range 2 {
				worker := &DeliveryWorker{Store: store.NewStore(fixture.queueStore.DB, 3), Wake: wake}
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
	queueStore *store.Store
	commandID  string
	calls      int
}

func (w *cancelDuringWake) RequestWake(ctx context.Context, _ *lifecycle.Target, _ lifecycle.WakeCaller) (*environmentpb.WakeAssessment, error) {
	w.calls++
	if _, err := w.queueStore.CancelForCaller(ctx, w.commandID, testStoreCaller(nil)); err != nil {
		return nil, err
	}
	return nil, status.Error(codes.Unavailable, "uncertain wake result")
}

func TestCancellationDuringWakeRetainsUncertaintyForSession(t *testing.T) {
	ctx := t.Context()
	wake := &cancelDuringWake{}
	fixture := newWakeWorkerFixture(t, &sessionpb.SessionSpec{}, wake)
	wake.queueStore, wake.commandID = fixture.queueStore, fixture.dispatch.CommandID
	if ready, err := fixture.worker.prepareDispatch(ctx, fixture.dispatch, fixture.sandbox, fixture.owner); err != nil || ready {
		t.Fatalf("cancel during wake: %v %v", ready, err)
	}
	row, err := fixture.queueStore.GetDispatch(ctx, fixture.dispatch.CommandID)
	if err != nil || row.State != store.CancelledState || row.WakeAssessment.GetState() != environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN || row.WakeAssessment.GetTargetGeneration() != fixture.sandbox.WakeGeneration {
		t.Fatalf("lost in-flight wake marker: %+v %v", row, err)
	}
	if _, err := fixture.queueStore.EnqueueForCaller(ctx, fixture.opening.SessionID, fixture.sandbox.Name, fixture.sandbox.UID, input(uuid.NewString(), "later notice"), nil, store.ServiceAccountCaller{Privileged: true, ServiceAccountIdentity: fixture.caller, Attribution: admissionAttribution(fixture.dispatch.Attribution)}); err != nil {
		t.Fatal(err)
	}
	worker := &DeliveryWorker{Store: store.NewStore(fixture.queueStore.DB, 3), Wake: wake}
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
	opener  *identitypb.ServiceAccountIdentity
	allowed bool
	callers []*identitypb.ServiceAccountIdentity
}

func (w *changingWakeGrant) RequestWake(ctx context.Context, target *lifecycle.Target, caller lifecycle.WakeCaller) (*environmentpb.WakeAssessment, error) {
	w.callers = append(w.callers, wakeAccount(caller))
	if proto.Equal(wakeAccount(caller), w.opener) {
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
	wake.queueStore, wake.commandID, wake.opener = fixture.queueStore, fixture.dispatch.CommandID, fixture.caller
	notification := serviceAccountIdentityFor(auth.Account{Namespace: "test-notifications", Name: "notification-service"}, "notification-uid")
	if _, err := fixture.queueStore.DB.ExecContext(ctx, "UPDATE dispatch SET actor_account_namespace=$2,actor_account_name=$3,actor_account_uid=$4,effective_caller_account_namespace=$2,effective_caller_account_name=$3,effective_caller_account_uid=$4 WHERE command_id=$1", fixture.dispatch.CommandID, notification.GetAccount().GetNamespace(), notification.GetAccount().GetName(), notification.GetUid()); err != nil {
		t.Fatal(err)
	}
	if ready, err := fixture.worker.prepareSession(ctx, fixture.opening, fixture.sandbox, fixture.owner); err != nil || ready {
		t.Fatalf("initial denied opening/notice wake: %v %v", ready, err)
	}
	if len(wake.callers) != 2 || !proto.Equal(wake.callers[0], fixture.caller) || !proto.Equal(wake.callers[1], notification) || !wake.allowed {
		t.Fatalf("wake attribution/grant transition: %+v", wake.callers)
	}
	for range 2 {
		worker := &DeliveryWorker{Store: store.NewStore(fixture.queueStore.DB, 3), Wake: wake}
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
	opening, err := fixture.queueStore.GetSession(ctx, fixture.opening.SessionID)
	if err != nil {
		t.Fatal(err)
	}
	if ready, err := fixture.worker.prepareSession(ctx, opening, fixture.sandbox, fixture.owner); err != nil || ready {
		t.Fatalf("reassess after observed generation change: %v %v", ready, err)
	}
	if len(wake.callers) != 3 || !proto.Equal(wake.callers[2], fixture.caller) {
		t.Fatalf("later generation did not reassess opening caller: %+v", wake.callers)
	}
}

// The external boundary observes the committed marker before returning a lost
// admission reply. Requeue and recovery cannot turn that attempt into unsent work.
type attemptProbeRuntime struct {
	*retryRuntime
	before func()
}

func (r *attemptProbeRuntime) Deliver(ctx context.Context, sandbox SandboxSnapshot, sessionID string, command *commandpb.Command) (*eventlogpb.EventEntry, error) {
	r.before()
	return r.retryRuntime.Deliver(ctx, sandbox, sessionID, command)
}

func TestDeletionAfterLostCommandReplyRemainsUnknown(t *testing.T) {
	ctx := t.Context()
	queueStore := testStore(t, 3)
	head, err := enqueueWithSession(t, queueStore, ctx, "lost-reply-session", "sandbox-worker", sandboxUID, input("worker-input", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	deliveryOwner := claimSessionOwner(t, queueStore, head.SessionID)
	session, err := queueStore.GetSession(ctx, head.SessionID)
	if err != nil {
		t.Fatal(err)
	}
	worker := &DeliveryWorker{Store: queueStore, Catalog: testCatalog{}, Delivery: &attemptProbeRuntime{retryRuntime: &retryRuntime{}, before: func() {
		row, err := queueStore.GetDispatch(ctx, head.CommandID)
		if err != nil || !row.DeliveryAttempted || row.State != store.DeliveringState {
			t.Fatalf("RPC preceded durable marker: %+v, %v", row, err)
		}
	}}}
	worker.init()
	defer worker.queue.ShutDown()
	if !worker.deliverCommand(ctx, head, SandboxSnapshot{Name: "sandbox-worker", UID: sandboxUID, Incarnation: "pod-1"}, deliveryOwner, session) {
		t.Fatal("worker did not attempt delivery")
	}
	queued, err := queueStore.GetDispatch(ctx, head.CommandID)
	if err != nil || queued.State != store.QueueState || !queued.DeliveryAttempted {
		t.Fatalf("lost reply requeue: %+v, %v", queued, err)
	}
	if _, err := queueStore.CancelForCaller(ctx, head.CommandID, testStoreCaller(nil)); !errors.Is(err, store.ErrNotCancellable) {
		t.Fatalf("uncertain attempt cancelled as unsent: %v", err)
	}
	if _, err := queueStore.RecoverSession(ctx, deliveryOwner); err != nil {
		t.Fatal(err)
	}
	if err := queueStore.EndSandbox(ctx, sandboxUID); err != nil {
		t.Fatal(err)
	}
	var state string
	if err := queueStore.DB.QueryRowContext(ctx, "SELECT state FROM dispatch_tombstone WHERE command_id=$1", head.CommandID).Scan(&state); err != nil {
		t.Fatal(err)
	}
	if state != store.UnknownState {
		t.Fatalf("deletion after lost reply = %s, want %s", state, store.UnknownState)
	}
}

func TestUncertainAttemptCannotBecomeKnownFailedOnRetryRefusal(t *testing.T) {
	ctx := t.Context()
	queueStore := testStore(t, 3)
	head, err := enqueueWithSession(t, queueStore, ctx, "retry-refusal-session", "sandbox-worker", sandboxUID, input("retry-refusal-input", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	deliveryOwner := claimSessionOwner(t, queueStore, head.SessionID)
	if _, err := queueStore.Settle(ctx, head.CommandID, deliveryOwner, []string{store.QueueState}, store.DeliveringState, "", nil); err != nil {
		t.Fatal(err)
	}
	if marked, err := queueStore.MarkDeliveryAttempt(ctx, head.CommandID, deliveryOwner); err != nil || !marked {
		t.Fatalf("marker: %t, %v", marked, err)
	}
	if _, err := queueStore.Settle(ctx, head.CommandID, deliveryOwner, []string{store.DeliveringState}, store.QueueState, "lost_reply", nil); err != nil {
		t.Fatal(err)
	}
	head, err = queueStore.GetDispatch(ctx, head.CommandID)
	if err != nil {
		t.Fatal(err)
	}
	session, err := queueStore.GetSession(ctx, head.SessionID)
	if err != nil {
		t.Fatal(err)
	}
	worker := &DeliveryWorker{Store: queueStore, Catalog: testCatalog{}, Delivery: &rejectingRuntime{err: status.Error(codes.FailedPrecondition, "receiver sealed")}}
	worker.init()
	defer worker.queue.ShutDown()
	if !worker.deliverCommand(ctx, head, SandboxSnapshot{UID: sandboxUID}, deliveryOwner, session) {
		t.Fatal("retry did not run")
	}
	row, err := queueStore.GetDispatch(ctx, head.CommandID)
	if err != nil || row == nil || row.State != store.UnknownState {
		t.Fatalf("retry refusal overwrote uncertain result: %+v, %v", row, err)
	}
}

func TestFirstCommandAttemptDoesNotInferNonAdmissionFromRPCCode(t *testing.T) {
	for _, test := range []struct {
		code codes.Code
		want string
	}{
		{codes.Canceled, store.UnknownState},
		{codes.Unknown, store.UnknownState},
		{codes.Internal, store.UnknownState},
		{codes.ResourceExhausted, store.UnknownState},
		{codes.FailedPrecondition, store.UnknownState},
	} {
		t.Run(test.code.String(), func(t *testing.T) {
			ctx := t.Context()
			queueStore := testStore(t, 3)
			head, err := enqueueWithSession(t, queueStore, ctx, "first-attempt-session", "sandbox-worker", sandboxUID, input("first-attempt-input", "hello"), nil)
			if err != nil {
				t.Fatal(err)
			}
			deliveryOwner := claimSessionOwner(t, queueStore, head.SessionID)
			// The fake external owner commits before returning an error code.
			runtime := &rejectingRuntime{err: status.Error(test.code, "response unavailable"), admitted: map[string]bool{}}
			session, err := queueStore.GetSession(ctx, head.SessionID)
			if err != nil {
				t.Fatal(err)
			}
			worker := &DeliveryWorker{Store: queueStore, Catalog: testCatalog{}, Delivery: runtime}
			worker.init()
			defer worker.queue.ShutDown()
			if !worker.deliverCommand(ctx, head, SandboxSnapshot{UID: sandboxUID}, deliveryOwner, session) {
				t.Fatal("worker did not attempt delivery")
			}
			if !runtime.admitted[head.CommandID] {
				t.Fatal("external admission was not exercised")
			}
			row, err := queueStore.GetDispatch(ctx, head.CommandID)
			if err != nil || row.State != test.want {
				t.Fatalf("first attempt result: %+v, %v; want %s", row, err, test.want)
			}
		})
	}
}

func TestWakeAssessmentRoundTripsForDispatchAndSession(t *testing.T) {
	ctx := context.Background()
	queueStore := testStore(t, 3)
	uid := uuid.NewString()
	dispatch, err := enqueueWithSession(t, queueStore, ctx, "wake-receipt-session", "sandbox-wake-receipt", uid,
		input("wake-receipt-command", "hello"), nil)
	if err != nil {
		t.Fatal(err)
	}
	opening, err := createSession(queueStore, ctx, "wake-receipt-opening", "sandbox-wake-receipt", uid,
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
	dispatchOwner := claimSessionOwner(t, queueStore, dispatch.SessionID)
	openingOwner := claimSessionOwner(t, queueStore, opening.SessionID)
	blocker := &environmentpb.DeliveryBlocker{Kind: environmentpb.DeliveryBlockerKind_DELIVERY_BLOCKER_KIND_ENVIRONMENT_SUSPENDED}
	assessment := &environmentpb.WakeAssessment{
		State:      environmentpb.WakeState_WAKE_STATE_DENIED,
		Reason:     environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED,
		ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(7),
	}
	if err := queueStore.SetDeliveryAssessment(ctx, dispatch.CommandID, dispatchOwner, false, blocker, assessment); err != nil {
		t.Fatal(err)
	}
	if err := queueStore.SetDeliveryAssessment(ctx, opening.SessionID, openingOwner, true, blocker, assessment); err != nil {
		t.Fatal(err)
	}
	gotDispatch, err := queueStore.GetDispatch(ctx, dispatch.CommandID)
	if err != nil {
		t.Fatal(err)
	}
	rows, err := queueStore.ListDispatchesForCaller(ctx, testSessionDispatchFilter(dispatch.SessionID), testStoreCaller(nil))
	if err != nil || len(rows) != 1 {
		t.Fatalf("list dispatches: %d rows, %v", len(rows), err)
	}
	gotSession, err := queueStore.GetSession(ctx, opening.SessionID)
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
	wireDispatch, err := store.DescribeDispatch(gotDispatch, true)
	if err != nil || !proto.Equal(wireDispatch.GetWakeAssessment(), assessment) || !proto.Equal(wireDispatch.GetDeliveryBlocker(), blocker) {
		t.Fatalf("dispatch projection: %+v %v", wireDispatch, err)
	}
	wireSession, err := DescribeSession(gotSession)
	if err != nil || !proto.Equal(wireSession.GetStatus().GetWakeAssessment(), assessment) || !proto.Equal(wireSession.GetStatus().GetDeliveryBlocker(), blocker) {
		t.Fatalf("opening projection: %+v %v", wireSession, err)
	}
}
