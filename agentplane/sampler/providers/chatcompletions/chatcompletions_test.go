package chatcompletions

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

func sse(frames ...string) *strings.Reader {
	return strings.NewReader("data: " + strings.Join(frames, "\n\ndata: ") + "\n\n")
}

func TestStreamingToolCallsAndReplay(t *testing.T) {
	result, err := readStream(t.Context(), sse(
		`{"id":"chat","choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"checking","tool_calls":[{"index":0,"id":"a","type":"function","function":{"name":"exec","arguments":"{\"code\":"}}]}}]}`,
		`{"id":"chat","choices":[{"index":0,"delta":{"tool_calls":[{"index":1,"id":"b","type":"function","function":{"name":"exec","arguments":"{}"}},{"index":0,"function":{"arguments":"\"return 3\"}"}}]}}]}`,
		`{"id":"chat","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}`,
		`{"id":"chat","choices":[],"usage":{"total_tokens":251}}`,
		`[DONE]`,
	), nil, protocol.ProviderHooks{})
	if err != nil {
		t.Fatal(err)
	}
	if result.ContextTokens == nil || *result.ContextTokens != 251 {
		t.Fatalf("context tokens: %v", result.ContextTokens)
	}
	c := &Context{}
	if err := c.AppendRecords([]json.RawMessage{result.Record}); err != nil {
		t.Fatal(err)
	}
	calls, err := c.PendingCalls()
	if err != nil || len(calls) != 2 || calls[0].CallID != "a" || calls[0].Input != `{"code":"return 3"}` || calls[1].CallID != "b" {
		t.Fatalf("unexpected pending calls: %+v, %v", calls, err)
	}
	if err := c.AppendToolResult(calls[0], []protocol.ContentPart{{Type: "input_text", Text: "3"}}); err != nil {
		t.Fatal(err)
	}
	if err := c.AppendToolResult(calls[1], []protocol.ContentPart{{Type: "input_image"}}); err != nil {
		t.Fatal(err)
	}
	if err := c.AppendToolResult(calls[0], []protocol.ContentPart{{Type: "input_text", Text: "updated output"}}); err != nil {
		t.Fatal(err)
	}
	records, err := c.Records()
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 4 || !strings.Contains(string(records[0]), `"reasoning_content":"checking"`) ||
		!strings.Contains(string(records[1]), `"tool_call_id":"a"`) ||
		!strings.Contains(string(records[2]), "cannot display") ||
		!strings.Contains(string(records[3]), `"role":"system"`) {
		t.Fatalf("unexpected native messages: %s", records)
	}
	reloaded := &Context{}
	if err := reloaded.AppendRecords(records); err != nil {
		t.Fatal(err)
	}
	if err := reloaded.CheckSettled(); err != nil || reloaded.TurnComplete() {
		t.Fatalf("restored context should need another sample: %v", err)
	}
}

func TestStreamCompletion(t *testing.T) {
	const tool = `{"id":"chat","choices":[{"delta":{"tool_calls":[{"index":0,"id":"a","type":"function","function":{"name":"exec","arguments":"{}"}}]}}]}`
	const callFinish = `{"id":"chat","choices":[{"delta":{},"finish_reason":"tool_calls"}]}`
	const stop = `{"id":"chat","choices":[{"delta":{"content":"Done"},"finish_reason":"stop"}]}`
	for _, tc := range []struct {
		name      string
		frames    []string
		transient bool
		fails     bool
	}{
		{name: "clean stop", frames: []string{stop, "[DONE]"}},
		{name: "truncated after tool delta", frames: []string{tool}, transient: true, fails: true},
		{name: "truncated after finish reason", frames: []string{tool, callFinish}, transient: true, fails: true},
		{name: "contradictory finish reason", frames: []string{tool, stop, "[DONE]"}, fails: true},
		{name: "output limit", frames: []string{`{"id":"chat","choices":[{"delta":{"content":"Partial"},"finish_reason":"length"}]}`, "[DONE]"}, fails: true},
		{name: "provider error", frames: []string{`{"error":{"message":"completion aborted"}}`}, fails: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			result, err := readStream(t.Context(), sse(tc.frames...), nil, protocol.ProviderHooks{})
			if tc.fails {
				var transient *protocol.ProviderTransientError
				if result != nil || err == nil || errors.As(err, &transient) != tc.transient {
					t.Fatalf("result=%+v error=%v", result, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			c := &Context{}
			if err := c.AppendRecords([]json.RawMessage{result.Record}); err != nil || !c.TurnComplete() {
				t.Fatalf("clean stop should complete the turn: %v", err)
			}
		})
	}
}
