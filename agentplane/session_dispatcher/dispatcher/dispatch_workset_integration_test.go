package dispatcher

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/grpc-ecosystem/grpc-gateway/v2/runtime"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"k8s.io/apimachinery/pkg/util/sets"
)

func openWorksetWatch(t *testing.T, ctx context.Context, service *Service, sessionID, token string) *versionWatch {
	t.Helper()
	ctx, cancel := context.WithCancel(ctx)
	watch := &versionWatch{stream: &channelWatch{ctx: ctx, frames: make(chan *dispatcherpb.WatchDispatchesResponse, 8)}, done: make(chan error, 1), cancel: cancel}
	go func() {
		watch.done <- service.WatchDispatches(&dispatcherpb.WatchDispatchesRequest{WorksetSessionId: sessionID, ResumeToken: token}, watch.stream)
	}()
	t.Cleanup(cancel)
	return watch
}

func TestDispatchWorksetTracksDeliveryAndLegacyCoverage(t *testing.T) {
	ctx := admitted(t)
	const sessionID, commandID = "workset-session", "workset-command"
	queueStore := testStore(t, 5)
	if _, err := enqueueWithSession(t, queueStore, ctx, sessionID, "workset-sandbox", sandboxUID, input(commandID, "authored payload"), nil); err != nil {
		t.Fatal(err)
	}
	// A pre-retention Dispatcher left a deletion receipt for this Session. It
	// contributes current metadata but no command payload.
	legacyReason := "sandbox_deleted: " + strings.Repeat("r", 300)
	if _, err := queueStore.DB.ExecContext(ctx, `INSERT INTO dispatch_tombstone
		(command_id,version,session_id,operation,state,reason,wake_assessment,created_at,updated_at)
		SELECT 'legacy',nextval('dispatch_version_seq'),session_id,operation,'UNKNOWN',$2,
			wake_assessment,created_at,updated_at FROM dispatch WHERE command_id=$1`, commandID, legacyReason); err != nil {
		t.Fatal(err)
	}
	hub := testWatchHub(t, queueStore)
	service := &Service{Store: queueStore, Hub: hub, DispatcherServices: sets.New(peertls.AppURI), WatchLease: time.Minute}
	watch := openWorksetWatch(t, ctx, service, sessionID, "")
	first := watch.next(t)
	if initial := first.GetWorkset(); len(initial.GetActive()) != 1 || initial.GetActive()[0].GetCommandId() != commandID ||
		len(initial.GetRecentIssues()) != 1 || initial.GetRecentIssues()[0].GetCommandId() != "legacy" ||
		!initial.GetRecentIssues()[0].GetReasonTruncated() || initial.GetRecentIssues()[0].GetReason() != legacyReason[:256] {
		t.Fatalf("initial workset = %v", initial)
	}
	exact := startVersionWatch(t, ctx, queueStore, hub, testSessionDispatchFilter(sessionID, "legacy"), "")
	if full := exact.next(t).GetDispatch(); full.GetCommandId() != "legacy" || full.GetReason() != legacyReason {
		t.Fatalf("selected watch lost the original legacy reason: %v", full)
	}
	exact.stop(t)
	owner := claimSessionOwner(t, queueStore, sessionID)
	if _, err := queueStore.Settle(ctx, commandID, owner, []string{store.QueueState}, store.DeliveringState, "", nil); err != nil {
		t.Fatal(err)
	}
	if delivering := watch.next(t).GetWorkset(); len(delivering.GetActive()) != 1 ||
		delivering.GetActive()[0].GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_DELIVERING_TO_RUNNER {
		t.Fatalf("delivering workset = %v", delivering)
	}
	if _, err := queueStore.Settle(ctx, commandID, owner, []string{store.DeliveringState}, store.AdmittedState, "", &eventlogpb.EventEntry{Cursor: 1}); err != nil {
		t.Fatal(err)
	}
	settled := watch.next(t)
	admitted := settled.GetWorkset().GetRecentAdmittedOrCancelled()
	if len(settled.GetWorkset().GetActive()) != 0 || len(admitted) != 1 ||
		admitted[0].GetCommandId() != commandID || !admitted[0].GetJournalAdmitted() {
		t.Fatalf("admitted workset = %v", settled.GetWorkset())
	}
	watch.stop(t)
	reconnected := openWorksetWatch(t, ctx, service, sessionID, settled.GetResumeToken())
	if replacement := reconnected.next(t).GetWorkset(); len(replacement.GetRecentAdmittedOrCancelled()) != 1 ||
		len(replacement.GetRecentIssues()) != 1 {
		t.Fatalf("resume omitted unchanged receipts: %v", replacement)
	}
	// Pruning a shown receipt wakes its current Session watch. An older
	// checkpoint below the coverage floor receives an explicit error.
	if _, err := queueStore.DB.ExecContext(ctx, "UPDATE dispatch_tombstone SET deleted_at=clock_timestamp()-interval '48 hours'"); err != nil {
		t.Fatal(err)
	}
	if count, err := queueStore.PruneDispatchTombstones(ctx); err != nil || count != 1 {
		t.Fatalf("prune: %d, %v", count, err)
	}
	if replacement := reconnected.next(t).GetWorkset(); len(replacement.GetRecentIssues()) != 0 {
		t.Fatalf("pruned receipt still visible: %v", replacement)
	}
	reconnected.stop(t)
	_, _, err := queueStore.ReadDispatchWorkset(ctx, sessionID, testStoreCaller(nil), 0, true)
	if status.Code(err) != codes.OutOfRange {
		t.Fatalf("expired checkpoint: %v", err)
	}
	if err := service.WatchDispatches(&dispatcherpb.WatchDispatchesRequest{WorksetSessionId: "other-session", ResumeToken: settled.GetResumeToken()}, &collectedWatch{ctx: ctx}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("cross-Session token: %v", err)
	}
	stranger := store.IdentityCaller{Identity: &identitypb.Identity{Name: "unrelated", Uid: sandboxUID}}
	hidden, _, err := queueStore.ReadDispatchWorkset(ctx, sessionID, stranger, 0, false)
	if err != nil || len(hidden.GetRecentAdmittedOrCancelled()) != 0 {
		t.Fatalf("another Identity saw the workset: %v, %v", hidden, err)
	}
}

func TestDispatchWorksetGeneratedFacadeReportsEachBound(t *testing.T) {
	const sessionID = "workset-http-session"
	queueStore, databaseURL := testStoreWithURL(t, 2048)
	if _, err := enqueueWithSession(t, queueStore, t.Context(), sessionID, "workset-http-sandbox", sandboxUID, input("head", "large payload omitted"), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := queueStore.DB.ExecContext(t.Context(), `INSERT INTO dispatch
		(command_id,session_id,sandbox_name,sandbox_uid,operation,command,state,reason,created_at,updated_at)
		SELECT 'workset-' || rows.i,session_id,sandbox_name,sandbox_uid,operation,command,
			CASE WHEN rows.i <= 1024 THEN 'QUEUED_IN_DISPATCHER'
				WHEN rows.i <= 1045 THEN 'FAILED' ELSE 'CANCELLED_IN_DISPATCHER' END,
			repeat('r',300),now(),now()
		FROM dispatch CROSS JOIN generate_series(1,1066) AS rows(i) WHERE command_id='head'`); err != nil {
		t.Fatal(err)
	}
	hub, err := wakeups.Listen(t.Context(), databaseURL, store.NotifyChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = hub.Close() })
	service := &Service{Store: queueStore, Hub: hub, WatchLease: time.Minute, DispatcherServices: sets.New(peertls.AppURI)}
	rule, err := auth.Services([]string{peertls.AppURI})
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.ServeTLSWithTimeout(t, 0, peertls.SessionDispatcherURI, Methods(rule), func(server *grpc.Server) {
		dispatcherpb.RegisterSessionDispatcherServer(server, service)
	}, peertls.AppURI)
	client := dispatcherpb.NewSessionDispatcherClient(grpcmethods.AsCaller(connection, peertls.AppURI))
	mux := runtime.NewServeMux()
	if err := dispatcherpb.RegisterSessionDispatcherHandlerClient(t.Context(), mux, client); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	httpClient := &http.Client{Timeout: 10 * time.Second}
	response, err := httpClient.Get(server.URL + "/v1/dispatches:watch?worksetSessionId=" + sessionID)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("workset HTTP status = %d", response.StatusCode)
	}
	var envelope struct {
		Result json.RawMessage `json:"result"`
	}
	if err := json.NewDecoder(response.Body).Decode(&envelope); err != nil {
		t.Fatal(err)
	}
	frame := &dispatcherpb.WatchDispatchesResponse{}
	if err := protojson.Unmarshal(envelope.Result, frame); err != nil {
		t.Fatal(err)
	}
	result := frame.GetWorkset()
	if frame.GetResumeToken() == "" || result.GetSessionId() != sessionID || len(result.GetActive()) != 1024 ||
		!result.GetMoreActiveAvailable() || len(result.GetRecentIssues()) != 20 || !result.GetMoreRecentIssuesAvailable() ||
		len(result.GetRecentAdmittedOrCancelled()) != 20 || !result.GetMoreRecentAdmittedOrCancelledAvailable() {
		t.Fatalf("unexpected workset bounds: active=%d/%t issues=%d/%t admitted=%d/%t",
			len(result.GetActive()), result.GetMoreActiveAvailable(),
			len(result.GetRecentIssues()), result.GetMoreRecentIssuesAvailable(),
			len(result.GetRecentAdmittedOrCancelled()), result.GetMoreRecentAdmittedOrCancelledAvailable())
	}
	if first := result.GetActive()[0].GetCommandId(); first != "head" {
		t.Fatalf("queue order lost: first = %q", first)
	}
	if issue := result.GetRecentIssues()[0]; !issue.GetReasonTruncated() || len(issue.GetReason()) != 256 ||
		result.GetActive()[0].GetReasonTruncated() || strings.Contains(string(envelope.Result), "large payload omitted") {
		t.Fatal("workset delivered unbounded reason or authored payload")
	}
	for _, query := range []string{"", "?worksetSessionId=" + sessionID + "&filter.sessionId=" + sessionID} {
		rejected, err := httpClient.Get(server.URL + "/v1/dispatches:watch" + query)
		if err != nil {
			t.Fatal(err)
		}
		rejected.Body.Close()
		if rejected.StatusCode != http.StatusBadRequest {
			t.Fatalf("invalid filter %q returned HTTP %d", query, rejected.StatusCode)
		}
	}
}
