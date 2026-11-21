package chatcompletions

import (
	"bytes"
	"encoding/json"
	"errors"
	"slices"
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
	), nil, false, protocol.ProviderHooks{})
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
	const reasoning = `{"id":"chat","choices":[{"delta":{"reasoning_content":"checking"}}]}`
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
		{name: "tool calls finish without tool deltas", frames: []string{reasoning, callFinish, "[DONE]"}, transient: true, fails: true},
		{name: "contradictory finish reason", frames: []string{tool, stop, "[DONE]"}, fails: true},
		{name: "output limit", frames: []string{`{"id":"chat","choices":[{"delta":{"content":"Partial"},"finish_reason":"length"}]}`, "[DONE]"}, fails: true},
		{name: "provider error", frames: []string{`{"error":{"message":"completion aborted"}}`}, fails: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			result, err := readStream(t.Context(), sse(tc.frames...), nil, false, protocol.ProviderHooks{})
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

// Staging LiteLLM 1.103.0 appended this usage-bearing empty choice after a
// finished TabbyAPI response on 2026-11-20.
func TestTrailingUsageChoice(t *testing.T) {
	const stop = `{"id":"chat","choices":[{"index":0,"delta":{"content":"Done"},"finish_reason":"stop"}]}`
	for _, tc := range []struct {
		name, trailing string
		accept         bool
	}{
		{name: "empty choice with usage", trailing: `{"id":"chat","choices":[{"index":0,"delta":{}}],"usage":{"total_tokens":251}}`, accept: true},
		{name: "empty choice and null finish", trailing: `{"id":"chat","choices":[{"index":0,"delta":{},"finish_reason":null}],"usage":{"total_tokens":251}}`, accept: true},
		{name: "missing usage", trailing: `{"id":"chat","choices":[{"index":0,"delta":{}}]}`},
		{name: "null usage", trailing: `{"id":"chat","choices":[{"index":0,"delta":{}}],"usage":null}`},
		{name: "late content", trailing: `{"id":"chat","choices":[{"index":0,"delta":{"content":"late"}}],"usage":{"total_tokens":251}}`},
		{name: "late tool", trailing: `{"id":"chat","choices":[{"index":0,"delta":{"tool_calls":[{}]}}],"usage":{"total_tokens":251}}`},
		{name: "unknown delta", trailing: `{"id":"chat","choices":[{"index":0,"delta":{"audio":"late"}}],"usage":{"total_tokens":251}}`},
		{name: "unknown choice", trailing: `{"id":"chat","choices":[{"index":0,"delta":{},"logprobs":{}}],"usage":{"total_tokens":251}}`},
		{name: "repeated finish", trailing: `{"id":"chat","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":251}}`},
		{name: "wrong index", trailing: `{"id":"chat","choices":[{"index":1,"delta":{}}],"usage":{"total_tokens":251}}`},
		{name: "null delta", trailing: `{"id":"chat","choices":[{"index":0,"delta":null}],"usage":{"total_tokens":251}}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			result, err := readStream(t.Context(), sse(stop, tc.trailing, "[DONE]"), nil, false, protocol.ProviderHooks{})
			if !tc.accept {
				if err == nil || result != nil {
					t.Fatalf("accepted invalid trailing choice: %+v", result)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if result.Audit.FinishReason != "stop" || result.ContextTokens == nil || *result.ContextTokens != 251 || !bytes.Contains(result.Audit.Usage, []byte(`"total_tokens":251`)) || !bytes.Contains(result.Record, []byte(`"content":"Done"`)) {
				t.Fatalf("lost result or usage: %+v", result)
			}
		})
	}
	if result, err := readStream(t.Context(), sse(stop, `{"id":"chat","choices":[{"index":0,"delta":{}}],"usage":{"total_tokens":251}}`), nil, false, protocol.ProviderHooks{}); err == nil || result != nil {
		t.Fatalf("accepted stream without DONE: %+v %v", result, err)
	}
}

func TestCompactionKeepsRecentTurnsAndToolPairs(t *testing.T) {
	c := &Context{}
	if err := c.AppendRecords([]json.RawMessage{
		json.RawMessage(`{"role":"user","content":"` + strings.Repeat("old data ", 100) + `"}`),
		json.RawMessage(`{"role":"assistant","content":"old answer"}`),
		json.RawMessage(`{"role":"user","content":"recent task"}`),
		json.RawMessage(`{"role":"assistant","tool_calls":[{"id":"latest-call","type":"function","function":{"name":"exec","arguments":"{}"}}]}`),
		json.RawMessage(`{"role":"tool","content":"recent result","tool_call_id":"latest-call"}`),
	}); err != nil {
		t.Fatal(err)
	}
	if c.CanCompact(2) {
		t.Fatal("two retained turns leave no older turn")
	}
	plan, err := c.PrepareCompaction(CompactionConfig{KeepRecentTurns: 1}, "current environment")
	if err != nil {
		t.Fatal(err)
	}
	initial, err := c.MarshalJSON()
	if err != nil {
		t.Fatal(err)
	}
	for _, bad := range []struct {
		finish  string
		content string
	}{
		{finish: "tool_calls", content: "stray call"},
		{finish: "stop", content: "  "},
		{finish: "stop", content: strings.Repeat("expanded summary ", 100)},
	} {
		raw, err := json.Marshal(message{Role: "assistant", Content: bad.content})
		if err != nil {
			t.Fatal(err)
		}
		if replacement, err := plan.Apply([]json.RawMessage{raw}, Audit{FinishReason: bad.finish}); err == nil || replacement != nil {
			t.Fatalf("invalid summary was accepted with finish reason %q", bad.finish)
		}
	}
	result, err := plan.Apply([]json.RawMessage{json.RawMessage(`{"role":"assistant","reasoning_content":"private reasoning","content":"useful summary"}`)}, Audit{FinishReason: "stop"})
	if err != nil {
		t.Fatal(err)
	}
	if err := result.CheckSettled(); err != nil {
		t.Fatal(err)
	}
	if result.CanCompact(1) {
		t.Fatal("synthetic summary counted as another user turn")
	}
	wantSuffix, err := c.RecordsFrom(2)
	if err != nil {
		t.Fatal(err)
	}
	gotSuffix, err := result.RecordsFrom(2)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.EqualFunc(wantSuffix, gotSuffix, func(a, b json.RawMessage) bool { return bytes.Equal(a, b) }) {
		t.Fatalf("retained turns changed: %s", gotSuffix)
	}
	wire, err := result.MarshalJSON()
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(wire), "old data") || strings.Contains(string(wire), "private reasoning") ||
		!strings.Contains(string(wire), "useful summary") || !strings.Contains(string(wire), "current environment") {
		t.Fatalf("unexpected replacement: %s", wire)
	}
	after, err := c.MarshalJSON()
	if err != nil || !bytes.Equal(initial, after) {
		t.Fatal("compaction mutated the original conversation")
	}
}

func TestUserCompactionSummaryIsNotCountedAsAnAuthoredTurn(t *testing.T) {
	state := &Context{}
	if err := state.AppendRecords([]json.RawMessage{
		json.RawMessage(`{"role":"user","content":"` + strings.Repeat("old data ", 100) + `"}`),
		json.RawMessage(`{"role":"user","content":"recent task"}`),
	}); err != nil {
		t.Fatal(err)
	}
	config := CompactionConfig{
		MaxTokens: 100, KeepRecentTurns: 1,
		Replacement: &CompactionReplacement{Role: "user", Template: "Handoff: {{.Summary}}"},
	}
	if err := config.Validate(); err != nil {
		t.Fatal(err)
	}
	plan, err := state.PrepareCompaction(config, "")
	if err != nil {
		t.Fatal(err)
	}
	result, err := plan.Apply([]json.RawMessage{json.RawMessage(`{"role":"assistant","content":"useful summary"}`)}, Audit{FinishReason: "stop"})
	if err != nil {
		t.Fatal(err)
	}
	records, err := result.Records()
	if err != nil {
		t.Fatal(err)
	}
	restored := &Context{}
	if err := restored.AppendRecords(records); err != nil {
		t.Fatal(err)
	}
	if restored.CanCompact(1) {
		t.Fatal("reloaded user summary must not count as old conversation")
	}
	projected, err := restored.ProjectRecords(0)
	if err != nil {
		t.Fatal(err)
	}
	if len(projected) != 2 || string(projected[0]) != `{"role":"user","content":"Handoff: useful summary"}` {
		t.Fatalf("unexpected projected records: %s", projected)
	}
}

func TestCompactionRejectsInvalidTemplates(t *testing.T) {
	for _, tc := range []struct {
		name        string
		prompt      string
		replacement *CompactionReplacement
	}{
		{name: "bad prompt field", prompt: "{{.Unknown}}"},
		{name: "bad replacement syntax", replacement: &CompactionReplacement{Role: "user", Template: "{{"}},
		{name: "summary omitted", replacement: &CompactionReplacement{Role: "user", Template: "No handoff"}},
		{name: "system message", replacement: &CompactionReplacement{Role: "system", Template: "{{.Summary}}"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			config := CompactionConfig{
				MaxTokens: 100, KeepRecentTurns: 1,
				SummaryPromptTemplate: tc.prompt, Replacement: tc.replacement,
			}
			if err := config.Validate(); err == nil {
				t.Fatal("invalid compaction config accepted")
			}
		})
	}
}
