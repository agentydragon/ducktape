package sessionclient

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"strconv"
	"strings"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const (
	secondSessionID = "ca5db09e-2b26-4518-a587-adab5a2c3f60"
	firstInputID    = "c40dd833-326f-4be1-b42a-4bb74c76c49a"
	secondInputID   = "a5f7615d-66cc-447a-84ec-5d7a63dd07e7"
)

var resultSessionID = uuid.MustParse(testSessionID)

type resultDispatcherStub struct {
	dispatcherpb.SessionDispatcherClient
	opening    *dispatcherpb.GetSessionOpeningResponse
	openingErr error
	calls      int
}

func (s *resultDispatcherStub) GetSessionOpening(_ context.Context, _ *dispatcherpb.GetSessionOpeningRequest, _ ...grpc.CallOption) (*dispatcherpb.GetSessionOpeningResponse, error) {
	s.calls++
	return s.opening, s.openingErr
}

type resultHistoryStub struct {
	historypb.HistoryServiceClient
	summary    *historypb.SessionSummary
	summaryErr error
	read       func(*historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error)
	readCalls  []*historypb.ReadSessionEventsRequest
}

func (s *resultHistoryStub) GetSessionSummary(_ context.Context, _ *historypb.GetSessionSummaryRequest, _ ...grpc.CallOption) (*historypb.SessionSummary, error) {
	return s.summary, s.summaryErr
}

func (s *resultHistoryStub) ReadSessionEvents(_ context.Context, request *historypb.ReadSessionEventsRequest, _ ...grpc.CallOption) (*historypb.ReadSessionEventsResponse, error) {
	copy := *request
	s.readCalls = append(s.readCalls, &copy)
	if s.read == nil {
		return nil, status.Error(codes.Unavailable, "History unavailable")
	}
	return s.read(request)
}

func TestGetSessionComposesProtoJSONWithoutLosingEnumOrUint64Shapes(t *testing.T) {
	dispatcher := &resultDispatcherStub{opening: &dispatcherpb.GetSessionOpeningResponse{Opening: &dispatcherpb.SessionOpening{
		SessionId: testSessionID, State: dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED,
	}}}
	history := &resultHistoryStub{summary: &historypb.SessionSummary{
		SessionId: testSessionID, CommittedCursor: math.MaxUint64,
		FeedStatus: historypb.FeedStatus_FEED_STATUS_ACTIVE,
	}}

	got, err := GetSession(context.Background(), dispatcher, history, resultSessionID)
	if err != nil {
		t.Fatalf("GetSession() error = %v", err)
	}
	if dispatcher.calls != 1 || got.Opening.Kind != SourceObserved || got.History.Kind != SourceObserved {
		t.Fatalf("snapshot/source calls = %#v, dispatcher calls %d", got, dispatcher.calls)
	}
	if got.SessionID.String() != testSessionID || got.Opening.Source != SourceDispatcher || got.History.Source != SourceHistory {
		t.Fatalf("snapshot identities = %#v", got)
	}
	var opening, summary map[string]json.RawMessage
	if err := json.Unmarshal(got.Opening.Value, &opening); err != nil {
		t.Fatal(err)
	}
	if string(opening["state"]) != `"DELIVERY_STATE_RUNNER_ADMITTED"` {
		t.Fatalf("opening ProtoJSON state = %s", opening["state"])
	}
	if err := json.Unmarshal(got.History.Value, &summary); err != nil {
		t.Fatal(err)
	}
	if string(summary["feedStatus"]) != `"FEED_STATUS_ACTIVE"` || string(summary["committedCursor"]) != `"18446744073709551615"` {
		t.Fatalf("summary ProtoJSON fields = %#v", summary)
	}
}

func TestGetSessionPreservesMissingAndUnavailableSourcesIndependently(t *testing.T) {
	dispatcher := &resultDispatcherStub{openingErr: status.Error(codes.Unavailable, "secret backend detail")}
	history := &resultHistoryStub{summaryErr: status.Error(codes.NotFound, "missing")}
	got, err := GetSession(context.Background(), dispatcher, history, resultSessionID)
	if err != nil {
		t.Fatalf("GetSession() error = %v", err)
	}
	if got.Opening.Kind != SourceUnavailable || got.Opening.Error == nil || got.Opening.Error.Code != codes.Unavailable.String() ||
		strings.Contains(got.Opening.Error.Message, "secret") {
		t.Fatalf("Dispatcher failure was not sanitized: %#v", got.Opening)
	}
	if got.History.Kind != SourceNotObserved || got.History.Error != nil {
		t.Fatalf("missing History was not distinct from an unavailable source: %#v", got.History)
	}
}

func TestReadTurnsPagesAtCompletedTurnsAndPinsTheInitialHistoryTail(t *testing.T) {
	turn1 := []*eventlogpb.EventEntry{
		turnEntry(1, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "provider:turn-1"}}}),
		turnEntry(2, &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{
			TurnId: "provider:turn-1", Text: "first task", OriginCommandIds: []string{firstInputID},
		}}}),
		turnEntry(3, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
			ItemId: "assistant-item", Kind: eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT,
		}}}),
		turnEntry(4, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
			ItemId: "assistant-item", Outcome: &eventpb.ItemCompleted_Text{Text: "first answer"},
		}}}),
		turnEntry(5, completedTurn("provider:turn-1", eventpb.TurnStatus_TURN_STATUS_COMPLETED, "")),
	}
	turn2 := []*eventlogpb.EventEntry{
		turnEntry(6, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "provider:turn-2"}}}),
		turnEntry(7, &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{
			TurnId: "provider:turn-2", Text: "second task", OriginCommandIds: []string{secondInputID},
		}}}),
		turnEntry(9, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
			ItemId: "call/not-a-uuid", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "shell",
		}}}),
		turnEntry(11, completedTurn("provider:turn-2", eventpb.TurnStatus_TURN_STATUS_FAILED, "shell exited 1")),
		turnEntry(12, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "provider:turn-3"}}}),
		turnEntry(13, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "provider:turn-4"}}}),
		turnEntry(14, completedTurn("provider:turn-4", eventpb.TurnStatus_TURN_STATUS_COMPLETED, "new tail")),
	}
	entries := append(append([]*eventlogpb.EventEntry(nil), turn1...), turn2...)
	var readCalls int
	history := &resultHistoryStub{read: func(request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
		readCalls++
		tail := uint64(12)
		if readCalls > 1 {
			tail = 14
		}
		response := &historypb.ReadSessionEventsResponse{LastCursor: tail}
		for _, entry := range entries {
			if entry.GetCursor() > request.GetAfterCursor() {
				response.Entries = append(response.Entries, entry)
			}
		}
		if uint32(len(response.Entries)) > request.GetLimit() {
			response.Entries = response.Entries[:request.GetLimit()]
		}
		if request.GetAfterCursor() == 5 {
			response.Removed = []*historypb.CursorRange{{First: 7, Last: 7}}
			response.Settlements = []*historypb.SettledDeltas{{Ranges: []*historypb.CursorRange{{First: 3, Last: 3}}}}
		}
		return response, nil
	}}

	first, err := ReadTurns(context.Background(), history, ReadTurnsRequest{SessionID: resultSessionID, Count: 1})
	if err != nil {
		t.Fatalf("ReadTurns(first page) error = %v", err)
	}
	if len(first.Turns) != 1 || first.Turns[0].TurnID != "provider:turn-1" || first.ThroughCursor != "5" || first.NextCursor == "" {
		t.Fatalf("first page = %#v", first)
	}
	if got := first.Turns[0]; got.Status == nil || *got.Status != TurnStatusCompleted || len(got.Input) != 1 ||
		got.Input[0].Text != "first task" || len(got.Input[0].CommandIDs) != 1 ||
		got.FinalAssistantMessage == nil || *got.FinalAssistantMessage != "first answer" {
		t.Fatalf("first projected turn = %#v", got)
	}

	second, err := ReadTurns(context.Background(), history, ReadTurnsRequest{SessionID: resultSessionID, Count: 1, PageCursor: first.NextCursor})
	if err != nil {
		t.Fatalf("ReadTurns(second page) error = %v", err)
	}
	if len(second.Turns) != 1 || second.Turns[0].TurnID != "provider:turn-2" || second.ThroughCursor != "11" || second.NextCursor == "" {
		t.Fatalf("second page = %#v", second)
	}
	gotSecond := second.Turns[0]
	if gotSecond.Status == nil || *gotSecond.Status != TurnStatusFailed || gotSecond.Error == nil || *gotSecond.Error != "shell exited 1" ||
		len(gotSecond.ToolCalls) != 1 || gotSecond.ToolCalls[0].ItemID != "call/not-a-uuid" || len(second.Gaps) != 1 ||
		second.Gaps[0] != (HistoryCursorRange{First: 7, Last: 7}) {
		t.Fatalf("second projected turn/coverage = %#v, %#v", gotSecond, second.HistoryCoverage)
	}
	if len(second.Turns[0].Input[0].CommandIDs) != 1 || second.Turns[0].Input[0].CommandIDs[0].String() != secondInputID {
		t.Fatalf("second input command IDs = %#v", second.Turns[0].Input)
	}

	third, err := ReadTurns(context.Background(), history, ReadTurnsRequest{SessionID: resultSessionID, Count: 10, PageCursor: second.NextCursor})
	if err != nil {
		t.Fatalf("ReadTurns(third page) error = %v", err)
	}
	if len(third.Turns) != 1 || third.Turns[0].TurnID != "provider:turn-3" || third.Turns[0].Status != nil || third.ThroughCursor != "12" {
		t.Fatalf("pinned unfinished final turn page = %#v", third)
	}
	if got := strings.Join(cursorRequests(history.readCalls), ","); !strings.Contains(got, "after=11") {
		t.Fatalf("third page did not resume after second completed turn: %s", got)
	}
	encoded, err := json.Marshal(second)
	if err != nil {
		t.Fatal(err)
	}
	var output map[string]any
	if err := json.Unmarshal(encoded, &output); err != nil {
		t.Fatal(err)
	}
	if output["through_cursor"] != "11" {
		t.Fatalf("uint64 cursor JSON = %s", encoded)
	}
	gaps := output["gaps"].([]any)
	if len(gaps) != 1 || gaps[0].(map[string]any)["first"] != "7" || gaps[0].(map[string]any)["last"] != "7" {
		t.Fatalf("cursor range ProtoJSON = %s", encoded)
	}
}

func TestHistoryCursorRangeRoundTripsProtoJSONUint64Boundaries(t *testing.T) {
	coverage := HistoryCoverage{
		Source: SourceHistory, ThroughCursor: strconv.FormatUint(math.MaxUint64, 10),
		Gaps: []HistoryCursorRange{{First: math.MaxUint64 - 1, Last: math.MaxUint64}},
	}
	encoded, err := json.Marshal(coverage)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(encoded), `"first":"18446744073709551614"`) ||
		!strings.Contains(string(encoded), `"last":"18446744073709551615"`) {
		t.Fatalf("cursor range did not use ProtoJSON uint64 strings: %s", encoded)
	}
	var decoded HistoryCoverage
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.Gaps[0] != coverage.Gaps[0] {
		t.Fatalf("cursor range round trip = %#v, want %#v", decoded.Gaps[0], coverage.Gaps[0])
	}
}

func TestReadTurnsPreservesToolOnlyUnfinishedTurn(t *testing.T) {
	entries := []*eventlogpb.EventEntry{
		turnEntry(1, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "turn:tool-only"}}}),
		turnEntry(2, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
			ItemId: "tool-opaque", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "exec",
		}}}),
		turnEntry(3, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
			ItemId: "tool-opaque", Outcome: &eventpb.ItemCompleted_Tool{Tool: &eventpb.ToolResult{Output: "partial output", Succeeded: true}},
		}}}),
	}
	history := &resultHistoryStub{read: func(request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
		var result []*eventlogpb.EventEntry
		for _, entry := range entries {
			if entry.GetCursor() > request.GetAfterCursor() {
				result = append(result, entry)
			}
		}
		return &historypb.ReadSessionEventsResponse{LastCursor: 3, Entries: result}, nil
	}}
	page, err := ReadTurns(context.Background(), history, ReadTurnsRequest{SessionID: resultSessionID, Count: 10})
	if err != nil {
		t.Fatalf("ReadTurns() error = %v", err)
	}
	if page.Error != nil || len(page.Turns) != 1 || page.Turns[0].TurnID != "turn:tool-only" || page.Turns[0].Status != nil ||
		len(page.Turns[0].ToolCalls) != 1 || page.Turns[0].ToolCalls[0].ItemID != "tool-opaque" || page.ThroughCursor != "3" {
		t.Fatalf("tool-only unfinished turn was lost or finalized: %#v", page)
	}
}

func TestReadTurnsKeepsIncompleteTurnBehindResumeCursorOnSourceFailure(t *testing.T) {
	const largeTurnEnd = turnEventPageSize + 3
	entries := make([]*eventlogpb.EventEntry, 0, largeTurnEnd)
	entries = append(
		entries,
		turnEntry(1, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{
			TurnStarted: &eventpb.TurnStarted{TurnId: "provider:first-turn"},
		}}),
		turnEntry(2, completedTurn("provider:first-turn", eventpb.TurnStatus_TURN_STATUS_COMPLETED, "")),
		turnEntry(3, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{
			TurnStarted: &eventpb.TurnStarted{TurnId: "provider:large-turn"},
		}}),
	)
	for cursor := uint64(4); cursor <= largeTurnEnd-1; cursor++ {
		entries = append(entries, turnEntry(cursor, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{
			ItemStarted: &eventpb.ItemStarted{ItemId: fmt.Sprintf("tool-%d", cursor), Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "exec"},
		}}))
	}
	entries = append(entries, turnEntry(largeTurnEnd, completedTurn("provider:large-turn", eventpb.TurnStatus_TURN_STATUS_COMPLETED, "")))
	failOnce := true
	history := &resultHistoryStub{read: func(request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
		if request.GetAfterCursor() == turnEventPageSize && failOnce {
			failOnce = false
			return nil, status.Error(codes.Unavailable, "temporary read failure")
		}
		response := &historypb.ReadSessionEventsResponse{LastCursor: largeTurnEnd}
		for _, entry := range entries {
			if entry.GetCursor() > request.GetAfterCursor() && uint32(len(response.Entries)) < request.GetLimit() {
				response.Entries = append(response.Entries, entry)
			}
		}
		return response, nil
	}}
	first, err := ReadTurns(context.Background(), history, ReadTurnsRequest{SessionID: resultSessionID, Count: 2})
	if err != nil {
		t.Fatalf("ReadTurns(first attempt) error = %v", err)
	}
	if len(first.Turns) != 1 || first.Turns[0].TurnID != "provider:first-turn" || first.Error == nil || first.NextCursor == "" || first.ThroughCursor != "2" {
		t.Fatalf("partial turn did not remain behind a retryable completed-turn boundary: %#v", first)
	}
	retried, err := ReadTurns(context.Background(), history, ReadTurnsRequest{SessionID: resultSessionID, Count: 2, PageCursor: first.NextCursor})
	if err != nil {
		t.Fatalf("ReadTurns(retry) error = %v", err)
	}
	if retried.Error != nil || len(retried.Turns) != 1 || retried.Turns[0].TurnID != "provider:large-turn" ||
		len(retried.Turns[0].ToolCalls) != turnEventPageSize-1 || retried.ThroughCursor != strconv.FormatUint(largeTurnEnd, 10) {
		t.Fatalf("retry lost or duplicated events contributing to the turn: %#v", retried)
	}
}

func TestReadTurnsRejectsCursorForAnotherSessionBeforeReading(t *testing.T) {
	cursor := encodeTurnPageCursor(turnPageCursor{Version: turnCursorVersion, SessionID: secondSessionID, AfterCursor: 4, Through: 8})
	history := &resultHistoryStub{}
	_, err := ReadTurns(context.Background(), history, ReadTurnsRequest{SessionID: resultSessionID, Count: 1, PageCursor: cursor})
	if status.Code(err) != codes.InvalidArgument || len(history.readCalls) != 0 {
		t.Fatalf("ReadTurns() error = %v, reads = %d; want identity rejection before backend access", err, len(history.readCalls))
	}
}

func TestReadTurnsRejectsCursorThatStartsInsideATurn(t *testing.T) {
	cursor := encodeTurnPageCursor(turnPageCursor{Version: turnCursorVersion, SessionID: testSessionID, AfterCursor: 2, Through: 3})
	history := &resultHistoryStub{read: func(request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
		if request.GetAfterCursor() == 0 {
			return &historypb.ReadSessionEventsResponse{LastCursor: 3}, nil
		}
		return &historypb.ReadSessionEventsResponse{LastCursor: 3, Entries: []*eventlogpb.EventEntry{
			turnEntry(2, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
				ItemId: "tool", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "exec",
			}}}),
		}}, nil
	}}
	page, err := ReadTurns(context.Background(), history, ReadTurnsRequest{SessionID: resultSessionID, Count: 1, PageCursor: cursor})
	if err != nil || page.Error == nil || page.Error.Code != codes.DataLoss.String() || len(page.Turns) != 0 {
		t.Fatalf("mid-turn cursor result = %#v, error = %v", page, err)
	}
}

func TestReadTurnsReturnsHistoryErrorsInsteadOfEmptySuccess(t *testing.T) {
	history := &resultHistoryStub{read: func(request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
		return nil, status.Error(codes.Unavailable, "secret details")
	}}
	page, err := ReadTurns(context.Background(), history, ReadTurnsRequest{SessionID: resultSessionID, Count: 1})
	if err != nil || page.Error == nil || page.Error.Code != codes.Unavailable.String() || strings.Contains(page.Error.Message, "secret") || len(page.Turns) != 0 {
		t.Fatalf("source failure became an empty success: %#v, error = %v", page, err)
	}
}

func turnEntry(cursor uint64, event *eventpb.Event) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{Cursor: cursor, Event: event}
}

func completedTurn(turnID string, turnStatus eventpb.TurnStatus, message string) *eventpb.Event {
	return &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{
		TurnId: turnID, Status: turnStatus, Error: message,
	}}}
}

func cursorRequests(requests []*historypb.ReadSessionEventsRequest) []string {
	result := make([]string, 0, len(requests))
	for _, request := range requests {
		result = append(result, "after="+strconv.FormatUint(request.GetAfterCursor(), 10))
	}
	return result
}
