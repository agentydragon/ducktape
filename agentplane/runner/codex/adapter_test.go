package codex

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/runner/codex/protocol"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/testing/adaptertest"
)

func TestDecodeCodexItemRetainsGeneratedVariants(t *testing.T) {
	for _, test := range []struct {
		typeName string
		want     reflect.Type
	}{
		{"agentMessage", reflect.TypeOf(protocol.AgentMessageThreadItem{})},
		{"reasoning", reflect.TypeOf(protocol.ReasoningThreadItem{})},
		{"commandExecution", reflect.TypeOf(protocol.CommandExecutionThreadItem{})},
		{"subAgentActivity", reflect.TypeOf(protocol.SubAgentActivityThreadItem{})},
	} {
		t.Run(test.typeName, func(t *testing.T) {
			item, err := decodeCodexItem(json.RawMessage(`{"type":"` + test.typeName + `","id":"item-1"}`))
			if err != nil {
				t.Fatal(err)
			}
			if got := reflect.TypeOf(item); got != test.want {
				t.Fatalf("decoded type = %v, want %v", got, test.want)
			}
		})
	}
}

func TestDecodeCodexItemPreservesUnknownRawPayload(t *testing.T) {
	raw := json.RawMessage(`{"type":"futureTool","id":"future-call","large":9007199254740993,"explicitNull":null}`)
	item, err := decodeCodexItem(raw)
	if err != nil {
		t.Fatal(err)
	}
	unknown, ok := item.(codexUnknownItem)
	if !ok {
		t.Fatalf("decoded type = %T, want codexUnknownItem", item)
	}
	if unknown.typeName != "futureTool" || unknown.id != "future-call" {
		t.Fatalf("unknown item identity = (%q, %q)", unknown.typeName, unknown.id)
	}
	if !bytes.Equal(unknown.raw, raw) {
		t.Fatalf("raw item changed: got %s, want %s", unknown.raw, raw)
	}
	fields, err := unknown.fields()
	if err != nil {
		t.Fatal(err)
	}
	if string(fields["large"]) != "9007199254740993" {
		t.Fatalf("large integer changed: %s", fields["large"])
	}
	if string(fields["explicitNull"]) != "null" {
		t.Fatalf("explicit null changed: %s", fields["explicitNull"])
	}
}

func newAdapterTestHost() *adaptertest.Host { return adaptertest.NewHost() }

type adapterHandshakeHost struct {
	*adaptertest.Host
	requests []json.RawMessage
}

func (h *adapterHandshakeHost) RequestNativeHandshake(_ context.Context, frame json.RawMessage, match func(json.RawMessage) bool) (harness.NativeReceipt, error) {
	h.requests = append(h.requests, append(json.RawMessage(nil), frame...))
	var request struct {
		ID     string `json:"id"`
		Method string `json:"method"`
	}
	if err := json.Unmarshal(frame, &request); err != nil {
		return harness.NativeReceipt{}, err
	}
	var response any
	switch request.Method {
	case "thread/start":
		response = map[string]any{"id": request.ID, "result": map[string]any{"thread": map[string]string{"id": "thread-created"}}}
	default:
		return harness.NativeReceipt{}, errors.New("unexpected Codex request " + request.Method)
	}
	encoded, err := json.Marshal(response)
	if err != nil {
		return harness.NativeReceipt{}, err
	}
	raw := json.RawMessage(encoded)
	if !match(raw) {
		return harness.NativeReceipt{}, errors.New("test Codex response did not match request")
	}
	return harness.NativeReceipt{Frame: raw, Sequence: uint64(len(h.requests))}, nil
}

func codexTurnStartResponse(t *testing.T, requestID, turnID string) json.RawMessage {
	t.Helper()
	frame, err := json.Marshal(map[string]any{
		"id": requestID, "result": map[string]any{"turn": map[string]string{"id": turnID}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return frame
}

func TestCodexLateTurnStartResponseConfirmsPendingCommand(t *testing.T) {
	ctx := context.Background()
	host := newAdapterTestHost()
	adapter := New(host)
	if err := adapter.Submit(ctx, "input-1", "hello"); err != nil {
		t.Fatalf("Submit error = %v", err)
	}
	if len(host.NativeRequests) != 1 || host.NativeRequests[0].Kind != harness.CodexTurnStart || !slices.Equal(host.NativeRequests[0].CommandIDs, []string{"input-1"}) {
		t.Fatalf("Codex requests = %+v", host.NativeRequests)
	}
	requestID := host.NativeRequests[0].ID
	if requestID == "" {
		t.Fatal("Codex did not record a native request ID")
	}
	if len(host.NativeWrites) != 1 || len(host.Failures) != 0 || len(host.Confirmations) != 0 {
		t.Fatalf("dispatch did not return with one pending native request: writes=%d failures=%v confirmations=%v", len(host.NativeWrites), host.Failures, host.Confirmations)
	}
	if err := adapter.OnFrame(ctx, codexTurnStartResponse(t, requestID, "turn-late"), 17); err != nil {
		t.Fatal(err)
	}
	if len(host.Confirmations) != 1 || host.Confirmations[0].Value.OriginCommandIDs[0] != "input-1" {
		t.Fatalf("late response confirmations = %+v", host.Confirmations)
	}
	if len(host.Failures) != 0 {
		t.Fatalf("late success failed command: %v", host.Failures)
	}
	if err := adapter.OnFrame(ctx, codexTurnStartResponse(t, requestID, "turn-late"), 18); err != nil {
		t.Fatal(err)
	}
	if len(host.Confirmations) != 1 {
		t.Fatalf("duplicate response confirmed command again: %+v", host.Confirmations)
	}
}

func TestCodexRecoveryReplaysJournaledLateTurnStartResponse(t *testing.T) {
	ctx := context.Background()
	host := newAdapterTestHost()
	host.SessionState.ActiveTurnID = ""
	requestID := "agentplane-prior-request"
	command := &commandpb.Command{CommandId: "input-1", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "hello"}}}
	host.JournalState.Requests = []harness.NativeRequest{{
		ID: requestID, Kind: harness.CodexTurnStart,
		Commands: []harness.CommandRecord{
			{Command: command, DispatchPlanned: true},
			{Command: &commandpb.Command{CommandId: "model-1", Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: "recovered-model"}}}, DispatchPlanned: true},
			{Command: &commandpb.Command{CommandId: "effort-1", Operation: &commandpb.Command_ChangeReasoningEffort{ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: "high"}}}, DispatchPlanned: true},
		},
	}}
	host.JournalState.Events = []*eventlogpb.EventEntry{{
		Cursor: 1, Origin: &eventlogpb.EventOrigin{SourceId: "test-source", Sequence: 43},
		Event: &eventpb.Event{Observation: &eventpb.Event_Native{Native: &eventpb.Native{
			Direction: eventpb.Direction_DIRECTION_FROM_HARNESS,
			Line:      string(codexTurnStartResponse(t, requestID, "turn-late")),
		}}},
	}}
	adapter := New(host)
	if err := adapter.(harness.RecoveryAdapter).Recover(ctx); err != nil {
		t.Fatal(err)
	}
	if len(host.Confirmations) != 1 || host.Confirmations[0].Sources[0] != 43 {
		t.Fatalf("recovered confirmation = %+v", host.Confirmations)
	}
	if host.SessionState.Model != "recovered-model" || host.SessionState.ReasoningEffort != "high" || len(host.NativeWrites) != 0 {
		t.Fatalf("recovery lost linked command values or resent native work: session=%+v writes=%d", host.SessionState, len(host.NativeWrites))
	}
}

func TestCodexHandshakeAndUnsupportedServerRequest(t *testing.T) {
	ctx := context.Background()
	base := newAdapterTestHost()
	base.SessionState.Resuming = false
	base.SessionState.Environment = map[string]string{"AGENTPLANE_DESTINATION": "test-destination"}
	host := &adapterHandshakeHost{Host: base}
	adapter := New(host)
	threadID, err := adapter.Handshake(ctx)
	if err != nil {
		t.Fatal(err)
	}
	// The Runner initializes the shared app-server once; a Session's handshake only starts its thread.
	if threadID != "thread-created" || len(host.requests) != 1 {
		t.Fatalf("unexpected Codex handshake: thread=%q requests=%d", threadID, len(host.requests))
	}
	var start map[string]any
	if err := json.Unmarshal(host.requests[0], &start); err != nil {
		t.Fatal(err)
	}
	params := start["params"].(map[string]any)
	if params["cwd"] != base.SessionState.Cwd || params["model"] != base.SessionState.Model || params["approvalPolicy"] != "never" || params["sandbox"] != "danger-full-access" || params["ephemeral"] != false {
		t.Fatalf("Codex thread/start lost launch policy: %s", host.requests[0])
	}
	set := params["config"].(map[string]any)["shell_environment_policy.set"].(map[string]any)
	if set["AGENTPLANE_DESTINATION"] != "test-destination" {
		t.Fatalf("Codex thread/start lost the Session's own tool environment: %s", host.requests[0])
	}
	if err := adapter.OnFrame(ctx, json.RawMessage(`{"id":"server-request","method":"item/tool/requestUserInput","params":{}}`), 3); err != nil {
		t.Fatal(err)
	}
	var answer map[string]any
	if err := json.Unmarshal(base.NativeWrites[len(base.NativeWrites)-1], &answer); err != nil {
		t.Fatal(err)
	}
	if answer["id"] != "server-request" || answer["error"].(map[string]any)["code"] != float64(-32601) {
		t.Fatalf("Codex unsupported request response: %s", base.NativeWrites[len(base.NativeWrites)-1])
	}
}

func TestCodexIgnoresChildFramesAndKeepsUnknownItemFields(t *testing.T) {
	ctx := context.Background()
	host := newAdapterTestHost()
	adapter := New(host)
	childFrame := json.RawMessage(`{"method":"item/completed","params":{"threadId":"child-thread","turnId":"child-turn","item":{"type":"agentMessage","id":"child-item","text":"CHILD_ONLY"}}}`)
	childBefore := append(json.RawMessage(nil), childFrame...)
	if err := adapter.OnFrame(ctx, childFrame, 1); err != nil {
		t.Fatal(err)
	}
	if len(host.Events) != 0 || !bytes.Equal(childFrame, childBefore) {
		t.Fatalf("Codex child frame changed root state or raw evidence: events=%d raw=%s", len(host.Events), childFrame)
	}
	unknown := json.RawMessage(`{"method":"item/started","params":{"threadId":"thread-root","turnId":"turn-root","item":{"type":"futureTool","id":"future-call","foo":{"x":9007199254740993},"status":"completed","exitCode":0}}}`)
	unknownBefore := append(json.RawMessage(nil), unknown...)
	if err := adapter.OnFrame(ctx, unknown, 2); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(unknown, unknownBefore) {
		t.Fatalf("unknown frame was rewritten: %s", unknown)
	}
	if len(host.Events) != 2 || host.Events[0].GetItemStarted() == nil || host.Events[1].GetToolArguments() == nil {
		t.Fatalf("unknown tool was not projected as a tool call: %#v", host.Events)
	}
	started := host.Events[0].GetItemStarted()
	if started.GetToolName() != "futureTool" || started.GetItemId() != "future-call" {
		t.Fatalf("unknown item identity was lost: %#v", started)
	}
	var arguments map[string]json.RawMessage
	if err := json.Unmarshal([]byte(host.Events[1].GetToolArguments().ArgumentsJson), &arguments); err != nil {
		t.Fatal(err)
	}
	if len(arguments) != 1 || arguments["foo"] == nil {
		t.Fatalf("unknown item's outcome fields leaked into arguments: %s", host.Events[1].GetToolArguments().ArgumentsJson)
	}
	if string(arguments["foo"]) != `{"x":9007199254740993}` {
		t.Fatalf("unknown item's payload lost precision: %s", arguments["foo"])
	}
}

func TestCodexStartReplyBeforeNotificationSettlesInputAndUnblocksDispatch(t *testing.T) {
	host := newAdapterTestHost()
	host.SessionState.ActiveTurnID = ""
	// This is the actual journal response before the native turn/started notification arrives.
	host.JournalState.TurnEventsError = fmt.Errorf("%w %q", harness.ErrUnknownRecoveryTurn, "new-turn")
	adapter := New(host)
	if err := adapter.Submit(context.Background(), "input", "hello"); err != nil {
		t.Fatal(err)
	}
	var request struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(host.NativeWrites[0], &request); err != nil {
		t.Fatal(err)
	}
	response := json.RawMessage(fmt.Sprintf(`{"id":%q,"result":{"turn":{"id":"new-turn"}}}`, request.ID))
	if err := adapter.OnFrame(context.Background(), response, 1); err != nil {
		t.Fatal(err)
	}
	if adapter.BlocksCommandDispatch() || len(host.Confirmations) != 1 || host.SessionState.ActiveTurnID != "new-turn" {
		t.Fatalf("early reply left input pending: blocked=%v confirmations=%v active=%q", adapter.BlocksCommandDispatch(), host.Confirmations, host.SessionState.ActiveTurnID)
	}
	if err := adapter.OnFrame(context.Background(), json.RawMessage(`{"method":"turn/started","params":{"threadId":"thread-root","turn":{"id":"new-turn"}}}`), 2); err != nil {
		t.Fatal(err)
	}
	starts := 0
	for _, event := range host.Events {
		if event.GetTurnStarted() != nil {
			starts++
		}
	}
	if starts != 1 {
		t.Fatalf("late notification duplicated TurnStarted: %d", starts)
	}
}
