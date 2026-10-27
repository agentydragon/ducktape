package api

import (
	"io"
	"slices"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/projection"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type watching struct {
	t      *testing.T
	stream grpc.ServerStreamingClient[historypb.WatchSessionSummariesResponse]
}

func (s testServer) watch(t *testing.T, request *historypb.WatchSessionSummariesRequest) watching {
	t.Helper()
	stream, err := s.client.WatchSessionSummaries(bearing(t, readerToken), request)
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
	if opened := response.GetSessionOpened(); opened.GetSessionId() != sessionID.String() || opened.GetPosition() != position {
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
	if err := s.db.QueryRowContext(t.Context(), "SELECT COALESCE(max(opened_position), 0) FROM session_history").Scan(&position); err != nil {
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

func TestAWatchAskingForOpeningsNamesTheSessionsRecordedAfterThePosition(t *testing.T) {
	server := newServer(t, time.Hour)
	before := server.currentPosition(t)
	first, firstPosition := server.record(t)
	second, secondPosition := server.record(t)
	server.settled(t)

	watch := server.watch(t, &historypb.WatchSessionSummariesRequest{OpenedAfter: proto.Uint64(before)})
	watch.wantChanged()
	watch.wantOpened(first, firstPosition)
	watch.wantOpened(second, secondPosition)

	third, thirdPosition := server.record(t)
	watch.wantChanged()
	watch.wantOpened(third, thirdPosition)

	// A caller that reconnects with the last position it handled is told of what came after it only.
	reconnected := server.watch(t, &historypb.WatchSessionSummariesRequest{OpenedAfter: proto.Uint64(secondPosition)})
	reconnected.wantChanged()
	reconnected.wantOpened(third, thirdPosition)
}

func TestOpeningsBeyondOnePageAreAllNamedInOrder(t *testing.T) {
	server := newServer(t, time.Hour)
	before := server.currentPosition(t)
	recorded := map[uuid.UUID]bool{}
	for range openingsPage + 1 {
		sessionID, _ := server.record(t)
		recorded[sessionID] = true
	}
	server.settled(t)

	watch := server.watch(t, &historypb.WatchSessionSummariesRequest{OpenedAfter: proto.Uint64(before)})
	watch.wantChanged()
	var positions []uint64
	for range openingsPage + 1 {
		opened := watch.receive().GetSessionOpened()
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

func TestAWatchEndsWithReconnectRequiredWhenItsLeaseEnds(t *testing.T) {
	server := newServer(t, 200*time.Millisecond)
	watch := server.watch(t, &historypb.WatchSessionSummariesRequest{})
	watch.wantChanged()
	watch.wantReconnectAndCleanEnd()
}

func TestDrainingEndsOpenWatches(t *testing.T) {
	server := newServer(t, time.Hour)
	watch := server.watch(t, &historypb.WatchSessionSummariesRequest{})
	watch.wantChanged()
	server.service.Drain()
	watch.wantReconnectAndCleanEnd()
}

// blockedStream is a watch whose caller has stopped reading, so a write never completes.
type blockedStream struct {
	grpc.ServerStream
	release chan struct{}
}

func (s blockedStream) Send(*historypb.WatchSessionSummariesResponse) error {
	<-s.release
	return nil
}

func TestAWriteTheCallerDoesNotReadFailsAtTheTimeout(t *testing.T) {
	stream := blockedStream{release: make(chan struct{})}
	t.Cleanup(func() { close(stream.release) })
	failed := make(chan error, 1)
	go func() {
		failed <- sendWithin(t.Context(), stream, 10*time.Millisecond, &historypb.WatchSessionSummariesResponse{})
	}()
	select {
	case err := <-failed:
		if got := status.Code(err); got != codes.DeadlineExceeded {
			t.Errorf("%v, want DeadlineExceeded", got)
		}
	case <-time.After(callDeadline):
		t.Fatal("the write was not given up on")
	}
}
