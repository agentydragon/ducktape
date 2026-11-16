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
	summary    *historypb.SessionSummary
	summaryErr error
}

func (f *fakeHistoryReader) GetSessionSummary(context.Context, *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	return f.summary, f.summaryErr
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
