package dispatcher

import (
	"context"
	"errors"
	"runtime"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/internal/resumetoken"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/apimachinery/pkg/util/sets"
)

type sessionWatchTestStream struct {
	grpc.ServerStream
	ctx    context.Context
	frames chan *dispatcherpb.WatchSessionsResponse
}

func (s *sessionWatchTestStream) Context() context.Context { return s.ctx }

func (s *sessionWatchTestStream) Send(frame *dispatcherpb.WatchSessionsResponse) error {
	select {
	case s.frames <- frame:
		return nil
	case <-s.ctx.Done():
		return s.ctx.Err()
	}
}

type sessionWatchRun struct {
	stream *sessionWatchTestStream
	cancel context.CancelFunc
	done   chan error
}

func startSessionWatch(t *testing.T, service *Service, ctx context.Context, token string) *sessionWatchRun {
	t.Helper()
	streamCtx, cancel := context.WithCancel(ctx)
	run := &sessionWatchRun{
		stream: &sessionWatchTestStream{ctx: streamCtx, frames: make(chan *dispatcherpb.WatchSessionsResponse, 16)},
		cancel: cancel,
		done:   make(chan error, 1),
	}
	go func() {
		defer close(run.done)
		run.done <- service.WatchSessions(&dispatcherpb.WatchSessionsRequest{ResumeToken: token}, run.stream)
	}()
	t.Cleanup(func() {
		run.cancel()
		select {
		case <-run.done:
		case <-time.After(5 * time.Second):
			t.Error("WatchSessions did not stop after cancellation")
		}
	})
	return run
}

func receiveSessionWatchFrame(t *testing.T, run *sessionWatchRun) *dispatcherpb.WatchSessionsResponse {
	t.Helper()
	select {
	case frame := <-run.stream.frames:
		return frame
	default:
	}
	select {
	case frame := <-run.stream.frames:
		return frame
	case err := <-run.done:
		select {
		case frame := <-run.stream.frames:
			return frame
		default:
		}
		t.Fatalf("WatchSessions ended before the next frame: %v", err)
	case <-time.After(10 * time.Second):
		t.Fatal("timed out waiting for WatchSessions frame")
	}
	return nil
}

func receiveCaughtUp(t *testing.T, run *sessionWatchRun) *dispatcherpb.WatchSessionsResponse {
	t.Helper()
	frame := receiveSessionWatchFrame(t, run)
	if _, ok := frame.GetFrame().(*dispatcherpb.WatchSessionsResponse_CaughtUp); !ok {
		t.Fatalf("frame = %T, want CaughtUp", frame.GetFrame())
	}
	return frame
}

func sessionWatchHub(t *testing.T, databaseURL, name string) *wakeups.Hub {
	t.Helper()
	parsed := postgres.URLWithApplicationName(t, databaseURL, name)
	hub, err := wakeups.Listen(t.Context(), parsed.String(), store.SessionNotifyChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = hub.Close() })
	return hub
}

func sessionWatchService(queueStore *store.Store, hub *wakeups.Hub) *Service {
	return &Service{
		Store: queueStore, DispatcherServices: sets.New(peertls.AppURI), Hub: hub, WatchLease: time.Minute,
	}
}

func TestWatchSessionsResumesAfterStreamDisconnect(t *testing.T) {
	queueStore, databaseURL := testStoreWithURL(t, 3)
	ctx := admitted(t)
	hub := sessionWatchHub(t, databaseURL, "session-watch-resume-"+uuid.NewString())
	service := sessionWatchService(queueStore, hub)
	id := uuid.NewString()
	if _, err := createSession(queueStore, ctx, id, "test-watch-resume", uuid.NewString(), &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}

	first := startSessionWatch(t, service, ctx, "")
	initial := receiveSessionWatchFrame(t, first)
	if initial.GetSession().GetSessionId() != id {
		t.Fatalf("initial Session = %v, want %s", initial.GetSession(), id)
	}
	checkpoint := receiveCaughtUp(t, first).GetResumeToken()
	if checkpoint == "" {
		t.Fatal("CaughtUp did not include a resume token")
	}
	first.cancel()
	if err := <-first.done; !errors.Is(err, context.Canceled) {
		t.Fatalf("disconnected stream ended with %v, want Canceled", err)
	}

	owner := claimSessionOwner(t, queueStore, id)
	if _, err := queueStore.SettleSession(ctx, id, owner, []string{store.SessionWaitingState}, store.SessionAcceptedState, "materialized"); err != nil {
		t.Fatal(err)
	}
	resumed := startSessionWatch(t, service, ctx, checkpoint)
	update := receiveSessionWatchFrame(t, resumed).GetSession()
	if update.GetSessionId() != id || update.GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_ACCEPTED {
		t.Fatalf("resumed Session = %v", update)
	}
	if receiveCaughtUp(t, resumed).GetResumeToken() == checkpoint {
		t.Fatal("resume token did not advance after the Session update")
	}
}

func TestWatchSessionsReportsMaterializationAndEnvironmentDeletion(t *testing.T) {
	queueStore, databaseURL := testStoreWithURL(t, 3)
	ctx := admitted(t)
	hub := sessionWatchHub(t, databaseURL, "session-watch-deletion-"+uuid.NewString())
	service := sessionWatchService(queueStore, hub)
	id, failedID, environmentUID := uuid.NewString(), uuid.NewString(), uuid.NewString()
	for _, sessionID := range []string{id, failedID} {
		if _, err := createSession(queueStore, ctx, sessionID, "test-watch-deletion", environmentUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
			t.Fatal(err)
		}
	}
	owner, failedOwner := claimSessionOwner(t, queueStore, id), claimSessionOwner(t, queueStore, failedID)
	run := startSessionWatch(t, service, ctx, "")
	for range 2 {
		initial := receiveSessionWatchFrame(t, run).GetSession()
		if initial.GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_WAITING {
			t.Fatalf("initial status = %v", initial.GetStatus())
		}
	}
	_ = receiveCaughtUp(t, run)
	if _, err := queueStore.SettleSession(ctx, id, owner, []string{store.SessionWaitingState}, store.SessionInProgressState, "starting"); err != nil {
		t.Fatal(err)
	}
	progress := receiveSessionWatchFrame(t, run).GetSession()
	if progress.GetSessionId() != id || progress.GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_IN_PROGRESS {
		t.Fatalf("materialization update = %v", progress.GetStatus())
	}
	if _, err := queueStore.SettleSession(ctx, failedID, failedOwner, []string{store.SessionWaitingState}, store.SessionFailedState, "rejected"); err != nil {
		t.Fatal(err)
	}
	failed := receiveSessionWatchFrame(t, run).GetSession()
	if failed.GetSessionId() != failedID || failed.GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_FAILED {
		t.Fatalf("failed materialization update = %v", failed.GetStatus())
	}
	if err := queueStore.EndSandbox(ctx, environmentUID); err != nil {
		t.Fatal(err)
	}
	deleted := map[string]*dispatcherpb.Session{}
	for range 2 {
		item := receiveSessionWatchFrame(t, run).GetSession()
		deleted[item.GetSessionId()] = item
	}
	for sessionID, wantState := range map[string]dispatcherpb.SessionStatus_Materialization{
		id:       dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_UNKNOWN,
		failedID: dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_FAILED,
	} {
		if got := deleted[sessionID]; got == nil || got.GetStatus().GetMaterialization() != wantState ||
			got.GetStatus().GetRequiredEnvironment() != dispatcherpb.SessionStatus_REQUIRED_ENVIRONMENT_DELETED {
			t.Errorf("environment deletion update for %s = %v, want materialization %v and deleted Environment", sessionID, got, wantState)
		}
	}
}

func TestWatchSessionsWakesAcrossReplicaHubs(t *testing.T) {
	queueStore, databaseURL := testStoreWithURL(t, 3)
	ctx := admitted(t)
	hubA := sessionWatchHub(t, databaseURL, "session-watch-replica-a-"+uuid.NewString())
	hubB := sessionWatchHub(t, databaseURL, "session-watch-replica-b-"+uuid.NewString())
	runA := startSessionWatch(t, sessionWatchService(queueStore, hubA), ctx, "")
	runB := startSessionWatch(t, sessionWatchService(queueStore, hubB), ctx, "")
	_ = receiveCaughtUp(t, runA)
	_ = receiveCaughtUp(t, runB)
	id := uuid.NewString()
	if _, err := createSession(queueStore, ctx, id, "test-watch-replicas", uuid.NewString(), &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal(err)
	}
	for name, run := range map[string]*sessionWatchRun{"replica A": runA, "replica B": runB} {
		if got := receiveSessionWatchFrame(t, run).GetSession().GetSessionId(); got != id {
			t.Errorf("%s received Session %q, want %q", name, got, id)
		}
	}
}

func TestWatchSessionsReplaysSnapshotAfterListenerReconnect(t *testing.T) {
	queueStore, databaseURL := testStoreWithURL(t, 3)
	ctx := admitted(t)
	applicationName := "session-watch-reconnect-" + uuid.NewString()
	hub := sessionWatchHub(t, databaseURL, applicationName)
	service := sessionWatchService(queueStore, hub)
	ids := []string{uuid.NewString(), uuid.NewString()}
	for _, id := range ids {
		if _, err := createSession(queueStore, ctx, id, "test-watch-reconnect", uuid.NewString(), &sessionpb.SessionSpec{}, nil, nil); err != nil {
			t.Fatal(err)
		}
	}
	run := startSessionWatch(t, service, ctx, "")
	for range ids {
		if _, ok := receiveSessionWatchFrame(t, run).GetFrame().(*dispatcherpb.WatchSessionsResponse_Session); !ok {
			t.Fatal("initial snapshot did not contain a Session")
		}
	}
	_ = receiveCaughtUp(t, run)
	before := hub.Generation()
	var terminated bool
	if err := queueStore.DB.QueryRowContext(ctx, `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name=$1`, applicationName).Scan(&terminated); err != nil || !terminated {
		t.Fatalf("terminate listener connection: terminated=%v err=%v", terminated, err)
	}
	deadline := time.Now().Add(10 * time.Second)
	for hub.Generation() <= before || !hub.Connected() {
		if time.Now().After(deadline) {
			t.Fatal("Session watch listener did not reconnect")
		}
		runtime.Gosched()
	}
	replayed := map[string]bool{}
	for {
		frame := receiveSessionWatchFrame(t, run)
		switch item := frame.GetFrame().(type) {
		case *dispatcherpb.WatchSessionsResponse_Session:
			replayed[item.Session.GetSessionId()] = true
		case *dispatcherpb.WatchSessionsResponse_CaughtUp:
			for _, id := range ids {
				if !replayed[id] {
					t.Fatalf("reconnected stream omitted prior snapshot Session %s: %v", id, replayed)
				}
			}
			return
		}
	}
}

func TestWatchSessionsRejectsFutureAndWrongScopeResumeTokens(t *testing.T) {
	queueStore, databaseURL := testStoreWithURL(t, 3)
	ctx := admitted(t)
	hub := sessionWatchHub(t, databaseURL, "session-watch-token-"+uuid.NewString())
	service := sessionWatchService(queueStore, hub)
	serviceCaller := store.ServiceCaller{SPIFFEID: peertls.AppURI}
	otherCaller := store.IdentityCaller{Identity: &identitypb.Identity{Name: "test-token-owner", Uid: uuid.NewString()}, Proof: store.IdentitySVID{}}
	var high int64
	if err := queueStore.DB.QueryRowContext(ctx, "SELECT version FROM session_watch_version WHERE singleton").Scan(&high); err != nil {
		t.Fatal(err)
	}
	tests := []struct {
		name  string
		token string
	}{
		{name: "wrong owner scope", token: resumetoken.Encode(0, sessionWatchDigest(otherCaller))},
		{name: "future version", token: resumetoken.Encode(high+1, sessionWatchDigest(serviceCaller))},
	}
	for _, test := range tests {
		stream := &sessionWatchTestStream{ctx: ctx, frames: make(chan *dispatcherpb.WatchSessionsResponse, 1)}
		err := service.WatchSessions(&dispatcherpb.WatchSessionsRequest{ResumeToken: test.token}, stream)
		if status.Code(err) != codes.InvalidArgument {
			t.Errorf("%s token returned %v, want InvalidArgument", test.name, err)
		}
	}
}

func TestWatchSessionsRequestsReconnectWhenLeaseExpires(t *testing.T) {
	queueStore, databaseURL := testStoreWithURL(t, 3)
	ctx := admitted(t)
	hub := sessionWatchHub(t, databaseURL, "session-watch-lease-"+uuid.NewString())
	service := sessionWatchService(queueStore, hub)
	service.WatchLease = time.Second
	run := startSessionWatch(t, service, ctx, "")
	_ = receiveCaughtUp(t, run)
	frame := receiveSessionWatchFrame(t, run)
	if _, ok := frame.GetFrame().(*dispatcherpb.WatchSessionsResponse_ReconnectRequired); !ok {
		t.Fatalf("lease expiry frame = %T, want ReconnectRequired", frame.GetFrame())
	}
	if err := <-run.done; err != nil {
		t.Fatalf("WatchSessions lease ended with %v, want clean EOF", err)
	}
}

func TestSessionWatchScopeBindsTokensAndReadsToIdentity(t *testing.T) {
	queueStore := testStore(t, 3)
	ctx := t.Context()
	owner := store.IdentityCaller{Identity: &identitypb.Identity{Name: "test-watch-owner", Uid: uuid.NewString()}, Proof: store.IdentitySVID{}}
	other := store.IdentityCaller{Identity: &identitypb.Identity{Name: "test-watch-other", Uid: uuid.NewString()}, Proof: store.IdentitySVID{}}
	for _, caller := range []store.IdentityCaller{owner, other} {
		id, sandboxUID := uuid.NewString(), uuid.NewString()
		spec := &sessionpb.SessionSpec{}
		testPlacement(spec, "test-watch-identity-"+id, sandboxUID)
		if _, err := queueStore.CreateSessionWithPlacementForCaller(ctx, id, "test-watch-identity-"+id, sandboxUID, spec, nil, nil,
			store.SessionExecution{Identity: caller.Identity}, caller); err != nil {
			t.Fatal(err)
		}
	}
	if sessionWatchDigest(owner) == sessionWatchDigest(other) || sessionWatchKey(owner) == sessionWatchKey(other) {
		t.Fatal("distinct Identity read scopes shared a Session watch token digest or notification key")
	}
	rows, err := queueStore.SessionsAfter(ctx, 0, owner)
	if err != nil || len(rows) != 1 || rows[0].ExecutionIdentity.GetUid() != owner.Identity.GetUid() {
		t.Fatalf("owner watch scope returned %+v, err=%v", rows, err)
	}
}
