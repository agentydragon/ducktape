package historydata

import (
	"context"
	"errors"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type fakeHistoryReader struct {
	observations        *historypb.ReadSessionObservationsResponse
	observationRequests []*historypb.ReadSessionObservationsRequest
	findSessions        *historypb.FindSessionsRequest
	summary             *historypb.SessionSummary
	summaryErr          error
	batch               *historypb.BatchGetSessionSummariesResponse
}

func (f *fakeHistoryReader) ReadSessionObservations(_ context.Context, request *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error) {
	f.observationRequests = append(f.observationRequests, request)
	return f.observations, nil
}

func (f *fakeHistoryReader) GetSessionSummary(context.Context, *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	return f.summary, f.summaryErr
}

func (f *fakeHistoryReader) BatchGetSessionSummaries(context.Context, *historypb.BatchGetSessionSummariesRequest) (*historypb.BatchGetSessionSummariesResponse, error) {
	return f.batch, nil
}

func (f *fakeHistoryReader) FindSessions(_ context.Context, request *historypb.FindSessionsRequest) (*historypb.FindSessionsResponse, error) {
	f.findSessions = request
	return &historypb.FindSessionsResponse{}, nil
}

func TestReadWatermarkReturnsTheHistoryCommittedCursor(t *testing.T) {
	reader := &fakeHistoryReader{observations: &historypb.ReadSessionObservationsResponse{LastCursor: 5}}
	store, err := New(reader)
	if err != nil {
		t.Fatal(err)
	}
	id := uuid.New()
	watermark, err := store.ReadWatermark(t.Context(), id)
	if err != nil {
		t.Fatal(err)
	}
	if watermark != 5 {
		t.Fatalf("ReadWatermark() = %d, want committed cursor 5", watermark)
	}
	if len(reader.observationRequests) != 1 || reader.observationRequests[0].GetSessionId() != id.String() || reader.observationRequests[0].GetLimit() != 1 {
		t.Fatalf("ReadWatermark requests = %+v, want one metadata request for the Session", reader.observationRequests)
	}
}

func TestGetSummaryReturnsGeneratedMessage(t *testing.T) {
	id := uuid.New()
	uid := uuid.NewString()
	want := &historypb.SessionSummary{
		SessionId: id.String(), Sandbox: &historypb.SandboxRef{Name: "worker", Uid: &uid},
		CommittedCursor: 19, Attached: &sessionpb.Attached{SessionId: id.String()},
	}
	reader := &fakeHistoryReader{summary: want}
	store, err := New(reader)
	if err != nil {
		t.Fatal(err)
	}
	got, err := store.GetSummary(t.Context(), id)
	if err != nil || got != want {
		t.Fatalf("GetSummary() = %v, %v, want generated SessionSummary", got, err)
	}
	reader.summaryErr = status.Error(codes.NotFound, "missing")
	if got, err := store.GetSummary(t.Context(), id); err != nil || got != nil {
		t.Fatalf("GetSummary(missing) = %v, %v, want nil, nil", got, err)
	}
}

func TestGetSummaryRejectsUnknownAttachedFeedStatus(t *testing.T) {
	reader := &fakeHistoryReader{summary: &historypb.SessionSummary{
		Attached: &sessionpb.Attached{}, FeedStatus: historypb.FeedStatus(999),
	}}
	store, err := New(reader)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.GetSummary(t.Context(), uuid.New()); !errors.Is(err, ErrInvalidPage) {
		t.Fatalf("GetSummary(unknown feed status) = %v, want ErrInvalidPage", err)
	}
}

func TestBatchGetSummariesOmitsMissingSessions(t *testing.T) {
	id, missing := uuid.New(), uuid.New()
	want := &historypb.SessionSummary{SessionId: id.String()}
	store, err := New(&fakeHistoryReader{batch: &historypb.BatchGetSessionSummariesResponse{Summaries: []*historypb.SessionSummary{want}}})
	if err != nil {
		t.Fatal(err)
	}
	got, err := store.BatchGetSummaries(t.Context(), []uuid.UUID{id, missing})
	if err != nil || got[id] != want || got[missing] != nil {
		t.Fatalf("BatchGetSummaries() = %v, %v, want only the existing summary", got, err)
	}
}

func TestFindSessionsUsesSandboxUIDFilter(t *testing.T) {
	uid := uuid.NewString()
	reader := &fakeHistoryReader{}
	store, err := New(reader)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.FindSessions(t.Context(), nil, nil, &uid); err != nil {
		t.Fatal(err)
	}
	if reader.findSessions == nil {
		t.Fatal("FindSessions did not call the History reader")
	}
	filter, ok := reader.findSessions.GetSandbox().(*historypb.FindSessionsRequest_SandboxUid)
	if !ok || filter.SandboxUid != uid {
		t.Fatalf("FindSessions Sandbox filter = %T (%v), want UID %q", reader.findSessions.GetSandbox(), reader.findSessions.GetSandbox(), uid)
	}
}
