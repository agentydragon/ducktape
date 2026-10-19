package runner

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/runner/go/claude"
	"github.com/agentydragon/ducktape/agentplane/runner/go/codex"
	"github.com/agentydragon/ducktape/agentplane/runner/go/harness"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
)

type adapterTestJournal struct {
	items  map[string]harness.ObservedItem
	events []*eventlogpb.EventEntry
	seen   map[string]bool
	blocks map[string]int
}

func newAdapterTestJournal() *adapterTestJournal {
	return &adapterTestJournal{
		items: make(map[string]harness.ObservedItem), seen: make(map[string]bool), blocks: make(map[string]int),
	}
}

func (j *adapterTestJournal) ObservedItems(context.Context, string) (map[string]harness.ObservedItem, error) {
	return j.items, nil
}

func (j *adapterTestJournal) TurnEvents(context.Context, string) ([]*eventlogpb.EventEntry, error) {
	return j.events, nil
}

func (j *adapterTestJournal) RememberAdapterItem(_ context.Context, adapterID, itemID string) (bool, error) {
	key := adapterID + "/" + itemID
	if j.seen[key] {
		return false, nil
	}
	j.seen[key] = true
	return true, nil
}

func (j *adapterTestJournal) HasAdapterItem(_ context.Context, adapterID, itemID string) (bool, error) {
	return j.seen[adapterID+"/"+itemID], nil
}

func (j *adapterTestJournal) NextAdapterBlock(_ context.Context, adapterID, messageID string) (int, error) {
	key := adapterID + "/" + messageID
	index := j.blocks[key]
	j.blocks[key]++
	return index, nil
}

type adapterTestHost struct {
	session       harness.SessionSnapshot
	journal       *adapterTestJournal
	nativeWrites  []json.RawMessage
	events        []*eventpb.Event
	eventSources  [][]uint64
	confirmations []adapterTestConfirmation
	noops         []string
	failures      []string
}

type adapterTestConfirmation struct {
	value   harness.UserMessageConfirmation
	sources []uint64
}

func newAdapterTestHost() *adapterTestHost {
	return &adapterTestHost{
		session: harness.SessionSnapshot{Directory: "/tmp/session", Cwd: "/workspace", Model: "test-model", ActiveTurnID: "turn-root", NativeSessionID: "thread-root", Resuming: true},
		journal: newAdapterTestJournal(),
	}
}

func (h *adapterTestHost) Session() harness.SessionSnapshot { return h.session }
func (h *adapterTestHost) Journal() harness.AdapterJournal  { return h.journal }
func (h *adapterTestHost) SendNative(_ context.Context, frame json.RawMessage) error {
	h.nativeWrites = append(h.nativeWrites, append(json.RawMessage(nil), frame...))
	return nil
}

func (h *adapterTestHost) RequestNative(context.Context, json.RawMessage, func(json.RawMessage) bool) (harness.NativeReceipt, error) {
	return harness.NativeReceipt{}, errors.New("unexpected native request in adapter test")
}

func (h *adapterTestHost) Emit(_ context.Context, event *eventpb.Event, sources []uint64) error {
	h.events = append(h.events, event)
	h.eventSources = append(h.eventSources, append([]uint64(nil), sources...))
	if turn := event.GetTurnStarted(); turn != nil {
		h.session.ActiveTurnID = turn.TurnId
	}
	if event.GetTurnCompleted() != nil {
		h.session.ActiveTurnID = ""
	}
	return nil
}

func (h *adapterTestHost) ConfirmUserMessage(_ context.Context, value harness.UserMessageConfirmation, sources []uint64) error {
	h.confirmations = append(h.confirmations, adapterTestConfirmation{value: value, sources: append([]uint64(nil), sources...)})
	return nil
}

func (h *adapterTestHost) ModelChanged(_ context.Context, _, model string, _ []uint64) error {
	h.session.Model = model
	return nil
}

func (h *adapterTestHost) ReasoningEffortChanged(context.Context, string, string, []uint64) error {
	return nil
}

func (h *adapterTestHost) FailCommand(_ context.Context, commandID, _ string, _ []uint64) error {
	h.failures = append(h.failures, commandID)
	return nil
}

func (h *adapterTestHost) NoopCommand(_ context.Context, commandID, _ string, _ []uint64) error {
	h.noops = append(h.noops, commandID)
	return nil
}

type adapterHandshakeHost struct {
	*adapterTestHost
	requests []json.RawMessage
}

func (h *adapterHandshakeHost) RequestNative(_ context.Context, frame json.RawMessage, match func(json.RawMessage) bool) (harness.NativeReceipt, error) {
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
	case "initialize":
		response = map[string]any{"id": request.ID, "result": map[string]any{}}
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

func TestClaudeOutboundFramesKeepNativeNullableFields(t *testing.T) {
	ctx := context.Background()
	host := newAdapterTestHost()
	adapter := claude.New(host, harness.LaunchConfig{Binary: "/bin/false"})
	if err := adapter.Submit(ctx, "input-1", "hello"); err != nil {
		t.Fatal(err)
	}
	if err := adapter.OnFrame(ctx, json.RawMessage(`{"type":"control_request","request_id":"permission-1","request":{"subtype":"can_use_tool","tool_name":"Bash","input":{"command":"true"}}}`), 1); err != nil {
		t.Fatal(err)
	}
	var user map[string]any
	if err := json.Unmarshal(host.nativeWrites[0], &user); err != nil {
		t.Fatal(err)
	}
	if parent, ok := user["parent_tool_use_id"]; !ok || parent != nil {
		t.Fatalf("Claude user frame omitted its explicit null parent id: %s", host.nativeWrites[0])
	}
	var answer map[string]any
	if err := json.Unmarshal(host.nativeWrites[1], &answer); err != nil {
		t.Fatal(err)
	}
	response := answer["response"].(map[string]any)
	if value, ok := response["error"]; !ok || value != nil {
		t.Fatalf("Claude success response omitted its null error field: %s", host.nativeWrites[1])
	}
	if err := adapter.Interrupt(ctx, "turn-root"); err != nil {
		t.Fatal(err)
	}
	var control struct {
		RequestID string `json:"request_id"`
	}
	if err := json.Unmarshal(host.nativeWrites[2], &control); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(control.RequestID, "capture-") {
		t.Fatalf("Claude request ID did not match the native driver prefix: %q", control.RequestID)
	}
}

func TestClaudePermissionReplyPreservesInput(t *testing.T) {
	for _, input := range []string{
		`{"large":9007199254740993,"decimal":1.00,"exponent":1e3,"nested":[{"n":9007199254740995,"empty":null}]}`,
		`null`,
	} {
		t.Run(input, func(t *testing.T) {
			host := newAdapterTestHost()
			adapter := claude.New(host, harness.LaunchConfig{Binary: "/bin/false"})
			frame := json.RawMessage(`{"type":"control_request","request_id":"permission-1","request":{"subtype":"can_use_tool","tool_name":"Bash","input":` + input + `}}`)
			if err := adapter.OnFrame(context.Background(), frame, 1); err != nil {
				t.Fatal(err)
			}
			if len(host.nativeWrites) != 1 {
				t.Fatalf("expected one permission reply, got %d", len(host.nativeWrites))
			}
			var reply struct {
				Type     string `json:"type"`
				Response struct {
					RequestID string `json:"request_id"`
					Response  struct {
						Behavior     string          `json:"behavior"`
						UpdatedInput json.RawMessage `json:"updatedInput"`
					} `json:"response"`
				} `json:"response"`
			}
			if err := json.Unmarshal(host.nativeWrites[0], &reply); err != nil {
				t.Fatal(err)
			}
			if reply.Type != "control_response" || reply.Response.RequestID != "permission-1" || reply.Response.Response.Behavior != "allow" {
				t.Fatalf("unexpected permission reply: %s", host.nativeWrites[0])
			}
			if string(reply.Response.Response.UpdatedInput) != input {
				t.Fatalf("permission reply changed tool input: got %s, want %s", reply.Response.Response.UpdatedInput, input)
			}
		})
	}
}

func TestClaudePermissionReplyRejectsNonObjectInput(t *testing.T) {
	for _, input := range []string{`[]`, `"text"`, `42`} {
		t.Run(input, func(t *testing.T) {
			host := newAdapterTestHost()
			adapter := claude.New(host, harness.LaunchConfig{Binary: "/bin/false"})
			frame := json.RawMessage(`{"type":"control_request","request_id":"permission-1","request":{"subtype":"can_use_tool","tool_name":"Bash","input":` + input + `}}`)
			if err := adapter.OnFrame(context.Background(), frame, 1); err == nil {
				t.Fatal("non-object tool input accepted")
			}
			if len(host.nativeWrites) != 0 {
				t.Fatalf("permission reply sent for invalid tool input: %s", host.nativeWrites)
			}
		})
	}
}

func TestClaudeUnknownControlRequestGetsCorrelatedError(t *testing.T) {
	host := newAdapterTestHost()
	adapter := claude.New(host, harness.LaunchConfig{Binary: "/bin/false"})
	frame := json.RawMessage(`{"type":"control_request","request_id":"future-request","request":{"subtype":"future_control","payload":{"large":9007199254740993}}}`)
	original := append(json.RawMessage(nil), frame...)
	if err := adapter.OnFrame(context.Background(), frame, 1); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(frame, original) || len(host.nativeWrites) != 1 {
		t.Fatalf("unknown control request changed or received no single reply: raw=%s replies=%d", frame, len(host.nativeWrites))
	}
	var reply struct {
		Type     string `json:"type"`
		Response struct {
			Subtype   string          `json:"subtype"`
			RequestID string          `json:"request_id"`
			Response  json.RawMessage `json:"response"`
			Error     string          `json:"error"`
		} `json:"response"`
	}
	if err := json.Unmarshal(host.nativeWrites[0], &reply); err != nil {
		t.Fatal(err)
	}
	if reply.Type != "control_response" || reply.Response.Subtype != "error" || reply.Response.RequestID != "future-request" || string(reply.Response.Response) != "null" || !strings.Contains(reply.Response.Error, "future_control") {
		t.Fatalf("unknown control request lost its correlated error or explicit null response: %s", host.nativeWrites[0])
	}
}

func TestClaudeToolArgumentsPreserveNativePayload(t *testing.T) {
	for _, input := range []string{`{ "large": 9007199254740993, "escaped": "\u0061", "text": "<&>" }`, `null`} {
		t.Run(input, func(t *testing.T) {
			host := newAdapterTestHost()
			adapter := claude.New(host, harness.LaunchConfig{Binary: "/bin/false"})
			frame := json.RawMessage(`{"type":"assistant","message":{"id":"message-1","content":[{"type":"tool_use","id":"tool-1","name":"Bash","input":` + input + `}]}}`)
			original := append(json.RawMessage(nil), frame...)
			if err := adapter.OnFrame(context.Background(), frame, 1); err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(frame, original) {
				t.Fatal("adapter changed the received frame")
			}
			for _, event := range host.events {
				if arguments := event.GetToolArguments(); arguments != nil {
					if arguments.ArgumentsJson != input {
						t.Fatalf("native tool arguments changed: got %s, want %s", arguments.ArgumentsJson, input)
					}
					return
				}
			}
			t.Fatal("native tool arguments were not projected")
		})
	}
}

func TestCodexHandshakeAndUnsupportedServerRequest(t *testing.T) {
	ctx := context.Background()
	base := newAdapterTestHost()
	base.session.Resuming = false
	host := &adapterHandshakeHost{adapterTestHost: base}
	adapter := codex.New(host, harness.LaunchConfig{Binary: "codex"})
	threadID, err := adapter.Handshake(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if threadID != "thread-created" || len(host.requests) != 2 {
		t.Fatalf("unexpected Codex handshake: thread=%q requests=%d", threadID, len(host.requests))
	}
	var initialize map[string]any
	if err := json.Unmarshal(host.requests[0], &initialize); err != nil {
		t.Fatal(err)
	}
	params := initialize["params"].(map[string]any)
	if params["clientInfo"].(map[string]any)["name"] != "agentplane" {
		t.Fatalf("Codex initialize lost client identity: %s", host.requests[0])
	}
	var start map[string]any
	if err := json.Unmarshal(host.requests[1], &start); err != nil {
		t.Fatal(err)
	}
	params = start["params"].(map[string]any)
	if params["cwd"] != base.session.Cwd || params["model"] != base.session.Model || params["approvalPolicy"] != "never" || params["sandbox"] != "danger-full-access" || params["ephemeral"] != false {
		t.Fatalf("Codex thread/start lost launch policy: %s", host.requests[1])
	}
	if err := adapter.OnFrame(ctx, json.RawMessage(`{"id":"server-request","method":"item/tool/requestUserInput","params":{}}`), 3); err != nil {
		t.Fatal(err)
	}
	var answer map[string]any
	if err := json.Unmarshal(base.nativeWrites[len(base.nativeWrites)-1], &answer); err != nil {
		t.Fatal(err)
	}
	if answer["id"] != "server-request" || answer["error"].(map[string]any)["code"] != float64(-32601) {
		t.Fatalf("Codex unsupported request response: %s", base.nativeWrites[len(base.nativeWrites)-1])
	}
}

func TestClaudeChildFramesStayOutsideRootTurnAndInputCohort(t *testing.T) {
	ctx := context.Background()
	host := newAdapterTestHost()
	adapter := claude.New(host, harness.LaunchConfig{Binary: "/bin/false"})
	if err := adapter.Submit(ctx, "input-1", "ROOT_INPUT"); err != nil {
		t.Fatal(err)
	}
	var submitted struct {
		UUID string `json:"uuid"`
	}
	if err := json.Unmarshal(host.nativeWrites[0], &submitted); err != nil {
		t.Fatal(err)
	}
	frames := []struct {
		sequence uint64
		raw      string
	}{
		{10, `{"type":"user","uuid":"root-tool-result","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"root-call","content":"ROOT_TOOL_OUTPUT"}]}}`},
		{11, `{"type":"command_lifecycle","command_uuid":"` + submitted.UUID + `","state":"started"}`},
		{12, `{"type":"assistant","parent_tool_use_id":"child-call","message":{"id":"child-message","content":[{"type":"text","text":"CHILD_ONLY"}]}}`},
		{13, `{"type":"result","parent_tool_use_id":"child-call","is_error":false,"result":"CHILD_ONLY"}`},
	}
	for _, frame := range frames {
		if err := adapter.OnFrame(ctx, json.RawMessage(frame.raw), frame.sequence); err != nil {
			t.Fatal(err)
		}
	}
	if len(host.confirmations) != 0 {
		t.Fatalf("child output closed the root input cohort: %#v", host.confirmations)
	}
	eventsBeforeClose := len(host.events)
	if err := adapter.OnFrame(ctx, json.RawMessage(`{"type":"command_lifecycle","command_uuid":"other","state":"queued"}`), 14); err != nil {
		t.Fatal(err)
	}
	if len(host.confirmations) != 1 {
		t.Fatalf("root cohort was not confirmed once: %#v", host.confirmations)
	}
	confirmation := host.confirmations[0]
	if confirmation.value.HarnessMessageID != "root-tool-result" || confirmation.value.Text != "ROOT_INPUT" || confirmation.value.TurnID != "turn-root" {
		t.Fatalf("unexpected confirmation: %#v", confirmation.value)
	}
	if !bytes.Equal(uint64Bytes(confirmation.sources), uint64Bytes([]uint64{10, 11})) {
		t.Fatalf("confirmation lost causal source sequences: %v", confirmation.sources)
	}
	if len(host.events) != eventsBeforeClose {
		t.Fatalf("child assistant/result became root observations: before=%d after=%d", eventsBeforeClose, len(host.events))
	}
}

func TestCodexIgnoresChildFramesAndKeepsUnknownItemFields(t *testing.T) {
	ctx := context.Background()
	host := newAdapterTestHost()
	adapter := codex.New(host, harness.LaunchConfig{Binary: "codex"})
	childFrame := json.RawMessage(`{"method":"item/completed","params":{"threadId":"child-thread","turnId":"child-turn","item":{"type":"agentMessage","id":"child-item","text":"CHILD_ONLY"}}}`)
	childBefore := append(json.RawMessage(nil), childFrame...)
	if err := adapter.OnFrame(ctx, childFrame, 1); err != nil {
		t.Fatal(err)
	}
	if len(host.events) != 0 || !bytes.Equal(childFrame, childBefore) {
		t.Fatalf("Codex child frame changed root state or raw evidence: events=%d raw=%s", len(host.events), childFrame)
	}
	unknown := json.RawMessage(`{"method":"item/started","params":{"threadId":"thread-root","turnId":"turn-root","item":{"type":"futureTool","id":"future-call","foo":{"x":9007199254740993},"status":"completed","exitCode":0}}}`)
	unknownBefore := append(json.RawMessage(nil), unknown...)
	if err := adapter.OnFrame(ctx, unknown, 2); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(unknown, unknownBefore) {
		t.Fatalf("unknown frame was rewritten: %s", unknown)
	}
	if len(host.events) != 2 || host.events[0].GetItemStarted() == nil || host.events[1].GetToolArguments() == nil {
		t.Fatalf("unknown tool was not projected as a tool call: %#v", host.events)
	}
	started := host.events[0].GetItemStarted()
	if started.GetToolName() != "futureTool" || started.GetItemId() != "future-call" {
		t.Fatalf("unknown item identity was lost: %#v", started)
	}
	var arguments map[string]json.RawMessage
	if err := json.Unmarshal([]byte(host.events[1].GetToolArguments().ArgumentsJson), &arguments); err != nil {
		t.Fatal(err)
	}
	if len(arguments) != 1 || arguments["foo"] == nil {
		t.Fatalf("unknown item's outcome fields leaked into arguments: %s", host.events[1].GetToolArguments().ArgumentsJson)
	}
	if string(arguments["foo"]) != `{"x":9007199254740993}` {
		t.Fatalf("unknown item's payload lost precision: %s", arguments["foo"])
	}
}

func uint64Bytes(values []uint64) []byte {
	encoded, _ := json.Marshal(values)
	return encoded
}

func TestClaudeCancellationSettlesOnlyPendingInputOnce(t *testing.T) {
	ctx := context.Background()
	host := newAdapterTestHost()
	adapter := claude.New(host, harness.LaunchConfig{Binary: "/bin/false"})
	if err := adapter.Submit(ctx, "input-1", "hello"); err != nil {
		t.Fatal(err)
	}
	var request struct {
		UUID string `json:"uuid"`
	}
	if err := json.Unmarshal(host.nativeWrites[0], &request); err != nil {
		t.Fatal(err)
	}
	for index, id := range []string{"unknown", request.UUID, request.UUID} {
		frame, err := json.Marshal(map[string]string{"type": "command_lifecycle", "state": "cancelled", "command_uuid": id})
		if err != nil {
			t.Fatal(err)
		}
		if err := adapter.OnFrame(ctx, frame, uint64(index+1)); err != nil {
			t.Fatal(err)
		}
	}
	if len(host.noops) != 1 || host.noops[0] != "input-1" {
		t.Fatalf("cancelled input receipts = %v", host.noops)
	}
}

func TestRecoveryComparesArgumentsWithoutLosingNumbers(t *testing.T) {
	for _, test := range []struct {
		name, observed, recovered string
		retained                  bool
	}{
		{"formatting", `{"a":1,"b":[true,null,"text"]}`, " {\n\"b\":[true,null,\"text\"], \"a\":1 } ", true},
		{"large integer", `{"n":9007199254740992}`, `{"n":9007199254740993}`, false},
		{"precise decimal", `{"n":0.10000000000000001}`, `{"n":0.10000000000000002}`, false},
		{"number spelling", `{"n":1}`, `{"n":1.0}`, false},
		{"trailing data", `{"n":1}`, `{"n":1} {}`, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			observed := harness.ObservedItem{ItemID: "call", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, Arguments: test.observed}
			recovered := observed
			recovered.Arguments = test.recovered
			result := harness.CompareItem(observed, &recovered)
			want := eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_REVISED
			if test.retained {
				want = eventpb.RecoveryDisposition_RECOVERY_DISPOSITION_RETAINED
			}
			if result.Disposition != want {
				t.Fatalf("comparison = %v, want %v", result.Disposition, want)
			}
			if !test.retained && result.Replacement.ArgumentsJson != test.recovered {
				t.Fatalf("revision lost the recovered payload: %v", result)
			}
		})
	}
}
