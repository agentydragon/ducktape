package sessionclient

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	testSessionID = "58cae82c-453d-4de7-ae5b-17a8f7596077"
	testCommandID = "c40dd833-326f-4be1-b42a-4bb74c76c49a"
)

type dispatcherStub struct {
	dispatcherpb.SessionDispatcherClient
	dispatch      *dispatcherpb.Dispatch
	watch         dispatcherpb.SessionDispatcher_WatchDispatchesClient
	watchReq      *dispatcherpb.WatchDispatchesRequest
	watchRequests []*dispatcherpb.WatchDispatchesRequest
	streams       []dispatcherpb.SessionDispatcher_WatchDispatchesClient
	watchErrors   []error
	getError      error
}

func (s dispatcherStub) GetDispatch(_ context.Context, _ *dispatcherpb.GetDispatchRequest, _ ...grpc.CallOption) (*dispatcherpb.GetDispatchResponse, error) {
	if s.getError != nil {
		return nil, s.getError
	}
	return &dispatcherpb.GetDispatchResponse{Dispatch: s.dispatch}, nil
}

func (s *dispatcherStub) WatchDispatches(_ context.Context, request *dispatcherpb.WatchDispatchesRequest, _ ...grpc.CallOption) (dispatcherpb.SessionDispatcher_WatchDispatchesClient, error) {
	s.watchReq = request
	s.watchRequests = append(s.watchRequests, request)
	index := len(s.watchRequests) - 1
	if index < len(s.watchErrors) && s.watchErrors[index] != nil {
		return nil, s.watchErrors[index]
	}
	if index < len(s.streams) {
		return s.streams[index], nil
	}
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
	err    error
}

func (s *dispatchStreamStub) Recv() (*dispatcherpb.WatchDispatchesResponse, error) {
	if s.index >= len(s.frames) {
		if s.err != nil {
			return nil, s.err
		}
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
		entriesFrame(
			21,
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

func TestObserveInputReturnsWakeDenialFromQueuedWatchUpdate(t *testing.T) {
	initial := &dispatcherpb.Dispatch{
		CommandId: testCommandID, SessionId: testSessionID,
		Operation:      commandpb.Operation_OPERATION_SUBMIT_INPUT,
		State:          dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
		WakeAssessment: &environmentlifecyclepb.WakeAssessment{State: environmentlifecyclepb.WakeState_WAKE_STATE_NOT_EVALUATED},
	}
	updated := &dispatcherpb.Dispatch{
		CommandId: testCommandID, SessionId: testSessionID,
		Operation: commandpb.Operation_OPERATION_SUBMIT_INPUT,
		State:     dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
		DeliveryBlocker: &environmentlifecyclepb.DeliveryBlocker{
			Kind: environmentlifecyclepb.DeliveryBlockerKind_DELIVERY_BLOCKER_KIND_ENVIRONMENT_SUSPENDED,
		},
		WakeAssessment: &environmentlifecyclepb.WakeAssessment{
			State:      environmentlifecyclepb.WakeState_WAKE_STATE_DENIED,
			Reason:     environmentlifecyclepb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED,
			ObservedAt: timestamppb.Now(), TargetGeneration: proto.Int64(4),
		},
	}
	dispatcher := &dispatcherStub{
		dispatch: initial,
		watch: &dispatchStreamStub{frames: []*dispatcherpb.WatchDispatchesResponse{{
			Frame: &dispatcherpb.WatchDispatchesResponse_Dispatch{Dispatch: updated},
		}}},
	}
	history := &historyStub{}
	got, err := ObserveInput(context.Background(), dispatcher, history,
		ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID, WaitSeconds: 30})
	if err != nil {
		t.Fatalf("ObserveInput() error = %v", err)
	}
	if history.calls != 0 || got.Dispatch.Kind != "observed" {
		t.Fatalf("wake denial was not returned before History: dispatch=%#v History calls=%d", got.Dispatch, history.calls)
	}
	observed := got.Dispatch.Value
	if observed.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER ||
		observed.GetDeliveryBlocker().GetKind() != environmentlifecyclepb.DeliveryBlockerKind_DELIVERY_BLOCKER_KIND_ENVIRONMENT_SUSPENDED ||
		observed.GetWakeAssessment().GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_DENIED ||
		observed.GetWakeAssessment().GetReason() != environmentlifecyclepb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED ||
		observed.GetWakeAssessment().GetTargetGeneration() != 4 {
		t.Fatalf("observed queued wake denial: %+v", observed)
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
			Operation: commandpb.Operation_OPERATION_SUBMIT_INPUT,
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
	receipt := got.Dispatch.Value
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
		Operation: commandpb.Operation_OPERATION_SUBMIT_INPUT,
		State:     dispatcherpb.DeliveryState(99),
		Reason:    new("dispatcher returned an unrecognized state"),
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
	if got.Dispatch.Value.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_UNKNOWN {
		t.Fatalf("future delivery state was not normalized: %s", got.Dispatch.Value.GetState())
	}
}

func TestObserveInputStillReadsHistoryWhenTerminalDispatchHasAdmission(t *testing.T) {
	dispatch := admittedDispatch(18)
	dispatch.State = dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED
	dispatch.Reason = new("Dispatcher delivery attempt failed after admission")
	history := &historyStub{stream: &historyStreamStub{frames: []*historypb.FollowSessionResponse{
		entriesFrame(20, entry(20, &eventpb.Event{Observation: &eventpb.Event_CommandFailed{
			CommandFailed: &eventpb.CommandFailed{CommandId: testCommandID, Reason: "runner rejected input"},
		}})),
	}}}
	got, err := ObserveInput(context.Background(), &dispatcherStub{dispatch: dispatch}, history,
		ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID})
	if err != nil {
		t.Fatalf("ObserveInput() error = %v", err)
	}
	if history.calls != 1 || got.Observation.Kind != ObservationCommandFailed || got.Observation.Reason == nil || *got.Observation.Reason != "runner rejected input" {
		t.Fatalf("History calls = %d, observation = %#v", history.calls, got.Observation)
	}
}

func admittedDispatch(cursor uint64) *dispatcherpb.Dispatch {
	return &dispatcherpb.Dispatch{
		CommandId: testCommandID, SessionId: testSessionID,
		Operation: commandpb.Operation_OPERATION_SUBMIT_INPUT,
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

func queuedDispatch() *dispatcherpb.Dispatch {
	return &dispatcherpb.Dispatch{
		CommandId: testCommandID, SessionId: testSessionID,
		Operation: commandpb.Operation_OPERATION_SUBMIT_INPUT,
		State:     dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER,
	}
}

func cancelledDispatchFrame(token string) *dispatcherpb.WatchDispatchesResponse {
	dispatch := queuedDispatch()
	dispatch.State = dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER
	return &dispatcherpb.WatchDispatchesResponse{
		ResumeToken: token, Frame: &dispatcherpb.WatchDispatchesResponse_Dispatch{Dispatch: dispatch},
	}
}

func TestObserveInputResumesAfterDisconnectAndReadsDeletedReceipt(t *testing.T) {
	client := &dispatcherStub{dispatch: queuedDispatch(), watch: &dispatchStreamStub{
		frames: []*dispatcherpb.WatchDispatchesResponse{{ResumeToken: "test-dispatch-position", Frame: &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}}}},
	}}
	request := ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID, WaitSeconds: 1}
	first, err := ObserveInput(context.Background(), client, &historyStub{}, request)
	if err != nil || first.Dispatch.Kind != SourcePartial {
		t.Fatalf("disconnected watch = %+v, %v", first, err)
	}
	position, err := decodeResumeToken(first.Observation.ResumeToken, testSessionID, testCommandID)
	if err != nil || position.DispatchToken != "test-dispatch-position" {
		t.Fatalf("lost checkpoint: %+v, %v", position, err)
	}
	// The live row disappeared during the disconnect; GetDispatch cannot retrieve a tombstone.
	client = &dispatcherStub{getError: status.Error(codes.NotFound, "test deleted dispatch"), watch: &dispatchStreamStub{
		frames: []*dispatcherpb.WatchDispatchesResponse{cancelledDispatchFrame("test-deletion-position")},
	}}
	request.ResumeToken = first.Observation.ResumeToken
	got, err := ObserveInput(context.Background(), client, &historyStub{}, request)
	if err != nil {
		t.Fatal(err)
	}
	if client.watchReq.GetResumeToken() != "test-dispatch-position" || got.Dispatch.Value.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER || got.Dispatch.Value.GetSandbox() != "" || got.Dispatch.Value.GetSandboxUid() != "" {
		t.Fatalf("deletion replay: request=%+v result=%+v", client.watchReq, got)
	}
	position, err = decodeResumeToken(got.Observation.ResumeToken, testSessionID, testCommandID)
	if err != nil || position.DispatchToken != "test-deletion-position" {
		t.Fatalf("terminal checkpoint = %+v, %v", position, err)
	}
}

func TestObserveInputReportsExpiredCoverageAndResyncs(t *testing.T) {
	for _, atOpen := range []bool{false, true} {
		for _, deleted := range []bool{false, true} {
			t.Run(fmt.Sprintf("open=%v/deleted=%v", atOpen, deleted), func(t *testing.T) {
				client := &dispatcherStub{dispatch: queuedDispatch(), streams: []dispatcherpb.SessionDispatcher_WatchDispatchesClient{
					&dispatchStreamStub{err: status.Error(codes.OutOfRange, "test expired deletion coverage")},
					&dispatchStreamStub{frames: []*dispatcherpb.WatchDispatchesResponse{cancelledDispatchFrame("test-fresh-position")}},
				}}
				if atOpen {
					client.watchErrors = []error{status.Error(codes.OutOfRange, "test expired deletion coverage")}
				}
				if deleted {
					client.getError = status.Error(codes.NotFound, "test deleted dispatch")
					client.streams[1] = &dispatchStreamStub{frames: []*dispatcherpb.WatchDispatchesResponse{{ResumeToken: "test-fresh-position", Frame: &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}}}}}
				}
				got, err := ObserveInput(context.Background(), client, &historyStub{}, ObserveInputRequest{
					SessionID: testSessionID, CommandID: testCommandID, WaitSeconds: 1,
					ResumeToken: encodeResumeToken(resumeToken{Version: 1, SessionID: testSessionID, CommandID: testCommandID, DispatchToken: "test-expired-position"}),
				})
				if err != nil {
					t.Fatal(err)
				}
				if len(client.watchRequests) != 2 || client.watchRequests[0].GetResumeToken() != "test-expired-position" || client.watchRequests[1].GetResumeToken() != "" {
					t.Fatalf("snapshot requests: %+v", client.watchRequests)
				}
				if got.Dispatch.Kind != SourcePartial || got.Dispatch.Error == nil || got.Dispatch.Error.Code != codes.OutOfRange.String() {
					t.Fatalf("coverage loss hidden by resync: %+v", got.Dispatch)
				}
				if !deleted && got.Dispatch.Value.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER {
					t.Fatalf("fresh receipt lost: %+v", got.Dispatch)
				}
				position, err := decodeResumeToken(got.Observation.ResumeToken, testSessionID, testCommandID)
				if err != nil || position.DispatchToken != "test-fresh-position" {
					t.Fatalf("reset position: %+v, %v", position, err)
				}
			})
		}
	}
}

func TestObserveInputRestoresAlreadyCoveredDeletedReceiptFromSnapshot(t *testing.T) {
	client := &dispatcherStub{getError: status.Error(codes.NotFound, "test deleted dispatch"), streams: []dispatcherpb.SessionDispatcher_WatchDispatchesClient{
		&dispatchStreamStub{frames: []*dispatcherpb.WatchDispatchesResponse{{ResumeToken: "test-covered-position", Frame: &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}}}}},
		&dispatchStreamStub{frames: []*dispatcherpb.WatchDispatchesResponse{cancelledDispatchFrame("test-snapshot-position")}},
	}}
	got, err := ObserveInput(context.Background(), client, &historyStub{}, ObserveInputRequest{
		SessionID: testSessionID, CommandID: testCommandID,
		ResumeToken: encodeResumeToken(resumeToken{Version: 1, SessionID: testSessionID, CommandID: testCommandID, DispatchToken: "test-prior-position"}),
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(client.watchRequests) != 2 || client.watchRequests[1].GetResumeToken() != "" || got.Dispatch.Value.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_CANCELLED_IN_DISPATCHER {
		t.Fatalf("restore: requests=%+v result=%+v", client.watchRequests, got)
	}
}

func TestObserveInputKeepsDispatchCheckpointWhileRebuildingHistory(t *testing.T) {
	client := &dispatcherStub{dispatch: queuedDispatch(), watch: &dispatchStreamStub{
		frames: []*dispatcherpb.WatchDispatchesResponse{{ResumeToken: "test-admitted-position", Frame: &dispatcherpb.WatchDispatchesResponse_Dispatch{Dispatch: admittedDispatch(18)}}},
	}}
	history := &historyStub{stream: &historyStreamStub{frames: []*historypb.FollowSessionResponse{
		entriesFrame(20, entry(20, &eventpb.Event{Observation: &eventpb.Event_CommandFailed{CommandFailed: &eventpb.CommandFailed{CommandId: testCommandID, Reason: "test failure"}}})),
	}}}
	got, err := ObserveInput(context.Background(), client, history, ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID, WaitSeconds: 1})
	if err != nil {
		t.Fatal(err)
	}
	position, err := decodeResumeToken(got.Observation.ResumeToken, testSessionID, testCommandID)
	if err != nil || position.DispatchToken != "test-admitted-position" || position.StartCursor != 17 || position.Through != 20 {
		t.Fatalf("combined cursor = %+v, %v", position, err)
	}
	// A token supplies a Dispatch position, never a shortcut around rebuilding command/turn correlation.
	client.dispatch = admittedDispatch(18)
	history.stream = &historyStreamStub{frames: []*historypb.FollowSessionResponse{{ThroughCursor: 20, Frame: &historypb.FollowSessionResponse_CaughtUp{CaughtUp: &emptypb.Empty{}}}}}
	_, err = ObserveInput(context.Background(), client, history, ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID, ResumeToken: got.Observation.ResumeToken})
	if err != nil || history.request.GetAfterCursor() != 17 {
		t.Fatalf("History replay = %+v, %v", history.request, err)
	}
}

func TestObserveInputResyncsWhenAnActiveFreshWatchLosesCoverage(t *testing.T) {
	client := &dispatcherStub{dispatch: queuedDispatch(), streams: []dispatcherpb.SessionDispatcher_WatchDispatchesClient{
		&dispatchStreamStub{
			frames: []*dispatcherpb.WatchDispatchesResponse{{ResumeToken: "test-active-position", Frame: &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}}}},
			err:    status.Error(codes.OutOfRange, "test active coverage expired"),
		},
		&dispatchStreamStub{frames: []*dispatcherpb.WatchDispatchesResponse{cancelledDispatchFrame("test-resynced-position")}},
	}}
	got, err := ObserveInput(context.Background(), client, &historyStub{}, ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID, WaitSeconds: 1})
	if err != nil || len(client.watchRequests) != 2 || client.watchRequests[1].GetResumeToken() != "" || got.Dispatch.Error == nil || got.Dispatch.Error.Code != codes.OutOfRange.String() {
		t.Fatalf("active stream coverage loss = %+v, %v", got, err)
	}
}

func TestObserveInputDoesNotKeepStaleReceiptAfterExpiredCoverageAndEmptySnapshot(t *testing.T) {
	client := &dispatcherStub{dispatch: queuedDispatch(), streams: []dispatcherpb.SessionDispatcher_WatchDispatchesClient{
		&dispatchStreamStub{err: status.Error(codes.OutOfRange, "test expired coverage")},
		&dispatchStreamStub{frames: []*dispatcherpb.WatchDispatchesResponse{{ResumeToken: "test-empty-snapshot-position", Frame: &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}}}}},
	}}
	got, err := ObserveInput(context.Background(), client, &historyStub{}, ObserveInputRequest{
		SessionID: testSessionID, CommandID: testCommandID, WaitSeconds: 1,
		ResumeToken: encodeResumeToken(resumeToken{Version: 1, SessionID: testSessionID, CommandID: testCommandID, DispatchToken: "test-expired-position"}),
	})
	if err != nil || got.Dispatch.Value != nil || got.Dispatch.Error == nil || got.Dispatch.Error.Code != codes.OutOfRange.String() {
		t.Fatalf("stale receipt survived resync: %+v, %v", got, err)
	}
}

func TestObserveInputReadOncePersistsDispatcherCheckpoint(t *testing.T) {
	client := &dispatcherStub{dispatch: queuedDispatch(), watch: &dispatchStreamStub{frames: []*dispatcherpb.WatchDispatchesResponse{{
		ResumeToken: "test-read-once-position", Frame: &dispatcherpb.WatchDispatchesResponse_CaughtUp{CaughtUp: &dispatcherpb.CaughtUp{}},
	}}}}
	got, err := ObserveInput(context.Background(), client, &historyStub{}, ObserveInputRequest{SessionID: testSessionID, CommandID: testCommandID})
	if err != nil {
		t.Fatal(err)
	}
	position, err := decodeResumeToken(got.Observation.ResumeToken, testSessionID, testCommandID)
	if err != nil || position.DispatchToken != "test-read-once-position" || got.Dispatch.Kind != SourceObserved {
		t.Fatalf("read-once checkpoint = %+v result=%+v, %v", position, got, err)
	}
}

func TestNewSourceErrorReportsTheCodeWithoutBackendDetail(t *testing.T) {
	const detail = "test-only backend secret"
	for code := codes.Canceled; code <= codes.Unauthenticated; code++ {
		want := code
		if code == codes.Unknown {
			want = codes.Unavailable
		}
		got := NewSourceError(status.Error(code, detail))
		if got.Code != want.String() || got.Message == "" || strings.Contains(got.Message, detail) {
			t.Errorf("%v became %+v", code, got)
		}
	}
}
