package chatcompletions

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

type observationSink struct {
	events   []*eventpb.Event
	progress []bool
}

func (s *observationSink) Record(_ context.Context, _ protocol.ProviderEvidence, semanticProgress bool, events ...*eventpb.Event) error {
	s.events = append(s.events, events...)
	s.progress = append(s.progress, semanticProgress)
	return nil
}

func TestStreamingReasoningObservations(t *testing.T) {
	frames := []string{
		`{"id":"chat","choices":[{"delta":{"role":"assistant"}}]}`,
		`{"id":"chat","choices":[{"delta":{"reasoning_content":"First "}}]}`,
		`{"id":"chat","choices":[{"delta":{"reasoning_content":"thought","content":"Answer"}}]}`,
		`{"id":"chat","choices":[{"delta":{},"finish_reason":"stop"}]}`,
		`{"id":"chat","choices":[],"usage":{"total_tokens":251}}`,
		`[DONE]`,
	}
	for _, compact := range []bool{false, true} {
		sink := &observationSink{}
		callbacks := 0
		hooks := protocol.ProviderHooks{Progress: func() error {
			if len(sink.progress) == 0 || !sink.progress[len(sink.progress)-1] {
				t.Fatal("idle callback ran before evidence with semantic progress was recorded")
			}
			callbacks++
			return nil
		}}
		result, err := readStream(t.Context(), sse(frames...), sink, compact, hooks)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(string(result.Record), `"reasoning_content":"First thought"`) {
			t.Fatalf("reasoning missing from conversation record: %s", result.Record)
		}
		if want := []bool{false, true, true, true, false, false}; !slices.Equal(sink.progress, want) || callbacks != 3 {
			t.Fatalf("semantic progress by frame=%v, want %v, idle callbacks=%d", sink.progress, want, callbacks)
		}
		if compact {
			if len(sink.events) != 0 {
				t.Fatalf("compaction published observations: %v", sink.events)
			}
			continue
		}
		if len(sink.events) != 7 {
			t.Fatalf("unexpected observations: %v", sink.events)
		}
		if start := sink.events[0].GetItemStarted(); start.GetItemId() != "chat#reasoning" || start.GetKind() != eventpb.ItemKind_ITEM_KIND_REASONING {
			t.Fatalf("reasoning start: %v", start)
		}
		for i, want := range []string{"First ", "thought"} {
			delta := sink.events[i+1].GetTextDelta()
			if delta.GetItemId() != "chat#reasoning" || delta.GetText() != want {
				t.Fatalf("reasoning delta: %v", delta)
			}
		}
		if start := sink.events[3].GetItemStarted(); start.GetItemId() != "chat" || start.GetKind() != eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT {
			t.Fatalf("answer start: %v", start)
		}
		if completed := sink.events[5].GetItemCompleted(); completed.GetItemId() != "chat#reasoning" || completed.GetText() != "First thought" {
			t.Fatalf("reasoning completion: %v", completed)
		}
		if completed := sink.events[6].GetItemCompleted(); completed.GetItemId() != "chat" || completed.GetText() != "Answer" {
			t.Fatalf("answer completion: %v", completed)
		}
	}
}

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
		{name: "output limit", frames: []string{`{"id":"chat","choices":[{"delta":{"content":"Partial"},"finish_reason":"length"}]}`, "[DONE]"}, transient: true, fails: true},
		{name: "unfinished function arguments", frames: []string{strings.Replace(tool, `"arguments":"{}"`, `"arguments":"{"`, 1), callFinish, "[DONE]"}, transient: true, fails: true},
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

func TestUnsupportedFinishReason(t *testing.T) {
	const frame = `{"id":"chat","choices":[{"delta":{"content":"partial"},"finish_reason":"vendor_future_stop"}]}`
	result, err := readStream(t.Context(), sse(frame, "[DONE]"), nil, false, protocol.ProviderHooks{})
	var failure *protocol.KnownFailureError
	if result != nil || !errors.As(err, &failure) || failure.Error() != `Chat Completions returned an unsupported finish_reason: "vendor_future_stop"` {
		t.Fatalf("result=%v error=%v", result, err)
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

func TestCompactionKeepsRecentMessagesAndToolBatches(t *testing.T) {
	c := &Context{}
	if err := c.AppendRecords([]json.RawMessage{
		json.RawMessage(`{"role":"user","content":"` + strings.Repeat("old data ", 100) + `"}`),
		json.RawMessage(`{"role":"assistant","content":"old answer"}`),
		json.RawMessage(`{"role":"user","content":"recent task"}`),
		json.RawMessage(`{"role":"assistant","reasoning_content":"older reasoning","tool_calls":[{"id":"earlier-a","type":"function","function":{"name":"exec","arguments":"{}"}},{"id":"earlier-b","type":"function","function":{"name":"exec","arguments":"{}"}}]}`),
		json.RawMessage(`{"role":"tool","content":"` + strings.Repeat("large result ", 100) + `","tool_call_id":"earlier-a"}`),
		json.RawMessage(`{"role":"tool","content":"small result","tool_call_id":"earlier-b"}`),
		json.RawMessage(`{"role":"assistant","content":"Verbatim public progress","reasoning_content":"latest reasoning","tool_calls":[{"id":"latest-a","type":"function","function":{"name":"exec","arguments":"{}"}},{"id":"latest-b","type":"function","function":{"name":"exec","arguments":"{}"}}]}`),
		json.RawMessage(`{"role":"tool","content":"recent result","tool_call_id":"latest-a"}`),
		json.RawMessage(`{"role":"tool","content":"other recent result","tool_call_id":"latest-b"}`),
	}); err != nil {
		t.Fatal(err)
	}
	if c.CanCompact(10_000) {
		t.Fatal("history fits the recent token budget")
	}
	// The last result of the earlier batch would fit alone.
	const cut = 6
	budget := 0
	for _, m := range c.messages[cut-1:] {
		budget += m.estimatedTokens()
	}
	plan, err := c.PrepareCompaction(CompactionConfig{KeepRecentTokens: &budget}, "current environment")
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
		{finish: "stop", content: strings.Repeat("expanded summary ", 500)},
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
	if result.CanCompact(budget) {
		t.Fatal("only generated records precede the recent tail after compaction")
	}
	wantSuffix, err := c.RecordsFrom(cut)
	if err != nil {
		t.Fatal(err)
	}
	wantSuffix[0] = bytes.Replace(wantSuffix[0], []byte(`"reasoning_content":"latest reasoning",`), nil, 1)
	gotSuffix, err := result.RecordsFrom(2)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.EqualFunc(wantSuffix, gotSuffix, func(a, b json.RawMessage) bool { return bytes.Equal(a, b) }) {
		t.Fatalf("retained messages changed beyond omitting reasoning: %s", gotSuffix)
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

func TestSecondCompactionSummarizesPriorHandoff(t *testing.T) {
	state := &Context{}
	if err := state.AppendRecords([]json.RawMessage{
		json.RawMessage(`{"role":"user","content":"` + strings.Repeat("old data ", 100) + `"}`),
		json.RawMessage(`{"role":"user","content":"recent task"}`),
	}); err != nil {
		t.Fatal(err)
	}
	config := CompactionConfig{
		MaxTokens: 100, KeepRecentTokens: new(32),
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
	if restored.CanCompact(config.RecentTokenBudget()) {
		t.Fatal("reloaded user summary triggered compaction by itself")
	}
	projected, err := restored.ProjectRecords(0)
	if err != nil {
		t.Fatal(err)
	}
	if len(projected) != 2 || string(projected[0]) != `{"role":"user","content":"Handoff: useful summary"}` {
		t.Fatalf("unexpected projected records: %s", projected)
	}
	if err := restored.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: strings.Repeat("new work ", 100)}}); err != nil {
		t.Fatal(err)
	}
	if err := restored.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: "newest request"}}); err != nil {
		t.Fatal(err)
	}
	next, err := restored.PrepareCompaction(config, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(next.prefix) != 3 || next.prefix[0].Content != "Handoff: useful summary" ||
		len(next.suffix) != 1 || next.suffix[0].Content != "newest request" {
		t.Fatal("next prefix must carry the earlier handoff along with newly compacted messages")
	}
}

func TestCompactionSummarizesAnOversizeNewestUserMessage(t *testing.T) {
	context := &Context{}
	user := strings.Repeat("user-provided data ", 100)
	if err := context.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: user}}); err != nil {
		t.Fatal(err)
	}
	config := CompactionConfig{MaxTokens: 64, KeepRecentTokens: new(32)}
	plan, err := context.PrepareCompaction(config, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.prefix) != 1 || plan.prefix[0].Content != user || len(plan.suffix) != 0 {
		t.Fatal("overlong input must be supplied in full to the summarizer")
	}
	result, err := plan.Apply([]json.RawMessage{json.RawMessage(`{"role":"assistant","content":"user provided input data"}`)}, Audit{FinishReason: "stop"})
	if err != nil {
		t.Fatal(err)
	}
	if context.messages[0].Content != user || result.RecordCount() != 1 || result.TurnComplete() ||
		result.CanCompact(config.RecentTokenBudget()) {
		t.Fatal("summary must preserve the original and allow continuation without retriggering compaction")
	}
}

func TestCompactionBudgetSettings(t *testing.T) {
	for _, tc := range []struct {
		name          string
		tokens, turns *int
		want          int
	}{
		{name: "explicit token budget", tokens: new(200), want: 200},
		{name: "staging rollout compatibility", turns: new(2), want: 16_000},
		{name: "missing budget"},
		{name: "ambiguous budget", tokens: new(200), turns: new(2)},
		{name: "zero token budget", tokens: new(0)},
		{name: "negative legacy setting", turns: new(-1)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			config := CompactionConfig{MaxTokens: 100, KeepRecentTokens: tc.tokens, KeepRecentTurns: tc.turns}
			err := config.Validate()
			if tc.want == 0 && err == nil || tc.want > 0 && (err != nil || config.RecentTokenBudget() != tc.want) {
				t.Fatalf("configured budget = %d, err = %v; want valid budget of %d", config.RecentTokenBudget(), err, tc.want)
			}
		})
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
				MaxTokens: 100, KeepRecentTokens: new(32),
				SummaryPromptTemplate: tc.prompt, Replacement: tc.replacement,
			}
			if err := config.Validate(); err == nil {
				t.Fatal("invalid compaction config accepted")
			}
		})
	}
}
