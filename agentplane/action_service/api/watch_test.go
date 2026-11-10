package api

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/protobuf/encoding/protojson"
)

type setStream = grpc.ServerStreamingClient[action_servicepb.WatchActionRequestsResponse]

func (s *served) watchSet(t *testing.T, token string, states ...action_servicepb.ActionState) setStream {
	t.Helper()
	stream, err := s.client.WatchActionRequests(as(t, token), &action_servicepb.WatchActionRequestsRequest{State: states})
	if err != nil {
		t.Fatal(err)
	}
	return stream
}

func recvFrame(t *testing.T, stream setStream) *action_servicepb.WatchActionRequestsResponse {
	t.Helper()
	frame, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	return frame
}

// snapshot reads the frames up to snapshot_complete and returns the chunks they carried.
func snapshot(t *testing.T, stream setStream) [][]*action_servicepb.ActionRequest {
	t.Helper()
	var chunks [][]*action_servicepb.ActionRequest
	for {
		frame := recvFrame(t, stream)
		switch frame := frame.GetFrame().(type) {
		case *action_servicepb.WatchActionRequestsResponse_Snapshot:
			chunks = append(chunks, frame.Snapshot.GetActionRequests())
		case *action_servicepb.WatchActionRequestsResponse_SnapshotComplete:
			return chunks
		default:
			t.Fatalf("a %T frame before snapshot_complete", frame)
		}
	}
}

// upsert reads the next frame, which must be an upsert.
func upsert(t *testing.T, stream setStream) *action_servicepb.ActionRequest {
	t.Helper()
	frame := recvFrame(t, stream)
	if frame.GetUpsert() == nil {
		t.Fatalf("frame %v, want an upsert", frame)
	}
	return frame.GetUpsert()
}

func wantUpsert(t *testing.T, stream setStream, request store.ActionRequest, state action_servicepb.ActionState) {
	t.Helper()
	if got := upsert(t, stream); got.GetId() != request.ID.String() || got.GetState() != state {
		t.Errorf("upsert of %s in %v, want %s in %v", got.GetId(), got.GetState(), request.ID, state)
	}
}

// wantEnd reads the stream to its end, which must carry the code; OK is a clean end.
func wantEnd[Frame any](t *testing.T, what string, stream grpc.ServerStreamingClient[Frame], code codes.Code) {
	t.Helper()
	frame, err := stream.Recv()
	switch {
	case err == nil:
		t.Errorf("%s: frame %v, want the stream to end %v", what, frame, code)
	case code == codes.OK && !errors.Is(err, io.EOF):
		t.Errorf("%s: %v, want the stream to end", what, err)
	case code != codes.OK:
		wantCode(t, what, err, code)
	}
}

// claim reserves the allowed request's Execution, which moves it to dispatching.
func (s *served) claim(t *testing.T, request store.ActionRequest) *store.ExecutionClaim {
	t.Helper()
	claim, err := s.store.ClaimExecution(t.Context(), request.ID, "test-executor", time.Minute, func(store.ActionIdentity) bool { return true })
	if err != nil || claim == nil {
		t.Fatalf("claim: %v, %v", claim, err)
	}
	return claim
}

const pending = action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING

func TestASetWatchSendsTheFilteredSetInBoundedChunksThenEachChange(t *testing.T) {
	s := serve(t)
	want := map[string]bool{}
	for i := range snapshotChunk + 1 {
		want[s.submit(t, caller, fmt.Sprintf("test-pending-%d", i)).ID.String()] = true
	}
	s.decide(t, s.submit(t, caller, "test-denied"), store.VerdictDeny)
	stream := s.watchSet(t, callerToken, pending)
	chunks := snapshot(t, stream)
	got := map[string]bool{}
	for _, chunk := range chunks {
		if len(chunk) > snapshotChunk {
			t.Errorf("a chunk of %d requests", len(chunk))
		}
		for _, request := range chunk {
			got[request.GetId()] = true
		}
	}
	if len(chunks) != 2 || !maps.Equal(got, want) {
		t.Errorf("%d chunks of %d requests, want 2 of the %d pending", len(chunks), len(got), len(want))
	}

	entered := s.submit(t, caller, "test-entered")
	wantUpsert(t, stream, entered, pending)
	left := s.decide(t, entered, store.VerdictAllow)
	// A request that leaves the set arrives in its new state, and no more of its changes follow.
	wantUpsert(t, stream, left, action_servicepb.ActionState_ACTION_STATE_ALLOWED)
	s.claim(t, left)
	sentinel := s.submit(t, caller, "test-sentinel")
	wantUpsert(t, stream, sentinel, pending)
}

func TestAnEmptySetCompletesItsSnapshotAlone(t *testing.T) {
	s := serve(t)
	s.submit(t, caller, "test-pending")
	stream := s.watchSet(t, s.operatorToken, action_servicepb.ActionState_ACTION_STATE_CANCELLED)
	if frame := recvFrame(t, stream); frame.GetSnapshotComplete() == nil {
		t.Errorf("first frame %v, want snapshot_complete", frame)
	}
}

func TestASetWatchShowsOnlyWhatItsPrincipalMayRead(t *testing.T) {
	s := serve(t)
	own := s.submit(t, caller, "test-own")
	foreign := s.submit(t, otherCaller, "test-foreign")
	callers := s.watchSet(t, callerToken)
	readers := s.watchSet(t, readerToken)
	if chunks := snapshot(t, callers); len(chunks) != 1 || !slices.Equal(orderedIDs(chunks[0]), []string{own.ID.String()}) {
		t.Errorf("the caller's snapshot: %v", chunks)
	}
	if chunks := snapshot(t, readers); len(chunks) != 1 || !slices.Equal(requestIDs(chunks[0]), idsOf(own, foreign)) {
		t.Errorf("the reader's snapshot: %v", chunks)
	}
	foreignChange := s.decide(t, foreign, store.VerdictDeny)
	wantUpsert(t, readers, foreignChange, action_servicepb.ActionState_ACTION_STATE_DENIED)
	sentinel := s.submit(t, caller, "test-sentinel")
	wantUpsert(t, callers, sentinel, pending)
}

func TestAWatchEndsUnauthenticatedOnceItsBearerIsRefusedAndSendsNothingMore(t *testing.T) {
	s := serve(t)
	// Accounts of this test's own, so that changing them leaves the other tests' alone.
	delabeled := auth.Account{Namespace: testNamespace, Name: "test-watch-delabeled-caller"}
	revoked := auth.Account{Namespace: testNamespace, Name: "test-watch-revoked-caller"}
	for _, account := range []auth.Account{delabeled, revoked} {
		if err := setCallerLabel(t.Context(), account, true); err != nil {
			t.Fatal(err)
		}
	}
	delabeledToken, err := kube.WorkloadToken(t.Context(), delabeled, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	pod, err := kube.Pod(t.Context(), revoked)
	if err != nil {
		t.Fatal(err)
	}
	revokedToken, err := kube.PodToken(t.Context(), pod, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	delabeledWatch := s.watchSet(t, delabeledToken)
	revokedWatch := s.watchSet(t, revokedToken)
	snapshot(t, delabeledWatch)
	snapshot(t, revokedWatch)

	if err := setCallerLabel(t.Context(), delabeled, false); err != nil {
		t.Fatal(err)
	}
	// A change to the set is not sent once the bearer is refused.
	s.submit(t, principal(delabeled), "test-after-delabeling")
	wantEnd(t, "an unlabeled account's watch", delabeledWatch, codes.Unauthenticated)
	// With nothing to send, the bearer is checked again all the same.
	if err := kube.Revoke(t.Context(), pod, revokedToken, testAudience); err != nil {
		t.Fatal(err)
	}
	wantEnd(t, "a deleted Pod's watch", revokedWatch, codes.Unauthenticated)

	// An operator token past its expiry, but within the clock skew for a few seconds more.
	lapsing := s.issuer.token(t, map[string]any{"exp": time.Now().Add(-clockSkew + 5*time.Second).Unix()})
	request := s.submit(t, caller, "test-watched")
	receipt, err := s.client.WatchActionRequest(as(t, lapsing), &action_servicepb.WatchActionRequestRequest{RequestId: request.ID.String()})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := receipt.Recv(); err != nil {
		t.Fatal(err)
	}
	wantEnd(t, "a lapsed operator token's watch", receipt, codes.Unauthenticated)
}

func TestDrainingRenewsASetWatchAndEndsAReceiptWatch(t *testing.T) {
	s := serve(t)
	request := s.submit(t, caller, "test-watched")
	set := s.watchSet(t, s.operatorToken)
	snapshot(t, set)
	receipt, err := s.client.WatchActionRequest(as(t, callerToken), &action_servicepb.WatchActionRequestRequest{RequestId: request.ID.String()})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := receipt.Recv(); err != nil {
		t.Fatal(err)
	}
	s.gate.Drain()
	if frame := recvFrame(t, set); frame.GetReconnectRequired() == nil {
		t.Errorf("frame %v, want reconnect_required", frame)
	}
	wantEnd(t, "the set watch after reconnect_required", set, codes.OK)
	wantEnd(t, "the receipt watch", receipt, codes.Unavailable)
}

func TestAWatchOutlivesTheNotificationConnectionAndReadsWhatChangedUnannounced(t *testing.T) {
	s := serve(t)
	request := s.submit(t, caller, "test-watched")
	set := s.watchSet(t, s.operatorToken, pending)
	snapshot(t, set)
	receipt, err := s.client.WatchActionRequest(as(t, callerToken), &action_servicepb.WatchActionRequestRequest{RequestId: request.ID.String()})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := receipt.Recv(); err != nil {
		t.Fatal(err)
	}
	// A change no notification announces, then a dropped LISTEN connection: only the wakeup after it reconnects
	// can show the change.
	if _, err := s.db.ExecContext(t.Context(), `UPDATE action_request SET version = version + 1 WHERE id = $1`, request.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := s.db.ExecContext(t.Context(), `SELECT pg_terminate_backend(pid) FROM pg_stat_activity
		WHERE application_name = $1 AND datname = current_database()`, updatesConnection); err != nil {
		t.Fatal(err)
	}
	if got := upsert(t, set); got.GetId() != request.ID.String() || got.GetVersion() != request.Version+1 {
		t.Errorf("upsert %v, want version %d of %s", got, request.Version+1, request.ID)
	}
	if got, err := receipt.Recv(); err != nil || got.GetActionRequest().GetVersion() != request.Version+1 {
		t.Errorf("receipt %v, %v; want version %d", got, err, request.Version+1)
	}
}

func TestAReceiptWatchSendsEachChangeUntilItsConditionHolds(t *testing.T) {
	s := serve(t)
	watch := func(request store.ActionRequest, until action_servicepb.WatchUntil) grpc.ServerStreamingClient[action_servicepb.WatchActionRequestResponse] {
		t.Helper()
		stream, err := s.client.WatchActionRequest(as(t, callerToken), &action_servicepb.WatchActionRequestRequest{RequestId: request.ID.String(), Until: until})
		if err != nil {
			t.Fatal(err)
		}
		return stream
	}
	receive := func(stream grpc.ServerStreamingClient[action_servicepb.WatchActionRequestResponse]) *action_servicepb.ActionRequest {
		t.Helper()
		frame, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		return frame.GetActionRequest()
	}

	decided := s.submit(t, caller, "test-decision")
	decision := watch(decided, action_servicepb.WatchUntil_WATCH_UNTIL_DECISION)
	if got := receive(decision); got.GetState() != pending {
		t.Errorf("first frame in %v", got.GetState())
	}
	s.decide(t, decided, store.VerdictAllow)
	if got := receive(decision); got.GetState() != action_servicepb.ActionState_ACTION_STATE_ALLOWED {
		t.Errorf("decided frame in %v", got.GetState())
	}
	wantEnd(t, "a watch until the decision", decision, codes.OK)

	executed := s.submit(t, caller, "test-terminal")
	terminal := watch(executed, action_servicepb.WatchUntil_WATCH_UNTIL_UNSPECIFIED)
	last := receive(terminal)
	allowed := s.decide(t, executed, store.VerdictAllow)
	claim := s.claim(t, allowed)
	if _, err := s.store.MarkRunning(t.Context(), executed.ID); err != nil {
		t.Fatal(err)
	}
	if err := s.store.FinishExecution(t.Context(), executed.ID, claim.Lease, store.ExecutionOutcome{State: store.ExecutionSucceeded}); err != nil {
		t.Fatal(err)
	}
	// Changes in quick succession may arrive as one frame; each frame is newer than the last.
	for last.GetState() != action_servicepb.ActionState_ACTION_STATE_SUCCEEDED {
		next := receive(terminal)
		if next.GetVersion() <= last.GetVersion() {
			t.Fatalf("version %d after %d", next.GetVersion(), last.GetVersion())
		}
		last = next
	}
	wantEnd(t, "a watch until a terminal state", terminal, codes.OK)

	// A receipt that already holds the condition is the only frame.
	denied := s.decide(t, s.submit(t, caller, "test-denied"), store.VerdictDeny)
	done := watch(denied, action_servicepb.WatchUntil_WATCH_UNTIL_TERMINAL)
	if got := receive(done); got.GetState() != action_servicepb.ActionState_ACTION_STATE_DENIED {
		t.Errorf("frame in %v", got.GetState())
	}
	wantEnd(t, "a watch of a finished request", done, codes.OK)
}

func TestAWatchOfWhatTheCallerMayNotReadOrCannotNameFails(t *testing.T) {
	s := serve(t)
	foreign := s.submit(t, otherCaller, "test-foreign")
	for name, tc := range map[string]struct {
		request *action_servicepb.WatchActionRequestRequest
		code    codes.Code
	}{
		"another caller's request":        {&action_servicepb.WatchActionRequestRequest{RequestId: foreign.ID.String()}, codes.NotFound},
		"a request ID that is not a UUID": {&action_servicepb.WatchActionRequestRequest{RequestId: "test-not-a-uuid"}, codes.InvalidArgument},
		"an undefined condition":          {&action_servicepb.WatchActionRequestRequest{RequestId: foreign.ID.String(), Until: 99}, codes.InvalidArgument},
	} {
		stream, err := s.client.WatchActionRequest(as(t, callerToken), tc.request)
		if err != nil {
			t.Fatal(err)
		}
		wantEnd(t, name, stream, tc.code)
	}
	set := s.watchSet(t, callerToken, action_servicepb.ActionState_ACTION_STATE_UNSPECIFIED)
	wantEnd(t, "a set watch of an unspecified state", set, codes.InvalidArgument)
}

func TestTheHTTPFacadeStreamsAWatchAsLinesOfJSON(t *testing.T) {
	s := serve(t)
	denied := s.decide(t, s.submit(t, caller, "test-denied"), store.VerdictDeny)
	call, err := http.NewRequestWithContext(as(t, callerToken), http.MethodGet,
		s.url+"/v1/action-requests/"+denied.ID.String()+":watch?until=WATCH_UNTIL_DECISION", nil)
	if err != nil {
		t.Fatal(err)
	}
	call.Header.Set("Authorization", "Bearer "+callerToken)
	response, err := http.DefaultClient.Do(call)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	lines := bufio.NewScanner(response.Body)
	var got []*action_servicepb.WatchActionRequestResponse
	for lines.Scan() {
		var line struct {
			Result json.RawMessage `json:"result"`
		}
		if err := json.Unmarshal(lines.Bytes(), &line); err != nil {
			t.Fatalf("line %s: %v", lines.Bytes(), err)
		}
		frame := &action_servicepb.WatchActionRequestResponse{}
		if err := protojson.Unmarshal(line.Result, frame); err != nil {
			t.Fatal(err)
		}
		got = append(got, frame)
	}
	if response.StatusCode != http.StatusOK || len(got) != 1 || got[0].GetActionRequest().GetId() != denied.ID.String() {
		t.Errorf("%d %v", response.StatusCode, got)
	}
}
