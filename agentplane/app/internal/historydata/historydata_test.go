package historydata

import (
	"context"
	"errors"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
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

func TestCurrentEpoch(t *testing.T) {
	epoch := "epoch-1"
	serviceErr := errors.New("History Service unavailable")
	for _, test := range []struct {
		name      string
		summary   *historypb.SessionSummary
		err       error
		wantEpoch string
		wantFound bool
		wantErr   error
	}{
		{"materialized Session", &historypb.SessionSummary{ProjectionEpoch: &epoch}, nil, epoch, true, nil},
		{"no epoch yet", &historypb.SessionSummary{}, nil, "", false, nil},
		{"unknown Session", nil, status.Error(codes.NotFound, "missing"), "", false, nil},
		{"service error", nil, serviceErr, "", false, serviceErr},
	} {
		t.Run(test.name, func(t *testing.T) {
			store, err := New(&fakeHistoryReader{summary: test.summary, summaryErr: test.err})
			if err != nil {
				t.Fatal(err)
			}
			got, found, err := store.CurrentEpoch(t.Context(), uuid.New())
			if got != test.wantEpoch || found != test.wantFound || !errors.Is(err, test.wantErr) {
				t.Fatalf("CurrentEpoch() = %q, %v, %v; want %q, %v, %v", got, found, err, test.wantEpoch, test.wantFound, test.wantErr)
			}
		})
	}
}
