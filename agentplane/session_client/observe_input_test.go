package sessionclient

import (
	"context"
	"encoding/json"
	"io"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/types/known/emptypb"
)

const (
	testSessionID = "58cae82c-453d-4de7-ae5b-17a8f7596077"
	testCommandID = "c40dd833-326f-4be1-b42a-4bb74c76c49a"
)

type dispatcherStub struct {
	dispatcherpb.SessionDispatcherClient
	dispatch *dispatcherpb.Dispatch
	watch    dispatcherpb.SessionDispatcher_WatchDispatchesClient
	watchReq *dispatcherpb.WatchDispatchesRequest
}

func (s dispatcherStub) GetDispatch(_ context.Context, _ *dispatcherpb.GetDispatchRequest, _ ...grpc.CallOption) (*dispatcherpb.GetDispatchResponse, error) {
	return &dispatcherpb.GetDispatchResponse{Dispatch: s.dispatch}, nil
}

func (s *dispatcherStub) WatchDispatches(_ context.Context, request *dispatcherpb.WatchDispatchesRequest, _ ...grpc.CallOption) (dispatcherpb.SessionDispatcher_WatchDispatchesClient, error) {
	s.watchReq = request
	return s.watch, nil
}

type historyStub struct {
	historypb.HistoryServiceClient
	stream  historypb.HistoryService_FollowSessionClient
	request *historypb.FollowSessionRequest
	calls   int
}

func (s *historyStub) FollowSession(_ context.Context, request *historypb.FollowSessionRequest, _ ...grpc.CallOption) (historypb.HistoryService_FollowSessionClient, error) {
	s.request = request
	s.calls++
	return s.stream, nil
}

type dispatchStreamStub struct {
	dispatcherpb.SessionDispatcher_WatchDispatchesClient
	frames []*dispatcherpb.WatchDispatchesResponse
	index  int
}

func (s *dispatchStreamStub) Recv() (*dispatcherpb.WatchDispatchesResponse, error) {
	if s.index >= len(s.frames) {
		return nil, io.EOF
	}
	frame := s.frames[s.index]
	s.index++
	return frame, nil
}

type historyStreamStub struct {
	historypb.HistoryService_FollowSessionClient
	frames []*historypb.FollowSessionResponse
	index  int
}

func (s *historyStreamStub) Recv() (*historypb.FollowSessionResponse, error) {
	if s.index >= len(s.frames) {
		return nil, io.EOF
	}
	frame := s.frames[s.index]
	s.index++
	return frame, nil
}

func TestObserveInputCorrelatesCommandWithCompletedTurn(t *testing.T) {
	const (
		turnID         = "turn-17"
		otherCommandID = "9b536b52-555e-4e85-b45c-d516af196dfc"
	)
	dispatch := admittedDispatch(18)
	frames := []*historypb.FollowSessionResponse{
		entriesFrame(21,
			entry(20, &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{
				OriginCommandIds: []string{otherCommandID, testCommandID}, TurnId: turnID,
			}}}),
			entry(21, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: turnID}}}),
		),
		entriesFrame(22, entry(22, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
			Outcome: &eventpb.ItemCompleted_Text{Text: "done"},
		}}})),
		entriesFrame(23, entry(23, &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{
			TurnId: turnID, Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED,
		}}})),
	}
	history := &historyStub{stream: &historyStreamStub{frames: frames}}
	got, err := ObserveInput(context.Background(), &dispatcherStub{dispatch: dispatch}, history,
		ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID})
	if err != nil {
		t.Fatalf("ObserveInput() error = %v", err)
	}
	if got.Observation.Kind != ObservationTurnComplete {
		t.Fatalf("observation kind = %q, want %q", got.Observation.Kind, ObservationTurnComplete)
	}
	turn := got.Observation.Turn
	if turn == nil || turn.TurnID != turnID || len(turn.InputCommandIDs) != 2 || turn.InputCommandIDs[1] != testCommandID {
		t.Fatalf("correlated turn = %#v", turn)
	}
	if turn.FinalAssistantMessage == nil || *turn.FinalAssistantMessage != "done" || turn.Status != "TURN_STATUS_COMPLETED" {
		t.Fatalf("turn result = %#v", turn)
	}
	if got.Dispatch.Source != "dispatcher" || got.Dispatch.Kind != "observed" || got.Observation.Source != "history" ||
		got.Observation.ThroughCursor != "23" || got.Observation.ResumeToken == "" {
		t.Fatalf("source coverage = dispatch %#v, observation %#v", got.Dispatch, got.Observation)
	}
	if history.request.GetAfterCursor() != 17 || history.request.GetFollow() {
		t.Fatalf("History request = %#v", history.request)
	}
}

func TestObserveInputKeepsCursorWhenCommandFails(t *testing.T) {
	frames := []*historypb.FollowSessionResponse{entriesFrame(20, entry(20, &eventpb.Event{
		Observation: &eventpb.Event_CommandFailed{CommandFailed: &eventpb.CommandFailed{CommandId: testCommandID, Reason: "runner rejected input"}},
	}))}
	history := &historyStub{stream: &historyStreamStub{frames: frames}}
	got, err := ObserveInput(context.Background(), &dispatcherStub{dispatch: admittedDispatch(18)}, history,
		ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID})
	if err != nil {
		t.Fatalf("ObserveInput() error = %v", err)
	}
	if got.Observation.Kind != ObservationCommandFailed || got.Observation.Reason == nil || *got.Observation.Reason != "runner rejected input" {
		t.Fatalf("observation = %#v", got.Observation)
	}
	if got.Observation.ThroughCursor != "20" || got.Observation.ResumeToken == "" {
		t.Fatalf("History cursor was not retained: %#v", got.Observation)
	}
}

func TestObserveInputConfirmationMatchesApprovedObservationShape(t *testing.T) {
	const turnID = "turn-confirmed"
	history := &historyStub{stream: &historyStreamStub{frames: []*historypb.FollowSessionResponse{
		entriesFrame(11, entry(11, &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{
			HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{OriginCommandIds: []string{testCommandID}, TurnId: turnID},
		}})),
		{ThroughCursor: 12, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}},
	}}}
	got, err := ObserveInput(context.Background(), &dispatcherStub{dispatch: admittedDispatch(10)}, history,
		ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID})
	if err != nil {
		t.Fatalf("ObserveInput() error = %v", err)
	}
	if got.Observation.Kind != ObservationInputConfirmed || got.Observation.TurnID == nil || *got.Observation.TurnID != turnID || got.Observation.Turn != nil {
		t.Fatalf("observation = %#v", got.Observation)
	}
	encoded, err := json.Marshal(got)
	if err != nil {
		t.Fatalf("marshal observation: %v", err)
	}
	var object map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &object); err != nil {
		t.Fatalf("unmarshal observation JSON: %v", err)
	}
	if object["history"] != nil {
		t.Fatalf("unexpected sibling History field in schema: %s", encoded)
	}
	var observation map[string]json.RawMessage
	if err := json.Unmarshal(object["observation"], &observation); err != nil {
		t.Fatalf("unmarshal observation fields: %v", err)
	}
	if string(observation["source"]) != `"history"` || string(observation["kind"]) != `"input_confirmed"` || observation["turn_id"] == nil {
		t.Fatalf("observation JSON = %s", object["observation"])
	}
}

func TestObserveInputReportsRemovedHistoryGap(t *testing.T) {
	history := &historyStub{stream: &historyStreamStub{frames: []*historypb.FollowSessionResponse{{
		Frame: &historypb.FollowSessionResponse_Removed{Removed: &historypb.CursorRange{First: 19, Last: 21}},
	}}}}
	got, err := ObserveInput(context.Background(), &dispatcherStub{dispatch: admittedDispatch(18)}, history,
		ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID})
	if err != nil {
		t.Fatalf("ObserveInput() error = %v", err)
	}
	if got.Observation.Error == nil || got.Observation.Error.Code != codes.DataLoss.String() || len(got.Observation.Gaps) != 1 {
		t.Fatalf("History gap = %#v", got.Observation)
	}
	if got.Observation.Kind != ObservationNotObserved {
		t.Fatalf("observation = %q, want not_observed", got.Observation.Kind)
	}
}

func TestObserveInputRejectsResumeTokenForDifferentCommandBeforeReading(t *testing.T) {
	wrong := encodeResumeToken(resumeToken{Version: 1, SessionID: testSessionID, CommandID: "11111111-1111-4111-8111-111111111111"})
	history := &historyStub{}
	_, err := ObserveInput(context.Background(), &dispatcherStub{dispatch: admittedDispatch(18)}, history,
		ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID, ResumeToken: wrong})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("ObserveInput() error = %v, want InvalidArgument", err)
	}
	if history.request != nil {
		t.Fatalf("History was called for an invalid resume token: %#v", history.request)
	}
}

func TestObserveInputReturnsUnconfirmedAfterCaughtUp(t *testing.T) {
	history := &historyStub{stream: &historyStreamStub{frames: []*historypb.FollowSessionResponse{{
		ThroughCursor: 18, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}},
	}}}}
	got, err := ObserveInput(context.Background(), &dispatcherStub{dispatch: admittedDispatch(18)}, history,
		ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID})
	if err != nil {
		t.Fatalf("ObserveInput() error = %v", err)
	}
	if got.Observation.Kind != ObservationNotObserved || got.Observation.ThroughCursor != "18" {
		t.Fatalf("observation/history = %#v", got.Observation)
	}
}

func TestObserveInputStopsWatchingWhenDispatcherCancelsQueuedCommand(t *testing.T) {
	cancelled := admittedDispatch(0)
	cancelled.Admission = nil
	cancelled.State = dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER
	dispatcher := &dispatcherStub{
		dispatch: &dispatcherpb.Dispatch{
			CommandId: testCommandID, SessionId: testSessionID,
			Operation: dispatcherpb.Operation_OPERATION_SUBMIT_INPUT,
			State:     dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
		},
		watch: &dispatchStreamStub{frames: []*dispatcherpb.WatchDispatchesResponse{
			{Frame: &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}}},
			{Frame: &dispatcherpb.WatchDispatchesResponse_Dispatch{Dispatch: cancelled}},
		}},
	}
	history := &historyStub{}
	got, err := ObserveInput(context.Background(), dispatcher, history,
		ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID, WaitSeconds: 1})
	if err != nil {
		t.Fatalf("ObserveInput() error = %v", err)
	}
	if got.Observation.Kind != ObservationNotObserved || history.calls != 0 {
		t.Fatalf("observation = %#v, History calls = %d; canceled input must not be inferred as a turn", got.Observation, history.calls)
	}
	var receipt dispatcherpb.Dispatch
	if err := protojson.Unmarshal(got.Dispatch.Value, &receipt); err != nil {
		t.Fatalf("unmarshal Dispatcher receipt: %v", err)
	}
	if receipt.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER {
		t.Fatalf("Dispatcher state = %s, want cancelled", receipt.GetState())
	}
	if dispatcher.watchReq.GetFilter().GetSessionId() != testSessionID || len(dispatcher.watchReq.GetFilter().GetCommandIds()) != 1 || dispatcher.watchReq.GetFilter().GetCommandIds()[0] != testCommandID {
		t.Fatalf("watch filter = %#v", dispatcher.watchReq.GetFilter())
	}
}

func TestObserveInputDoesNotInferHistoryCommandOutcomeFromDispatcherState(t *testing.T) {
	dispatch := &dispatcherpb.Dispatch{
		CommandId: testCommandID, SessionId: testSessionID,
		Operation: dispatcherpb.Operation_OPERATION_SUBMIT_INPUT,
		State:     dispatcherpb.DeliveryState_DELIVERY_STATE_NO_OP,
		Reason:    new("dispatcher did not enqueue input"),
	}
	history := &historyStub{}
	got, err := ObserveInput(context.Background(), &dispatcherStub{dispatch: dispatch}, history,
		ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID})
	if err != nil {
		t.Fatalf("ObserveInput() error = %v", err)
	}
	if got.Dispatch.Kind != "observed" || got.Observation.Kind != ObservationNotObserved || history.calls != 0 {
		t.Fatalf("dispatch/observation/History calls = %#v / %#v / %d", got.Dispatch, got.Observation, history.calls)
	}
}

func admittedDispatch(cursor uint64) *dispatcherpb.Dispatch {
	return &dispatcherpb.Dispatch{
		CommandId: testCommandID, SessionId: testSessionID,
		Operation: dispatcherpb.Operation_OPERATION_SUBMIT_INPUT,
		State:     dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED,
		Admission: &eventlogpb.EventEntry{Cursor: cursor},
	}
}

func entriesFrame(through uint64, entries ...*eventlogpb.EventEntry) *historypb.FollowSessionResponse {
	return &historypb.FollowSessionResponse{
		ThroughCursor: through,
		Frame:         &historypb.FollowSessionResponse_Entries{Entries: &historypb.Entries{Entries: entries}},
	}
}

func entry(cursor uint64, event *eventpb.Event) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Event: event}
}
