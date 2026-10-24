package historywatch

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

type streamScript struct {
	watchErr  error
	responses []*historypb.WatchSessionSummariesResponse
	recvErr   error
}

type requestPosition struct {
	present bool
	value   uint64
}

type fakeClient struct {
	mu       sync.Mutex
	scripts  []streamScript
	requests []requestPosition
	contexts []context.Context
	redials  int
}

func (c *fakeClient) WatchSessionSummaries(
	ctx context.Context, request *historypb.WatchSessionSummariesRequest,
) (Stream, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	position := requestPosition{}
	if request.GetOpenedAfter() != 0 || request.OpenedAfter != nil {
		position = requestPosition{present: true, value: request.GetOpenedAfter()}
	}
	c.requests = append(c.requests, position)
	c.contexts = append(c.contexts, ctx)
	if len(c.scripts) == 0 {
		return &fakeStream{ctx: ctx}, nil
	}
	script := c.scripts[0]
	c.scripts = c.scripts[1:]
	if script.watchErr != nil {
		return nil, script.watchErr
	}
	return &fakeStream{ctx: ctx, responses: script.responses, err: script.recvErr}, nil
}

func (c *fakeClient) Redial() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.redials++
}

func (c *fakeClient) snapshot() ([]requestPosition, int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]requestPosition(nil), c.requests...), c.redials
}

func (c *fakeClient) contextsSnapshot() []context.Context {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]context.Context(nil), c.contexts...)
}

type fakeStream struct {
	ctx       context.Context
	responses []*historypb.WatchSessionSummariesResponse
	err       error
}

func (s *fakeStream) Recv() (*historypb.WatchSessionSummariesResponse, error) {
	if len(s.responses) != 0 {
		response := s.responses[0]
		s.responses = s.responses[1:]
		return response, nil
	}
	if s.err != nil {
		return nil, s.err
	}
	<-s.ctx.Done()
	return nil, s.ctx.Err()
}

type fakeThreadStore struct {
	mu        sync.Mutex
	ids       []uuid.UUID
	autoTitle []bool
	failFirst bool
}

func (s *fakeThreadStore) OpenThread(_ context.Context, id uuid.UUID, autoTitle bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ids = append(s.ids, id)
	s.autoTitle = append(s.autoTitle, autoTitle)
	if s.failFirst {
		s.failFirst = false
		return errors.New("temporary database error")
	}
	return nil
}

func (s *fakeThreadStore) snapshot() ([]uuid.UUID, []bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]uuid.UUID(nil), s.ids...), append([]bool(nil), s.autoTitle...)
}

func testOptions() Options {
	return Options{
		RetryInterval: time.Millisecond,
		Logger:        slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

func changedResponse() *historypb.WatchSessionSummariesResponse {
	return &historypb.WatchSessionSummariesResponse{
		Observation: &historypb.WatchSessionSummariesResponse_Changed{Changed: &emptypb.Empty{}},
	}
}

func reconnectResponse() *historypb.WatchSessionSummariesResponse {
	return &historypb.WatchSessionSummariesResponse{
		Observation: &historypb.WatchSessionSummariesResponse_ReconnectRequired{ReconnectRequired: &emptypb.Empty{}},
	}
}

func openedResponse(id string, position uint64) *historypb.WatchSessionSummariesResponse {
	return &historypb.WatchSessionSummariesResponse{
		Observation: &historypb.WatchSessionSummariesResponse_SessionOpened{
			SessionOpened: &historypb.SessionOpened{SessionId: id, Position: position},
		},
	}
}

func waitFor(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("condition was not met before timeout")
}

func runUntilCancelled(t *testing.T, run func(context.Context) error) context.CancelFunc {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-done:
			if !errors.Is(err, context.Canceled) {
				t.Errorf("Run returned %v, want context.Canceled", err)
			}
		case <-time.After(3 * time.Second):
			t.Error("Run did not stop after cancellation")
		}
	})
	return cancel
}

func TestHistoryWatchNotifiesForChangesAndRenewsWithoutRetry(t *testing.T) {
	client := &fakeClient{scripts: []streamScript{{responses: []*historypb.WatchSessionSummariesResponse{
		changedResponse(), changedResponse(), reconnectResponse(),
	}}}}
	notices := make(chan struct{}, 4)
	watch, err := NewHistoryWatch(client, func() { notices <- struct{}{} }, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	cancel := runUntilCancelled(t, watch.Run)
	waitFor(t, func() bool { return len(notices) == 2 })
	waitFor(t, func() bool {
		requests, _ := client.snapshot()
		return len(requests) >= 2
	})
	_, redials := client.snapshot()
	if redials != 0 {
		t.Fatalf("planned renewal caused %d redials, want 0", redials)
	}
	cancel()
}

func TestHistoryWatchRetriesAndRedialsAfterFourFailedStreams(t *testing.T) {
	client := &fakeClient{scripts: []streamScript{
		{watchErr: status.Error(codes.Unavailable, "dial failed")},
		{recvErr: status.Error(codes.DeadlineExceeded, "watch deadline")},
		{recvErr: io.EOF},
		{recvErr: status.Error(codes.Unavailable, "stream ended")},
		{responses: []*historypb.WatchSessionSummariesResponse{reconnectResponse()}},
	}}
	watch, err := NewHistoryWatch(client, func() { t.Error("failed stream unexpectedly notified") }, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	cancel := runUntilCancelled(t, watch.Run)
	waitFor(t, func() bool {
		requests, redials := client.snapshot()
		return len(requests) >= 6 && redials == 1
	})
	cancel()
}

func TestPlannedRenewalResetsConsecutiveFailureCount(t *testing.T) {
	failed := func() streamScript { return streamScript{recvErr: status.Error(codes.Unavailable, "stream failed")} }
	client := &fakeClient{scripts: []streamScript{
		failed(), failed(), failed(),
		{responses: []*historypb.WatchSessionSummariesResponse{reconnectResponse()}},
		failed(), failed(), failed(),
	}}
	watch, err := NewHistoryWatch(client, func() {}, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	cancel := runUntilCancelled(t, watch.Run)
	waitFor(t, func() bool {
		requests, _ := client.snapshot()
		return len(requests) >= 8
	})
	_, redials := client.snapshot()
	if redials != 0 {
		t.Fatalf("planned renewal failed to reset failure count; redials = %d", redials)
	}
	cancel()
}

func TestSessionOpeningsReplaysFailedOpenAndAdvancesPositionAfterSuccess(t *testing.T) {
	id1, id2 := uuid.New(), uuid.New()
	client := &fakeClient{scripts: []streamScript{
		{responses: []*historypb.WatchSessionSummariesResponse{changedResponse(), openedResponse(id1.String(), 1)}},
		{responses: []*historypb.WatchSessionSummariesResponse{
			changedResponse(), openedResponse(id1.String(), 1), openedResponse(id2.String(), 2), reconnectResponse(),
		}},
	}}
	store := &fakeThreadStore{failFirst: true}
	openings, err := NewSessionOpenings(client, store, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	cancel := runUntilCancelled(t, openings.Run)
	waitFor(t, func() bool {
		requests, _ := client.snapshot()
		ids, _ := store.snapshot()
		return len(requests) >= 3 && len(ids) == 3
	})
	contexts := client.contextsSnapshot()
	select {
	case <-contexts[0].Done():
	case <-time.After(time.Second):
		t.Fatal("failed opening left its History stream active")
	}
	requests, redials := client.snapshot()
	wantPositions := []requestPosition{{present: true, value: 0}, {present: true, value: 0}, {present: true, value: 2}}
	if len(requests) < len(wantPositions) {
		t.Fatalf("only got %d requests, want at least %d", len(requests), len(wantPositions))
	}
	for i, want := range wantPositions {
		if requests[i] != want {
			t.Errorf("request %d opened_after = %+v, want %+v", i, requests[i], want)
		}
	}
	ids, autoTitle := store.snapshot()
	wantIDs := []uuid.UUID{id1, id1, id2}
	for i, want := range wantIDs {
		if ids[i] != want {
			t.Errorf("OpenThread call %d ID = %s, want %s", i, ids[i], want)
		}
		if autoTitle[i] {
			t.Errorf("OpenThread call %d enabled auto-title; want default false", i)
		}
	}
	if redials != 0 {
		t.Errorf("redials = %d, want 0", redials)
	}
	cancel()
}

func TestSessionOpeningsRejectsNonCanonicalSessionID(t *testing.T) {
	id := uuid.New()
	nonCanonical := "{" + id.String() + "}"
	client := &fakeClient{scripts: []streamScript{{responses: []*historypb.WatchSessionSummariesResponse{
		openedResponse(nonCanonical, 1),
	}}}}
	store := &fakeThreadStore{}
	openings, err := NewSessionOpenings(client, store, testOptions())
	if err != nil {
		t.Fatal(err)
	}
	err = openings.Run(context.Background())
	if !errors.Is(err, ErrInvalidSessionID) {
		t.Fatalf("Run error = %v, want invalid Session ID error", err)
	}
	ids, _ := store.snapshot()
	if len(ids) != 0 {
		t.Fatalf("OpenThread called %d times for malformed Session ID", len(ids))
	}
}
