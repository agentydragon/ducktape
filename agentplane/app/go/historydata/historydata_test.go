package historydata

import (
	"context"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type fakeHistoryReader struct {
	observations        *historypb.ReadSessionObservationsResponse
	observationRequests []*historypb.ReadSessionObservationsRequest
	findSessions        *historypb.FindSessionsRequest
}

func (f *fakeHistoryReader) ReadSessionObservations(_ context.Context, request *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error) {
	f.observationRequests = append(f.observationRequests, request)
	return f.observations, nil
}

func (*fakeHistoryReader) GetSessionSummary(context.Context, *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	return nil, status.Error(codes.NotFound, "missing")
}

func (*fakeHistoryReader) BatchGetSessionSummaries(context.Context, *historypb.BatchGetSessionSummariesRequest) (*historypb.BatchGetSessionSummariesResponse, error) {
	return &historypb.BatchGetSessionSummariesResponse{}, nil
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

func TestFoldSummaryPreservesSandboxIncarnation(t *testing.T) {
	uid := uuid.NewString()
	view, err := foldSummary(&historypb.SessionSummary{
		Sandbox: &historypb.SandboxRef{Name: "worker", Uid: &uid}, CommittedCursor: 19,
	})
	if err != nil {
		t.Fatal(err)
	}
	if view.Sandbox != "worker" || view.SandboxUID != uid {
		t.Fatalf("summary Sandbox identity = %q/%q, want worker/%q", view.Sandbox, view.SandboxUID, uid)
	}
	if view.CommittedCursor != 19 {
		t.Fatalf("committed cursor = %d, want 19", view.CommittedCursor)
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
