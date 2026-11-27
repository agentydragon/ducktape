package rpc

import (
	"io"
	"slices"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/fieldmaskpb"
)

type watching struct {
	t      *testing.T
	stream grpc.ServerStreamingClient[historypb.WatchSessionSummariesResponse]
}

func (s testServer) watch(t *testing.T, request *historypb.WatchSessionSummariesRequest) watching {
	t.Helper()
	stream, err := s.client.WatchSessionSummaries(callContext(t), request)
	if err != nil {
		t.Fatal(err)
	}
	return watching{t: t, stream: stream}
}

func (w watching) receive() *historypb.WatchSessionSummariesResponse {
	w.t.Helper()
	response, err := w.stream.Recv()
	if err != nil {
		w.t.Fatalf("the watch ended: %v", err)
	}
	return response
}

func (w watching) wantChanged() {
	w.t.Helper()
	if response := w.receive(); response.GetChanged() == nil {
		w.t.Fatalf("want changed, received %v", response)
	}
}

func (w watching) wantOpened(sessionID uuid.UUID, position uint64) {
	w.t.Helper()
	response := w.receive()
	if opened := response.GetSessionRecorded(); opened.GetSessionId() != sessionID.String() || opened.GetPosition() != position {
		w.t.Fatalf("want %s opened at %d, received %v", sessionID, position, response)
	}
}

func (w watching) wantReconnectAndCleanEnd() {
	w.t.Helper()
	if response := w.receive(); response.GetReconnectRequired() == nil {
		w.t.Fatalf("want reconnect_required, received %v", response)
	}
	if _, err := w.stream.Recv(); err != io.EOF {
		w.t.Fatalf("want the watch to end cleanly, received %v", err)
	}
}

// currentPosition is the position of the last Session recorded so far, which earlier tests have advanced.
func (s testServer) currentPosition(t *testing.T) uint64 {
	t.Helper()
	var position uint64
	if err := s.db.QueryRowContext(t.Context(), "SELECT COALESCE(max(recorded_position), 0) FROM session_history").Scan(&position); err != nil {
		t.Fatal(err)
	}
	return position
}

// settled returns once the hub has passed on every notification committed before the call, so a watch opened
// after it hears only about what happens next.
func (s testServer) settled(t *testing.T) {
	t.Helper()
	barrier, unsubscribe, err := s.hub.Subscribe(projection.ChannelSummaries)
	if err != nil {
		t.Fatal(err)
	}
	defer unsubscribe()
	s.notifySummaries(t)
	select {
	case <-barrier:
	case <-time.After(callDeadline):
		t.Fatal("the hub never passed on the notification")
	}
}

func (s testServer) notifySummaries(t *testing.T) {
	t.Helper()
	if _, err := s.db.ExecContext(t.Context(), "SELECT pg_notify($1, '')", projection.ChannelSummaries); err != nil {
		t.Fatal(err)
	}
}

func TestAWatchOpensWithChangedAndRepeatsItAfterEachChange(t *testing.T) {
	server := newServer(t, time.Hour)
	watch := server.watch(t, &historypb.WatchSessionSummariesRequest{})
	watch.wantChanged()
	server.notifySummaries(t)
	watch.wantChanged()
	// A Session recorded wakes the watch too, but a request that asked for no openings is told of none.
	server.record(t)
	watch.wantChanged()
	server.notifySummaries(t)
	watch.wantChanged()
}

func TestAWatchAskingForSessionsNamesTheSessionsRecordedAfterThePosition(t *testing.T) {
	server := newServer(t, time.Hour)
	before := server.currentPosition(t)
	first, firstPosition := server.record(t)
	second, secondPosition := server.record(t)
	server.settled(t)

	watch := server.watch(t, &historypb.WatchSessionSummariesRequest{RecordedAfter: proto.Uint64(before)})
	watch.wantChanged()
	watch.wantOpened(first, firstPosition)
	watch.wantOpened(second, secondPosition)

	third, thirdPosition := server.record(t)
	watch.wantChanged()
	watch.wantOpened(third, thirdPosition)

	// A caller that reconnects with the last position it handled is told of what came after it only.
	reconnected := server.watch(t, &historypb.WatchSessionSummariesRequest{RecordedAfter: proto.Uint64(secondPosition)})
	reconnected.wantChanged()
	reconnected.wantOpened(third, thirdPosition)
}

func TestSessionsBeyondOnePageAreAllNamedInOrder(t *testing.T) {
	server := newServer(t, time.Hour)
	before := server.currentPosition(t)
	recorded := map[uuid.UUID]bool{}
	for range recordingsPage + 1 {
		sessionID, _ := server.record(t)
		recorded[sessionID] = true
	}
	server.settled(t)

	watch := server.watch(t, &historypb.WatchSessionSummariesRequest{RecordedAfter: proto.Uint64(before)})
	watch.wantChanged()
	var positions []uint64
	for range recordingsPage + 1 {
		opened := watch.receive().GetSessionRecorded()
		sessionID, err := uuid.Parse(opened.GetSessionId())
		if err != nil || !recorded[sessionID] {
			t.Fatalf("named %q, which was not recorded", opened.GetSessionId())
		}
		delete(recorded, sessionID)
		positions = append(positions, opened.GetPosition())
	}
	if !slices.IsSorted(positions) || slices.Min(positions) <= before {
		t.Errorf("positions %v are not ascending after %d", positions, before)
	}
	// Everything recorded was named; the next message is the wakeup for what changes next.
	server.notifySummaries(t)
	watch.wantChanged()
}

func TestSummaryWatchesReconnectOnLeaseExpiryOrDrain(t *testing.T) {
	for _, end := range []struct {
		name  string
		lease time.Duration
		drain bool
	}{
		{name: "lease", lease: 200 * time.Millisecond},
		{name: "drain", lease: time.Hour, drain: true},
	} {
		t.Run(end.name, func(t *testing.T) {
			for _, mode := range []string{"notifications", "selected"} {
				t.Run(mode, func(t *testing.T) {
					server := newServer(t, end.lease)
					request := &historypb.WatchSessionSummariesRequest{}
					if mode == "selected" {
						request.Selected = &historypb.SelectedSessionSummaries{FieldMask: everySummaryField()}
					}
					watch := server.watch(t, request)
					if mode == "selected" {
						if response := watch.receive(); response.GetSnapshot() == nil {
							t.Fatalf("want snapshot, received %v", response)
						}
					} else {
						watch.wantChanged()
					}
					if end.drain {
						server.service.Drain()
					}
					watch.wantReconnectAndCleanEnd()
				})
			}
		})
	}
}

func TestSelectedSummariesSnapshotAndUpdates(t *testing.T) {
	server := newServer(t, time.Hour)
	sessionID, _ := server.record(t)
	missing := uuid.New()
	stream, err := server.client.WatchSessionSummaries(callContext(t), selectedWatch(everySummaryField(), sessionID.String(), missing.String()))
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if snapshot := first.GetSnapshot(); snapshot == nil || len(snapshot.GetSummaries()) != 1 || snapshot.GetSummaries()[0].GetSessionId() != sessionID.String() {
		t.Fatalf("snapshot: %v", first)
	}
	if _, err := server.history.BindWriter(t.Context(), missing, testWriter, ""); err != nil {
		t.Fatal(err)
	}
	next, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if next.GetSummary().GetSessionId() != missing.String() {
		t.Fatalf("update: %v", next)
	}
}

func TestSelectedSummariesRespectDelegatedReadScope(t *testing.T) {
	owner := storage.Identity{Name: "watch-owner-" + uuid.NewString(), UID: uuid.NewString()}
	other := storage.Identity{Name: "watch-other-" + uuid.NewString(), UID: uuid.NewString()}
	server, connect := newServiceAccessServer(t, time.Minute, callDeadline, nil, []auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}}, newLiveIdentities(owner, other))
	ownID := server.recordAs(t, owner)
	otherID := server.recordAs(t, other)
	stream, err := connect(t, peertls.ActionsURI).WatchSessionSummaries(serviceDelegatedIdentity(t, owner), selectedWatch(everySummaryField(), ownID.String(), otherID.String()))
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if got := first.GetSnapshot().GetSummaries(); len(got) != 1 || got[0].GetSessionId() != ownID.String() {
		t.Fatalf("snapshot exposes wrong scope: %v", first)
	}
}

func TestSelectedSummariesRejectRecordedAfter(t *testing.T) {
	server := newServer(t, time.Hour)
	stream, err := server.client.WatchSessionSummaries(callContext(t), &historypb.WatchSessionSummariesRequest{
		RecordedAfter: proto.Uint64(0), Selected: &historypb.SelectedSessionSummaries{SessionIds: []string{uuid.NewString()}, FieldMask: everySummaryField()},
	})
	if err != nil {
		t.Fatal(err)
	}
	_, err = stream.Recv()
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("got %v, want InvalidArgument", err)
	}
}

func selectedWatch(fields *fieldmaskpb.FieldMask, sessionIDs ...string) *historypb.WatchSessionSummariesRequest {
	return &historypb.WatchSessionSummariesRequest{Selected: &historypb.SelectedSessionSummaries{SessionIds: sessionIDs, FieldMask: fields}}
}

// everySummaryField is a field set naming every SessionSummary field, for a watch that wants whole summaries.
func everySummaryField() *fieldmaskpb.FieldMask {
	fields := (&historypb.SessionSummary{}).ProtoReflect().Descriptor().Fields()
	paths := make([]string, 0, fields.Len())
	for i := range fields.Len() {
		paths = append(paths, string(fields.Get(i).Name()))
	}
	return &fieldmaskpb.FieldMask{Paths: paths}
}

func TestSelectedSummariesWithAFieldSetCarryOnlyThoseFields(t *testing.T) {
	server := newServer(t, time.Hour)
	sessionID, _ := server.record(t)
	appendFollowEntries(t, server, sessionID, testEntry(1))
	full := server.watch(t, selectedWatch(everySummaryField(), sessionID.String())).receive().GetSnapshot().GetSummaries()
	narrow := server.watch(t, selectedWatch(&fieldmaskpb.FieldMask{Paths: []string{"committed_cursor"}}, sessionID.String())).receive().GetSnapshot().GetSummaries()
	if len(full) != 1 || full[0].GetCommittedCursor() == 0 {
		t.Fatalf("the unreduced snapshot must have a cursor to reduce to: %v", full)
	}
	want := &historypb.SessionSummary{SessionId: sessionID.String(), CommittedCursor: full[0].GetCommittedCursor()}
	if len(narrow) != 1 || !proto.Equal(narrow[0], want) {
		t.Fatalf("got %v, want %v", narrow, want)
	}
}

func TestSelectedSummariesWithAFieldSetSendNothingForChangesOutsideIt(t *testing.T) {
	server := newServer(t, time.Hour)
	changing, _ := server.record(t)
	later := uuid.New()
	ids := []string{changing.String(), later.String()}
	everything := server.watch(t, selectedWatch(everySummaryField(), ids...))
	// No fold runs in this server, so appending moves the committed cursor but never the folded one.
	foldOnly := server.watch(t, selectedWatch(&fieldmaskpb.FieldMask{Paths: []string{"folded_through"}}, ids...))
	everything.receive()
	foldOnly.receive()

	appendFollowEntries(t, server, changing, testEntry(1))
	server.notifySummaries(t)
	if got := everything.receive().GetSummary(); got.GetSessionId() != changing.String() || got.GetCommittedCursor() == 0 {
		t.Fatalf("the unreduced watch must see the append: %v", got)
	}
	// The next frame the reduced watch sends is for a Session that appears, not for the append before it.
	if _, err := server.history.BindWriter(t.Context(), later, testWriter, ""); err != nil {
		t.Fatal(err)
	}
	if got := foldOnly.receive().GetSummary(); got.GetSessionId() != later.String() {
		t.Fatalf("a change outside the field set sent %v", got)
	}
}

func TestSelectedSummariesRefuseAFieldSetOfUnknownPaths(t *testing.T) {
	server := newServer(t, time.Hour)
	stream := server.watch(t, selectedWatch(&fieldmaskpb.FieldMask{Paths: []string{"no_such_field"}}, uuid.NewString()))
	if _, err := stream.stream.Recv(); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("got %v, want InvalidArgument", err)
	}
}

func TestSelectedSummariesRefuseAnEmptyFieldSet(t *testing.T) {
	server := newServer(t, time.Hour)
	stream := server.watch(t, selectedWatch(nil, uuid.NewString()))
	if _, err := stream.stream.Recv(); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("got %v, want InvalidArgument", err)
	}
}
