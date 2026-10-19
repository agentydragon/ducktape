// Package spans builds runner journal spans of one streamed item, shaped like the staging frames the
// delta templates match.
package spans

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	source        = "test-source"
	claudeSession = "test-claude-session"
	codexThread   = "test-codex-thread"
	codexTurn     = "test-codex-turn"
)

// Span is a journal prefix whose cursors run from 1.
type Span struct{ Entries []*eventlogpb.EventEntry }

func (s *Span) next() *eventlogpb.EventEntry {
	cursor := uint64(len(s.Entries) + 1)
	entry := &eventlogpb.EventEntry{
		Cursor: cursor,
		Origin: &eventlogpb.EventOrigin{SourceId: source, Sequence: cursor},
		Event:  &eventpb.Event{At: timestamppb.New(time.UnixMilli(1000 * int64(cursor)))},
	}
	s.Entries = append(s.Entries, entry)
	return entry
}

// Native appends a native frame from the harness and returns its cursor.
func (s *Span) Native(frame map[string]any) uint64 {
	entry := s.next()
	entry.Event.Observation = &eventpb.Event_Native{Native: &eventpb.Native{
		Direction: eventpb.Direction_DIRECTION_FROM_HARNESS, Line: encode(frame),
	}}
	return entry.Cursor
}

// Derived appends the runner's Event made from the native frames at sources and returns its cursor.
func (s *Span) Derived(sources []uint64, event *eventpb.Event) uint64 {
	entry := s.next()
	entry.Event.Observation = event.Observation
	entry.Event.SourceSequences = sources
	return entry.Cursor
}

// Stderr appends a harness stderr line, an anomaly that keeps a span whole.
func (s *Span) Stderr(line string) uint64 {
	entry := s.next()
	entry.Event.Observation = &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: line}}
	return entry.Cursor
}

func encode(frame map[string]any) string {
	line, err := json.Marshal(frame)
	if err != nil {
		panic(err)
	}
	return string(line)
}

// With returns frame with the extra keys added.
func With(frame map[string]any, extra map[string]any) map[string]any {
	result := map[string]any{}
	for _, source := range []map[string]any{frame, extra} {
		for key, value := range source {
			result[key] = value
		}
	}
	return result
}

// Encode serializes a frame as the line a native entry carries.
func Encode(frame map[string]any) string { return encode(frame) }

func ClaudeFrame(event map[string]any, uuid string) map[string]any {
	return map[string]any{
		"type": "stream_event", "event": event, "session_id": claudeSession, "parent_tool_use_id": nil, "uuid": uuid,
	}
}

func ClaudeDelta(index int, delta map[string]any, uuid string) map[string]any {
	return ClaudeFrame(map[string]any{"type": "content_block_delta", "index": index, "delta": delta}, uuid)
}

func ClaudeAssistant(messageID string, block map[string]any) map[string]any {
	return map[string]any{
		"type":               "assistant",
		"message":            map[string]any{"id": messageID, "type": "message", "role": "assistant", "content": []any{block}},
		"session_id":         claudeSession,
		"parent_tool_use_id": nil,
		"uuid":               "test-assistant-frame",
	}
}

type TextOptions struct {
	// Completed overrides what the completion holds.
	Completed *string
	// Interpose appends other entries before the completion.
	Interpose func(*Span)
}

// ClaudeText is a Claude text block streamed as chunks: cursors 1-2 start it, each chunk is a native
// frame and its derived delta, and the last two entries complete it.
func ClaudeText(chunks []string, options TextOptions) *Span {
	text := strings.Join(chunks, "")
	if options.Completed != nil {
		text = *options.Completed
	}
	span := &Span{}
	start := span.Native(ClaudeFrame(map[string]any{
		"type": "content_block_start", "index": 0, "content_block": map[string]any{"type": "text", "text": ""},
	}, "test-start"))
	span.Derived([]uint64{start}, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
		ItemId: "msg_test#0", Kind: eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT,
	}}})
	for number, chunk := range chunks {
		frame := span.Native(ClaudeDelta(0, map[string]any{"type": "text_delta", "text": chunk}, fmt.Sprintf("test-chunk-%d", number)))
		span.Derived([]uint64{frame}, &eventpb.Event{Observation: &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{
			ItemId: "msg_test#0", Text: chunk,
		}}})
	}
	if options.Interpose != nil {
		options.Interpose(span)
	}
	assistant := span.Native(ClaudeAssistant("msg_test", map[string]any{"type": "text", "text": text}))
	span.Derived([]uint64{assistant}, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
		ItemId: "msg_test#0", Outcome: &eventpb.ItemCompleted_Text{Text: text},
	}}})
	return span
}

// ClaudeToolArguments is a Claude tool call whose arguments stream as chunks of JSON.
func ClaudeToolArguments(chunks []string, toolInput map[string]any) *Span {
	span := &Span{}
	start := span.Native(ClaudeFrame(map[string]any{
		"type": "content_block_start", "index": 1,
		"content_block": map[string]any{"type": "tool_use", "id": "toolu_test", "name": "Bash", "input": map[string]any{}},
	}, "test-start"))
	span.Derived([]uint64{start}, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
		ItemId: "toolu_test", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "Bash",
	}}})
	for number, chunk := range chunks {
		frame := span.Native(ClaudeDelta(1, map[string]any{"type": "input_json_delta", "partial_json": chunk}, fmt.Sprintf("test-chunk-%d", number)))
		span.Derived([]uint64{frame}, &eventpb.Event{Observation: &eventpb.Event_ToolArgumentsDelta{
			ToolArgumentsDelta: &eventpb.ToolArgumentsDelta{ItemId: "toolu_test", PartialJson: chunk},
		}})
	}
	assistant := span.Native(ClaudeAssistant("msg_test", map[string]any{
		"type": "tool_use", "id": "toolu_test", "name": "Bash", "input": toolInput,
	}))
	span.Derived([]uint64{assistant}, &eventpb.Event{Observation: &eventpb.Event_ToolArguments{
		ToolArguments: &eventpb.ToolArguments{ItemId: "toolu_test", ArgumentsJson: encode(toolInput)},
	}})
	return span
}

func codexParams(itemID, delta string, extra map[string]any) map[string]any {
	return With(map[string]any{"threadId": codexThread, "turnId": codexTurn, "itemId": itemID, "delta": delta}, extra)
}

func codexFrame(method string, params map[string]any) map[string]any {
	return map[string]any{"method": method, "params": params, "emittedAtMs": 1_760_000_000_000}
}

func codexStarted(item map[string]any) map[string]any {
	return codexFrame("item/started", map[string]any{"item": item, "threadId": codexThread, "turnId": codexTurn, "startedAtMs": 1})
}

func codexCompleted(item map[string]any) map[string]any {
	return codexFrame("item/completed", map[string]any{
		"item": item, "threadId": codexThread, "turnId": codexTurn, "completedAtMs": 1_760_000_000_001,
	})
}

// CodexReasoning is a Codex reasoning item whose summary part i streams as the chunks parts[i].
func CodexReasoning(parts [][]string) *Span {
	span := &Span{}
	item := map[string]any{"type": "reasoning", "id": "rs_test", "summary": []any{}, "content": []any{}}
	start := span.Native(codexStarted(item))
	span.Derived([]uint64{start}, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
		ItemId: "rs_test", Kind: eventpb.ItemKind_ITEM_KIND_REASONING,
	}}})
	var summary []any
	var joined []string
	for index, chunks := range parts {
		for _, chunk := range chunks {
			frame := span.Native(codexFrame("item/reasoning/summaryTextDelta", codexParams("rs_test", chunk, map[string]any{"summaryIndex": index})))
			span.Derived([]uint64{frame}, &eventpb.Event{Observation: &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{
				ItemId: "rs_test", Text: chunk,
			}}})
		}
		summary, joined = append(summary, strings.Join(chunks, "")), append(joined, strings.Join(chunks, ""))
	}
	completed := span.Native(codexCompleted(With(item, map[string]any{"summary": summary})))
	span.Derived([]uint64{completed}, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
		ItemId: "rs_test", Outcome: &eventpb.ItemCompleted_Text{Text: strings.Join(joined, "\n")},
	}}})
	return span
}

// CodexCommand is a Codex command whose output streams as chunks; aggregated is the output its
// completed item holds, nil for none.
func CodexCommand(chunks []string, aggregated *string) *Span {
	span := &Span{}
	item := map[string]any{"type": "commandExecution", "id": "call_test", "command": "true", "status": "inProgress"}
	start := span.Native(codexStarted(item))
	span.Derived([]uint64{start}, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{
		ItemId: "call_test", Kind: eventpb.ItemKind_ITEM_KIND_TOOL_CALL, ToolName: "commandExecution",
	}}})
	for _, chunk := range chunks {
		frame := span.Native(codexFrame("item/commandExecution/outputDelta", codexParams("call_test", chunk, nil)))
		span.Derived([]uint64{frame}, &eventpb.Event{Observation: &eventpb.Event_ToolOutputDelta{ToolOutputDelta: &eventpb.ToolOutputDelta{
			ItemId: "call_test", Text: chunk,
		}}})
	}
	output := ""
	var aggregatedOutput any
	if aggregated != nil {
		output, aggregatedOutput = *aggregated, *aggregated
	}
	completed := span.Native(codexCompleted(With(item, map[string]any{
		"status": "completed", "aggregatedOutput": aggregatedOutput, "exitCode": 0,
	})))
	span.Derived([]uint64{completed}, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
		ItemId: "call_test", Outcome: &eventpb.ItemCompleted_Tool{Tool: &eventpb.ToolResult{Output: output, Succeeded: true}},
	}}})
	return span
}
