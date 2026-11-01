package historydata

import (
	"context"
	"errors"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type fakeHistoryReader struct {
	observations            *historypb.ReadSessionObservationsResponse
	observationRequests     []*historypb.ReadSessionObservationsRequest
	ignoreObservationBounds bool
	events                  *historypb.ReadSessionEventsResponse
	entities                *historypb.ReadThreadEntitiesResponse
	readEvents              error
	findSessions            *historypb.FindSessionsRequest
}

func uint64Pointer(cursor uint64) *uint64 { return &cursor }

func (f *fakeHistoryReader) ReadSessionEvents(context.Context, *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
	if f.readEvents != nil {
		return nil, f.readEvents
	}
	return f.events, nil
}

func (f *fakeHistoryReader) ReadSessionObservations(_ context.Context, request *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error) {
	f.observationRequests = append(f.observationRequests, request)
	if f.observations == nil || f.ignoreObservationBounds {
		return f.observations, nil
	}
	page := *f.observations
	page.Observations = nil
	for _, observation := range f.observations.GetObservations() {
		if request.AfterCursor != nil && observation.GetCursor() <= request.GetAfterCursor() {
			continue
		}
		if request.BeforeCursor != nil && observation.GetCursor() >= request.GetBeforeCursor() {
			continue
		}
		page.Observations = append(page.Observations, observation)
	}
	return &page, nil
}

func (*fakeHistoryReader) GetSessionSummary(context.Context, *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	return nil, status.Error(codes.NotFound, "missing")
}

func (*fakeHistoryReader) BatchGetSessionSummaries(context.Context, *historypb.BatchGetSessionSummariesRequest) (*historypb.BatchGetSessionSummariesResponse, error) {
	return &historypb.BatchGetSessionSummariesResponse{}, nil
}

func (f *fakeHistoryReader) ReadThreadEntities(context.Context, *historypb.ReadThreadEntitiesRequest) (*historypb.ReadThreadEntitiesResponse, error) {
	return f.entities, nil
}

func (*fakeHistoryReader) ReadEntityEvidence(context.Context, *historypb.ReadEntityEvidenceRequest) (*historypb.ReadEntityEvidenceResponse, error) {
	return nil, errors.New("unexpected ReadEntityEvidence")
}

func (*fakeHistoryReader) ReadNativeFrames(context.Context, *historypb.ReadNativeFramesRequest) (*historypb.ReadNativeFramesResponse, error) {
	return nil, errors.New("unexpected ReadNativeFrames")
}

func (f *fakeHistoryReader) FindSessions(_ context.Context, request *historypb.FindSessionsRequest) (*historypb.FindSessionsResponse, error) {
	f.findSessions = request
	return &historypb.FindSessionsResponse{}, nil
}

func TestObservationWindowSkipsSettledCursorAndPublishesStablePageTokens(t *testing.T) {
	store, err := New(&fakeHistoryReader{observations: &historypb.ReadSessionObservationsResponse{
		LastCursor: 3,
		Observations: []*historypb.SessionObservation{
			{Cursor: 1, Kind: "session_opened"},
			{Cursor: 3, Kind: "turn_started"},
		},
		Settled: []*historypb.CursorRange{{First: 2, Last: 2}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	page, err := store.Observations(t.Context(), uuid.New(), nil, nil, 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Observations) != 2 || page.Observations[1].Cursor != "3" {
		t.Fatalf("observations = %#v", page.Observations)
	}
	if page.NextBeforeCursor != nil || page.NextAfterCursor != nil {
		t.Fatalf("terminal page tokens = before:%v after:%v", page.NextBeforeCursor, page.NextAfterCursor)
	}
}

func TestObservationWindowRejectsUncoveredCursorGap(t *testing.T) {
	store, err := New(&fakeHistoryReader{observations: &historypb.ReadSessionObservationsResponse{
		LastCursor:   3,
		Observations: []*historypb.SessionObservation{{Cursor: 1}, {Cursor: 3}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	_, err = store.Observations(t.Context(), uuid.New(), nil, nil, 2)
	if !errors.Is(err, ErrInvalidPage) {
		t.Fatalf("Observations error = %v, want ErrInvalidPage", err)
	}
}

func TestObservationWindowTreatsRemovedSuffixAsCovered(t *testing.T) {
	store, err := New(&fakeHistoryReader{observations: &historypb.ReadSessionObservationsResponse{
		LastCursor: 7,
		Observations: []*historypb.SessionObservation{
			{Cursor: 1, Kind: "harness_started"},
			{Cursor: 2, Kind: "harness_started"},
		},
		Settled: []*historypb.CursorRange{{First: 3, Last: 7}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	page, err := store.Observations(t.Context(), uuid.New(), nil, nil, 3)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Observations) != 2 || page.Observations[1].Cursor != "2" || page.NextAfterCursor != nil {
		t.Fatalf("terminal page with removed suffix = %+v", page)
	}

	after := uint64(2)
	page, err = store.Observations(t.Context(), uuid.New(), nil, &after, 3)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Observations) != 0 || page.NextAfterCursor != nil {
		t.Fatalf("page after removed suffix = %+v", page)
	}
}

func TestObservationWindowRejectsInvalidPages(t *testing.T) {
	for _, test := range []struct {
		name  string
		page  *historypb.ReadSessionObservationsResponse
		after *uint64
		limit int
	}{
		{
			name:  "gap",
			page:  &historypb.ReadSessionObservationsResponse{LastCursor: 3, Observations: []*historypb.SessionObservation{{Cursor: 1}, {Cursor: 3}}},
			limit: 2,
		},
		{
			name:  "entry beyond watermark",
			page:  &historypb.ReadSessionObservationsResponse{Observations: []*historypb.SessionObservation{{Cursor: 1}}},
			limit: 1,
		},
		{
			name:  "missing entries",
			page:  &historypb.ReadSessionObservationsResponse{LastCursor: 3},
			limit: 2,
		},
		{
			name:  "resume ahead of watermark",
			page:  &historypb.ReadSessionObservationsResponse{},
			after: uint64Pointer(1),
			limit: 1,
		},
		{
			name: "entry before requested cursor",
			page: &historypb.ReadSessionObservationsResponse{
				LastCursor: 7,
				Observations: []*historypb.SessionObservation{
					{Cursor: 1},
					{Cursor: 2},
				},
			},
			after: uint64Pointer(2),
			limit: 3,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			store, err := New(&fakeHistoryReader{
				observations: test.page, ignoreObservationBounds: test.name == "entry before requested cursor",
			})
			if err != nil {
				t.Fatal(err)
			}
			_, err = store.Observations(t.Context(), uuid.New(), nil, test.after, test.limit)
			if !errors.Is(err, ErrInvalidPage) {
				t.Fatalf("Observations error = %v, want ErrInvalidPage", err)
			}
		})
	}
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
