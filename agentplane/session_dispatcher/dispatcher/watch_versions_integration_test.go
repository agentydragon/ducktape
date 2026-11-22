package dispatcher

import (
	"context"
	"errors"
	"runtime"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"k8s.io/apimachinery/pkg/util/sets"
)

type versionWatch struct {
	stream *channelWatch
	done   chan error
	cancel context.CancelFunc
}

func startVersionWatch(t *testing.T, ctx context.Context, queueStore *store.Store, hub *wakeups.Hub, filter *dispatcherpb.DispatchFilter, token string) *versionWatch {
	t.Helper()
	ctx, cancel := context.WithCancel(ctx)
	watch := &versionWatch{stream: &channelWatch{ctx: ctx, frames: make(chan *dispatcherpb.WatchDispatchesResponse, 32)}, done: make(chan error, 1), cancel: cancel}
	service := &Service{Store: queueStore, Hub: hub, DispatcherServices: sets.New(peertls.AppURI), WatchLease: time.Minute}
	go func() {
		watch.done <- service.WatchDispatches(&dispatcherpb.WatchDispatchesRequest{Filter: filter, ResumeToken: token}, watch.stream)
	}()
	t.Cleanup(cancel)
	return watch
}

func (w *versionWatch) next(t *testing.T) *dispatcherpb.WatchDispatchesResponse {
	t.Helper()
	select {
	case frame := <-w.stream.frames:
		if frame.GetResumeToken() == "" {
			t.Fatal("watch frame has no resume token")
		}
		return frame
	case err := <-w.done:
		t.Fatalf("watch ended before expected frame: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("watch did not produce expected frame")
	}
	return nil
}

func (w *versionWatch) stop(t *testing.T) {
	t.Helper()
	w.cancel()
	select {
	case err := <-w.done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("watch shutdown: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("watch did not stop")
	}
}

func testSessionDispatchFilter(session string, ids ...string) *dispatcherpb.DispatchFilter {
	return &dispatcherpb.DispatchFilter{Scope: &dispatcherpb.DispatchFilter_SessionId{SessionId: session}, CommandIds: ids}
}

func TestRecentDispatchIssuesReplacesBoundedSnapshot(t *testing.T) {
	ctx := admitted(t)
	queueStore := testStore(t, 5)
	filter := testSessionDispatchFilter("test-recent-issues")
	filter.RecentIssueLimit = 2
	for _, id := range []string{"earlier-input", "first-failure", "second-failure", "third-failure"} {
		if _, err := enqueueWithSession(t, queueStore, ctx, filter.GetSessionId(), "test-issue-sandbox", sandboxUID, input(id, "private test input"), nil); err != nil {
			t.Fatal(err)
		}
	}
	change := func(id, state, reason string) {
		t.Helper()
		if _, err := queueStore.DB.ExecContext(ctx, `WITH changed AS (
			UPDATE dispatch SET state=$2,reason=$3 WHERE command_id=$1 RETURNING session_id
		) SELECT pg_notify($4, session_id) FROM changed`, id, state, reason, store.NotifyChannel); err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []string{"first-failure", "second-failure", "third-failure"} {
		change(id, store.FailedState, "test refusal")
	}
	hub := testWatchHub(t, queueStore)
	watch := startVersionWatch(t, ctx, queueStore, hub, filter, "")
	assertIssues := func(frame *dispatcherpb.WatchDispatchesResponse, clipped bool, want ...string) {
		t.Helper()
		snapshot := frame.GetRecentIssues()
		var ids []string
		for _, row := range snapshot.GetReceipts() {
			ids = append(ids, row.GetCommandId())
		}
		if snapshot == nil || snapshot.GetMoreAvailable() != clipped || !slices.Equal(ids, want) {
			t.Fatalf("snapshot=%v, want clipped=%v receipts=%v", snapshot, clipped, want)
		}
	}
	initial := watch.next(t)
	assertIssues(initial, true, "third-failure", "second-failure")
	watch.stop(t)

	// A command that predated the snapshot becomes uncertain after the browser disconnects.
	change("earlier-input", store.UnknownState, "test call outcome lost")
	resumed := startVersionWatch(t, ctx, queueStore, hub, filter, initial.GetResumeToken())
	current := resumed.next(t)
	assertIssues(current, true, "earlier-input", "third-failure")
	issue := current.GetRecentIssues().GetReceipts()[0]
	if issue.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN || issue.GetReason() != "test call outcome lost" {
		t.Fatalf("uncertain receipt = %v", issue)
	}

	// Simulate a state correction and verify that a replacement removes the old problem.
	change("third-failure", store.CancelledState, "test removed issue")
	assertIssues(resumed.next(t), true, "earlier-input", "second-failure")
	change("second-failure", store.CancelledState, "test removed issue")
	assertIssues(resumed.next(t), false, "earlier-input", "first-failure")
	change("first-failure", store.CancelledState, "test removed issue")
	assertIssues(resumed.next(t), false, "earlier-input")

	resumed.stop(t)
	if err := queueStore.EndSandbox(ctx, sandboxUID); err != nil {
		t.Fatal(err)
	}
	fresh := startVersionWatch(t, ctx, queueStore, hub, filter, "")
	deleted := fresh.next(t)
	assertIssues(deleted, false, "earlier-input")
	if row := deleted.GetRecentIssues().GetReceipts()[0]; row.GetState() != issue.GetState() || row.GetReason() != issue.GetReason() {
		t.Fatalf("tombstone lost its outcome: %v", row)
	}
}

func TestVersionWatchResumesAfterDisconnectAndServiceRestart(t *testing.T) {
	ctx := admitted(t)
	queueStore := testStore(t, 5)
	filter := testSessionDispatchFilter("test-resume-session")
	for _, id := range []string{"test-resume-a", "test-resume-b", "test-resume-c"} {
		if _, err := enqueueWithSession(t, queueStore, ctx, filter.GetSessionId(), "test-resume-sandbox", sandboxUID, input(id, "test-input"), nil); err != nil {
			t.Fatal(err)
		}
	}
	hub := testWatchHub(t, queueStore)
	watch := startVersionWatch(t, ctx, queueStore, hub, filter, "")
	first := watch.next(t)
	if first.GetDispatch().GetCommandId() != "test-resume-a" {
		t.Fatalf("first receipt: %v", first)
	}
	// Disconnect after a receipt, before acknowledging the snapshot checkpoint. A new
	// process must resume from this frame without skipping the rest of the snapshot.
	watch.stop(t)
	resumed := startVersionWatch(t, ctx, store.NewStore(queueStore.DB, 5), testWatchHub(t, queueStore), filter, first.GetResumeToken())
	for _, id := range []string{"test-resume-b", "test-resume-c"} {
		if got := resumed.next(t).GetDispatch().GetCommandId(); got != id {
			t.Fatalf("resumed receipt = %s, want %s", got, id)
		}
	}
	checkpoint := resumed.next(t)
	if checkpoint.GetCaughtUp() == nil {
		t.Fatal("resumed snapshot has no checkpoint")
	}
	resumed.stop(t)
	if _, err := queueStore.CancelForCaller(ctx, "test-resume-b", testStoreCaller(nil)); err != nil {
		t.Fatal(err)
	}
	if err := queueStore.EndSandbox(ctx, sandboxUID); err != nil {
		t.Fatal(err)
	}
	// All source rows are gone before the replacement service starts.
	restarted := startVersionWatch(t, ctx, store.NewStore(queueStore.DB, 5), testWatchHub(t, queueStore), filter, checkpoint.GetResumeToken())
	var ids []string
	var deletionToken string
	for {
		frame := restarted.next(t)
		if frame.GetCaughtUp() != nil {
			deletionToken = frame.GetResumeToken()
			break
		}
		row := frame.GetDispatch()
		ids = append(ids, row.GetCommandId())
		if row.GetSandbox() != "" || row.GetSandboxUid() != "" {
			t.Fatalf("deletion receipt contains a Sandbox address: %v", row)
		}
		want := dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED
		if row.GetCommandId() == "test-resume-b" {
			want = dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER
		}
		if row.GetState() != want {
			t.Fatalf("retained terminal receipt: %v", row)
		}
	}
	slices.Sort(ids)
	if !slices.Equal(ids, []string{"test-resume-a", "test-resume-b", "test-resume-c"}) {
		t.Fatalf("deletion replay = %v", ids)
	}
	restarted.stop(t)
	// A caught-up deletion checkpoint resumes empty even on another replica.
	empty := startVersionWatch(t, ctx, queueStore, hub, filter, deletionToken)
	if empty.next(t).GetCaughtUp() == nil {
		t.Fatal("deletions replayed after their checkpoint")
	}
	empty.stop(t)
	// A fresh legacy caller still sees retained deletion outcomes without sending a token.
	fresh := startVersionWatch(t, ctx, queueStore, hub, &dispatcherpb.DispatchFilter{Scope: &dispatcherpb.DispatchFilter_Sandbox{Sandbox: "test-resume-sandbox"}}, "")
	for range 3 {
		if row := fresh.next(t).GetDispatch(); row == nil || row.GetSandboxUid() != "" {
			t.Fatalf("fresh Sandbox snapshot omitted retained deletion: %v", row)
		}
	}
	if fresh.next(t).GetCaughtUp() == nil {
		t.Fatal("fresh deletion snapshot missing checkpoint")
	}
	fresh.stop(t)
}

func TestVersionWatchResumesOnlyChangedRowsAndRejectsDifferentFilter(t *testing.T) {
	ctx := admitted(t)
	queueStore := testStore(t, 5)
	filter := testSessionDispatchFilter("test-delta-session", "test-delta-a", "test-delta-b")
	for _, id := range filter.GetCommandIds() {
		if _, err := enqueueWithSession(t, queueStore, ctx, filter.GetSessionId(), "test-delta-sandbox", sandboxUID, input(id, "test-input"), nil); err != nil {
			t.Fatal(err)
		}
	}
	hub := testWatchHub(t, queueStore)
	watch := startVersionWatch(t, ctx, queueStore, hub, filter, "")
	watch.next(t)
	watch.next(t)
	token := watch.next(t).GetResumeToken()
	watch.stop(t)
	if _, err := queueStore.CancelForCaller(ctx, "test-delta-b", testStoreCaller(nil)); err != nil {
		t.Fatal(err)
	}
	// Command IDs form a set: reordering and duplicates keep the same digest.
	canonical := testSessionDispatchFilter(filter.GetSessionId(), "test-delta-b", "test-delta-a", "test-delta-a")
	resumed := startVersionWatch(t, ctx, queueStore, hub, canonical, token)
	if row := resumed.next(t).GetDispatch(); row.GetCommandId() != "test-delta-b" || row.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER {
		t.Fatalf("incremental watch: %v", row)
	}
	if resumed.next(t).GetCaughtUp() == nil {
		t.Fatal("unchanged row replayed")
	}
	resumed.stop(t)
	service := &Service{Store: queueStore, Hub: hub, DispatcherServices: sets.New(peertls.AppURI), WatchLease: time.Minute}
	err := service.WatchDispatches(&dispatcherpb.WatchDispatchesRequest{Filter: testSessionDispatchFilter("test-different-session"), ResumeToken: token}, &collectedWatch{ctx: ctx})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("different filter: %v", err)
	}
	// The token carries no authority: applying the same position as another Identity
	// still filters every live and retained row by the current caller's retained owner.
	batch, err := queueStore.ReadDispatchChanges(ctx, filter, store.IdentityCaller{Identity: &identitypb.Identity{Name: "test-stranger", Uid: sandboxUID}}, 0, false)
	if err != nil || len(batch.Changes) != 0 {
		t.Fatalf("position conferred authority: %+v %v", batch, err)
	}
}

// pausedCheckpointWatch holds a send after the snapshot transaction has committed.
// Writers and pruning must proceed; a watch falling behind during that send must error.
type pausedCheckpointWatch struct {
	*channelWatch
	release chan struct{}
}

func (w *pausedCheckpointWatch) Send(frame *dispatcherpb.WatchDispatchesResponse) error {
	if err := w.channelWatch.Send(frame); err != nil {
		return err
	}
	if frame.GetCaughtUp() != nil {
		select {
		case <-w.release:
		case <-w.ctx.Done():
			return w.ctx.Err()
		}
	}
	return nil
}

func TestVersionWatchReportsExpiredCoverageOnResumeAndReconnectingStream(t *testing.T) {
	ctx := admitted(t)
	queueStore, databaseURL := testStoreWithURL(t, 5)
	filter := testSessionDispatchFilter("test-expired-session")
	if _, err := enqueueWithSession(t, queueStore, ctx, filter.GetSessionId(), "test-expired-sandbox", sandboxUID, input("test-expired-command", "test-input"), nil); err != nil {
		t.Fatal(err)
	}
	listenerURL := postgres.URLWithApplicationName(t, databaseURL, "test-dispatch-retention-reconnect")
	hub, err := wakeups.Listen(t.Context(), listenerURL.String(), store.NotifyChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = hub.Close() })
	watchCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream := &pausedCheckpointWatch{channelWatch: &channelWatch{ctx: watchCtx, frames: make(chan *dispatcherpb.WatchDispatchesResponse, 8)}, release: make(chan struct{})}
	defer close(stream.release)
	service := &Service{Store: queueStore, Hub: hub, DispatcherServices: sets.New(peertls.AppURI), WatchLease: time.Minute}
	done := make(chan error, 1)
	go func() { done <- service.WatchDispatches(&dispatcherpb.WatchDispatchesRequest{Filter: filter}, stream) }()
	watch := &versionWatch{stream: stream.channelWatch, done: done, cancel: cancel}
	watch.next(t)
	token := watch.next(t).GetResumeToken()
	generation := hub.Generation()
	var terminated bool
	if err := queueStore.DB.QueryRowContext(ctx, `SELECT pg_terminate_backend(pid) FROM pg_stat_activity
		WHERE application_name='test-dispatch-retention-reconnect' AND datname=current_database()`).Scan(&terminated); err != nil || !terminated {
		t.Fatalf("terminate watch listener: %t %v", terminated, err)
	}
	if err := queueStore.EndSandbox(ctx, sandboxUID); err != nil {
		t.Fatal(err)
	}
	if _, err := queueStore.DB.ExecContext(ctx, "UPDATE dispatch_tombstone SET deleted_at=clock_timestamp()-interval '48 hours'"); err != nil {
		t.Fatal(err)
	}
	if n, err := queueStore.PruneDispatchTombstones(ctx); err != nil || n != 1 {
		t.Fatalf("prune: %d %v", n, err)
	}
	stream.release <- struct{}{}
	select {
	case err := <-done:
		if status.Code(err) != codes.OutOfRange || !strings.Contains(status.Convert(err).Message(), "resync") {
			t.Fatalf("active stream silently lost deletion: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("expired active watch did not report lost deletion coverage")
	}
	if len(stream.frames) != 0 {
		t.Fatal("expired stream sent a checkpoint covering the missing deletion")
	}
	err = service.WatchDispatches(&dispatcherpb.WatchDispatchesRequest{Filter: filter, ResumeToken: token}, &collectedWatch{ctx: ctx})
	if status.Code(err) != codes.OutOfRange || !strings.Contains(status.Convert(err).Message(), "resync") {
		t.Fatalf("expired resumed stream: %v", err)
	}
	reconnected, cancelReconnect := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancelReconnect()
	for hub.Generation() <= generation || !hub.Connected() {
		if reconnected.Err() != nil {
			t.Fatal("watch listener did not change generation and reconnect")
		}
		runtime.Gosched()
	}
	fresh := startVersionWatch(t, ctx, queueStore, hub, filter, "")
	if fresh.next(t).GetCaughtUp() == nil {
		t.Fatal("fresh snapshot after explicit coverage loss was not empty")
	}
	fresh.stop(t)
}

func TestVersionWatchReplicasReadTerminalReceiptsFromHubWakeup(t *testing.T) {
	ctx := admitted(t)
	queueStore := testStore(t, 5)
	filter := testSessionDispatchFilter("test-replicas-session")
	if _, err := enqueueWithSession(t, queueStore, ctx, filter.GetSessionId(), "test-replicas-sandbox", sandboxUID, input("test-replicas-command", "test-input"), nil); err != nil {
		t.Fatal(err)
	}
	var replicas []*versionWatch
	for range 2 {
		watch := startVersionWatch(t, ctx, store.NewStore(queueStore.DB, 5), testWatchHub(t, queueStore), filter, "")
		if watch.next(t).GetDispatch() == nil || watch.next(t).GetCaughtUp() == nil {
			t.Fatal("replica did not finish its snapshot")
		}
		replicas = append(replicas, watch)
	}
	// This writer is a third process's store. No replica-local fanout is involved.
	if err := store.NewStore(queueStore.DB, 5).EndSandbox(ctx, sandboxUID); err != nil {
		t.Fatal(err)
	}
	for replica, watch := range replicas {
		row := watch.next(t).GetDispatch()
		if row.GetCommandId() != "test-replicas-command" || row.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED || row.GetReason() != "sandbox_deleted" || row.GetSandboxUid() != "" {
			t.Fatalf("replica %d missed deletion: %v", replica, row)
		}
		if watch.next(t).GetCaughtUp() == nil {
			t.Fatalf("replica %d did not advance deletion checkpoint", replica)
		}
		watch.stop(t)
	}
}

func TestVersionWatchAdvancesCheckpointForUnrelatedChanges(t *testing.T) {
	ctx := admitted(t)
	queueStore := testStore(t, 5)
	watch := startVersionWatch(t, ctx, queueStore, testWatchHub(t, queueStore), testSessionDispatchFilter("test-empty-scope"), "")
	before := watch.next(t)
	if before.GetCaughtUp() == nil {
		t.Fatal("empty snapshot has no checkpoint")
	}
	if _, err := enqueueWithSession(t, queueStore, ctx, "test-other-scope", "test-other-sandbox", sandboxUID, input("test-other-command", "test-input"), nil); err != nil {
		t.Fatal(err)
	}
	after := watch.next(t)
	if after.GetCaughtUp() == nil || after.GetResumeToken() == before.GetResumeToken() {
		t.Fatalf("unrelated change replayed a receipt or failed to advance checkpoint: %v", after)
	}
	watch.stop(t)
}
