package claude

import (
	"bytes"
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/testing/adaptertest"
)

func newAdapterTestHost() *adaptertest.Host { return adaptertest.NewHost() }

func TestClaudeLateControlResponseSettlesPendingModelChange(t *testing.T) {
	ctx := context.Background()
	host := newAdapterTestHost()
	adapter := New(host, harness.LaunchConfig{Binary: "/bin/false"})
	if err := adapter.ChangeModel(ctx, "model-1", "claude-model"); err != nil {
		t.Fatalf("ChangeModel error = %v", err)
	}
	if len(host.NativeRequests) != 1 || host.NativeRequests[0].Kind != harness.ClaudeModelChange || !slices.Equal(host.NativeRequests[0].CommandIDs, []string{"model-1"}) {
		t.Fatalf("Claude requests = %+v", host.NativeRequests)
	}
	requestID := host.NativeRequests[0].ID
	if requestID == "" {
		t.Fatal("Claude did not record a native request ID")
	}
	if len(host.NativeWrites) != 1 || host.SessionState.Model != "test-model" {
		t.Fatalf("model command did not remain pending after dispatch: writes=%d model=%q", len(host.NativeWrites), host.SessionState.Model)
	}
	var request struct {
		Type      string `json:"type"`
		RequestID string `json:"request_id"`
	}
	if err := json.Unmarshal(host.NativeWrites[0], &request); err != nil || request.Type != "control_request" || request.RequestID != requestID {
		t.Fatalf("Claude model request correlation = %+v, err=%v", request, err)
	}
	frame, err := json.Marshal(map[string]any{
		"type":     "control_response",
		"response": map[string]any{"subtype": "success", "request_id": requestID, "response": map[string]any{}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := adapter.OnFrame(ctx, frame, 19); err != nil {
		t.Fatal(err)
	}
	if host.SessionState.Model != "claude-model" || len(host.Failures) != 0 {
		t.Fatalf("late Claude control response: model=%q failures=%v", host.SessionState.Model, host.Failures)
	}
}

func TestClaudeOutboundFramesKeepNativeNullableFields(t *testing.T) {
	ctx := context.Background()
	host := newAdapterTestHost()
	adapter := New(host, harness.LaunchConfig{Binary: "/bin/false"})
	if err := adapter.Submit(ctx, "input-1", "hello"); err != nil {
		t.Fatal(err)
	}
	if err := adapter.OnFrame(ctx, json.RawMessage(`{"type":"control_request","request_id":"permission-1","request":{"subtype":"can_use_tool","tool_name":"Bash","input":{"command":"true"}}}`), 1); err != nil {
		t.Fatal(err)
	}
	var user map[string]any
	if err := json.Unmarshal(host.NativeWrites[0], &user); err != nil {
		t.Fatal(err)
	}
	if parent, ok := user["parent_tool_use_id"]; !ok || parent != nil {
		t.Fatalf("Claude user frame omitted its explicit null parent id: %s", host.NativeWrites[0])
	}
	var answer map[string]any
	if err := json.Unmarshal(host.NativeWrites[1], &answer); err != nil {
		t.Fatal(err)
	}
	response := answer["response"].(map[string]any)
	if value, ok := response["error"]; !ok || value != nil {
		t.Fatalf("Claude success response omitted its null error field: %s", host.NativeWrites[1])
	}
	if err := adapter.Interrupt(ctx, "turn-root"); err != nil {
		t.Fatal(err)
	}
	var control struct {
		RequestID string `json:"request_id"`
	}
	if err := json.Unmarshal(host.NativeWrites[2], &control); err != nil {
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
			adapter := New(host, harness.LaunchConfig{Binary: "/bin/false"})
			frame := json.RawMessage(`{"type":"control_request","request_id":"permission-1","request":{"subtype":"can_use_tool","tool_name":"Bash","input":` + input + `}}`)
			if err := adapter.OnFrame(context.Background(), frame, 1); err != nil {
				t.Fatal(err)
			}
			if len(host.NativeWrites) != 1 {
				t.Fatalf("expected one permission reply, got %d", len(host.NativeWrites))
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
			if err := json.Unmarshal(host.NativeWrites[0], &reply); err != nil {
				t.Fatal(err)
			}
			if reply.Type != "control_response" || reply.Response.RequestID != "permission-1" || reply.Response.Response.Behavior != "allow" {
				t.Fatalf("unexpected permission reply: %s", host.NativeWrites[0])
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
			adapter := New(host, harness.LaunchConfig{Binary: "/bin/false"})
			frame := json.RawMessage(`{"type":"control_request","request_id":"permission-1","request":{"subtype":"can_use_tool","tool_name":"Bash","input":` + input + `}}`)
			if err := adapter.OnFrame(context.Background(), frame, 1); err == nil {
				t.Fatal("non-object tool input accepted")
			}
			if len(host.NativeWrites) != 0 {
				t.Fatalf("permission reply sent for invalid tool input: %s", host.NativeWrites)
			}
		})
	}
}

func TestClaudeUnknownControlRequestGetsCorrelatedError(t *testing.T) {
	host := newAdapterTestHost()
	adapter := New(host, harness.LaunchConfig{Binary: "/bin/false"})
	frame := json.RawMessage(`{"type":"control_request","request_id":"future-request","request":{"subtype":"future_control","payload":{"large":9007199254740993}}}`)
	original := append(json.RawMessage(nil), frame...)
	if err := adapter.OnFrame(context.Background(), frame, 1); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(frame, original) || len(host.NativeWrites) != 1 {
		t.Fatalf("unknown control request changed or received no single reply: raw=%s replies=%d", frame, len(host.NativeWrites))
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
	if err := json.Unmarshal(host.NativeWrites[0], &reply); err != nil {
		t.Fatal(err)
	}
	if reply.Type != "control_response" || reply.Response.Subtype != "error" || reply.Response.RequestID != "future-request" || string(reply.Response.Response) != "null" || !strings.Contains(reply.Response.Error, "future_control") {
		t.Fatalf("unknown control request lost its correlated error or explicit null response: %s", host.NativeWrites[0])
	}
}

func TestClaudeToolArgumentsPreserveNativePayload(t *testing.T) {
	for _, input := range []string{`{ "large": 9007199254740993, "escaped": "\u0061", "text": "<&>" }`, `null`} {
		t.Run(input, func(t *testing.T) {
			host := newAdapterTestHost()
			adapter := New(host, harness.LaunchConfig{Binary: "/bin/false"})
			frame := json.RawMessage(`{"type":"assistant","message":{"id":"message-1","content":[{"type":"tool_use","id":"tool-1","name":"Bash","input":` + input + `}]}}`)
			original := append(json.RawMessage(nil), frame...)
			if err := adapter.OnFrame(context.Background(), frame, 1); err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(frame, original) {
				t.Fatal("adapter changed the received frame")
			}
			for _, event := range host.Events {
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

func TestClaudeChildFramesStayOutsideRootTurnAndInputCohort(t *testing.T) {
	ctx := context.Background()
	host := newAdapterTestHost()
	adapter := New(host, harness.LaunchConfig{Binary: "/bin/false"})
	if err := adapter.Submit(ctx, "input-1", "ROOT_INPUT"); err != nil {
		t.Fatal(err)
	}
	var submitted struct {
		UUID string `json:"uuid"`
	}
	if err := json.Unmarshal(host.NativeWrites[0], &submitted); err != nil {
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
	if len(host.Confirmations) != 0 {
		t.Fatalf("child output closed the root input cohort: %#v", host.Confirmations)
	}
	eventsBeforeClose := len(host.Events)
	if err := adapter.OnFrame(ctx, json.RawMessage(`{"type":"command_lifecycle","command_uuid":"other","state":"queued"}`), 14); err != nil {
		t.Fatal(err)
	}
	if len(host.Confirmations) != 1 {
		t.Fatalf("root cohort was not confirmed once: %#v", host.Confirmations)
	}
	confirmation := host.Confirmations[0]
	if confirmation.Value.HarnessMessageID != "root-tool-result" || confirmation.Value.Text != "ROOT_INPUT" || confirmation.Value.TurnID != "turn-root" {
		t.Fatalf("unexpected confirmation: %#v", confirmation.Value)
	}
	if !bytes.Equal(uint64Bytes(confirmation.Sources), uint64Bytes([]uint64{10, 11})) {
		t.Fatalf("confirmation lost causal source sequences: %v", confirmation.Sources)
	}
	if len(host.Events) != eventsBeforeClose {
		t.Fatalf("child assistant/result became root observations: before=%d after=%d", eventsBeforeClose, len(host.Events))
	}
}

func uint64Bytes(values []uint64) []byte {
	encoded, _ := json.Marshal(values)
	return encoded
}

func TestClaudeCancellationSettlesOnlyPendingInputOnce(t *testing.T) {
	ctx := context.Background()
	host := newAdapterTestHost()
	adapter := New(host, harness.LaunchConfig{Binary: "/bin/false"})
	if err := adapter.Submit(ctx, "input-1", "hello"); err != nil {
		t.Fatal(err)
	}
	var request struct {
		UUID string `json:"uuid"`
	}
	if err := json.Unmarshal(host.NativeWrites[0], &request); err != nil {
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
	if len(host.Noops) != 1 || host.Noops[0] != "input-1" {
		t.Fatalf("cancelled input receipts = %v", host.Noops)
	}
}
