package historydata

import (
	"context"
	"errors"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type fakeHistoryReader struct {
	observations *historypb.ReadSessionObservationsResponse
	events       *historypb.ReadSessionEventsResponse
	entities     *historypb.ReadThreadEntitiesResponse
	readEvents   error
}

func (f *fakeHistoryReader) ReadSessionEvents(context.Context, *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
	if f.readEvents != nil {
		return nil, f.readEvents
	}
	return f.events, nil
}

func (f *fakeHistoryReader) ReadSessionObservations(context.Context, *historypb.ReadSessionObservationsRequest) (*historypb.ReadSessionObservationsResponse, error) {
	return f.observations, nil
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

func (*fakeHistoryReader) FindSessions(context.Context, *historypb.FindSessionsRequest) (*historypb.FindSessionsResponse, error) {
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

func TestEventsAcceptSettlementRangesButRejectMissingPublishedEvents(t *testing.T) {
	id := uuid.New()
	store, err := New(&fakeHistoryReader{events: &historypb.ReadSessionEventsResponse{
		LastCursor:  3,
		Entries:     []*eventlogpb.EventEntry{{Cursor: 1}, {Cursor: 3}},
		Settlements: []*historypb.SettledDeltas{{Ranges: []*historypb.CursorRange{{First: 2, Last: 2}}}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	entries, err := store.Events(t.Context(), id, 0, 2)
	if err != nil || len(entries) != 2 {
		t.Fatalf("Events = %v, %v", entries, err)
	}
	store.reader = &fakeHistoryReader{events: &historypb.ReadSessionEventsResponse{
		LastCursor: 3, Entries: []*eventlogpb.EventEntry{{Cursor: 1}, {Cursor: 3}},
	}}
	_, err = store.Events(t.Context(), id, 0, 2)
	if !errors.Is(err, ErrInvalidPage) {
		t.Fatalf("Events error = %v, want ErrInvalidPage", err)
	}
}

func TestFoldSummaryPreservesSandboxIncarnation(t *testing.T) {
	uid := uuid.NewString()
	view, err := foldSummary(&historypb.SessionSummary{
		Sandbox: &historypb.SandboxRef{Name: "worker", Uid: &uid},
	})
	if err != nil {
		t.Fatal(err)
	}
	if view.Sandbox != "worker" || view.SandboxUID != uid {
		t.Fatalf("summary Sandbox identity = %q/%q, want worker/%q", view.Sandbox, view.SandboxUID, uid)
	}
}

func TestAdmittedCommandRequiresTheExactImmutableCommand(t *testing.T) {
	id := uuid.New()
	original := &commandpb.Command{CommandId: "retry-1", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}}}
	reader := &fakeHistoryReader{
		entities: &historypb.ReadThreadEntitiesResponse{Entities: []*historypb.ThreadEntity{{ViewJson: `{"entity_kind":"command","entity_id":"retry-1","cursor":1}`}}},
		events: &historypb.ReadSessionEventsResponse{LastCursor: 1, Entries: []*eventlogpb.EventEntry{{
			Cursor: 1, Origin: &eventlogpb.EventOrigin{Sequence: 1},
			Event: &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{CommandAdmitted: &eventpb.CommandAdmitted{Command: original}}},
		}}},
	}
	store, err := New(reader)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.AdmittedCommand(t.Context(), id, original); err != nil {
		t.Fatalf("exact retry: %v", err)
	}
	different := &commandpb.Command{CommandId: "retry-1", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "different"}}}
	if _, err := store.AdmittedCommand(t.Context(), id, different); !errors.Is(err, ErrCommandConflict) {
		t.Fatalf("different retry error = %v, want ErrCommandConflict", err)
	}
}
