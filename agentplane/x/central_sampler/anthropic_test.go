package centralsampler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/proto"
)

func anthropicTestConfig() ModelConfig {
	return ModelConfig{
		Connection:       "anthropic",
		ProviderModel:    "claude-test",
		API:              APIStyleAnthropicMessages,
		ExecEncoding:     ExecEncodingJSON,
		ReasoningEfforts: []string{"low", "high"},
		AnthropicMessages: &AnthropicMessagesConfig{
			MaxTokens: 4096,
			Thinking: &AnthropicThinkingConfig{
				Type:         "adaptive",
				BlockBinding: &AnthropicBlockBindingConfig{PrefixMismatchBehavior: "error"},
			},
			PromptCache:         &AnthropicPromptCacheConfig{TTL: "1h"},
			CacheDiagnostics:    true,
			PerMessageEffort:    true,
			StrictToolInput:     true,
			EagerInputStreaming: true,
		},
	}
}

func TestBuildAnthropicRequestConvertsImagesAndConfiguresCacheAndEffort(t *testing.T) {
	config := anthropicTestConfig()
	conversation := &AnthropicContext{}
	if err := conversation.ChangeEffort("low"); err != nil {
		t.Fatal(err)
	}
	image := json.RawMessage(`{"type":"input_image","image_url":"data:image/png;base64,aGVsbG8="}`)
	if err := conversation.AppendUser([]ContentPart{{Type: "input_text", Text: "inspect this"}, {Type: "input_image", Raw: image}}); err != nil {
		t.Fatal(err)
	}
	restoredEffort := "high"
	conversation.PendingEffort = &restoredEffort
	if err := conversation.AppendUser([]ContentPart{{Type: "input_text", Text: "next turn"}}); err != nil {
		t.Fatal(err)
	}
	body, err := BuildAnthropicRequest(AnthropicRequestParams{Context: conversation, Effort: "high"}, config, "msg_previous")
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	var wire map[string]any
	if err := json.Unmarshal(encoded, &wire); err != nil {
		t.Fatal(err)
	}
	if wire["model"] != "claude-test" || wire["max_tokens"] != float64(4096) || wire["stream"] != true {
		t.Fatalf("request identity/limits are wrong: %s", encoded)
	}
	thinking := wire["thinking"].(map[string]any)
	if thinking["type"] != "adaptive" || thinking["block_binding"].(map[string]any)["prefix_mismatch_behavior"] != "error" {
		t.Fatalf("thinking controls were not encoded: %s", encoded)
	}
	if wire["cache_control"].(map[string]any)["ttl"] != "1h" || wire["diagnostics"].(map[string]any)["previous_message_id"] != "msg_previous" {
		t.Fatalf("cache controls were not encoded: %s", encoded)
	}
	if wire["output_config"].(map[string]any)["effort"] != "high" {
		t.Fatalf("request effort was not encoded: %s", encoded)
	}
	tools := wire["tools"].([]any)
	tool := tools[0].(map[string]any)
	if tool["strict"] != true || tool["eager_input_streaming"] != true {
		t.Fatalf("tool input controls were not encoded: %s", encoded)
	}
	messages := wire["messages"].([]any)
	userMessage := messages[1].(map[string]any)
	content := userMessage["content"].([]any)
	imageBlock := content[1].(map[string]any)
	source := imageBlock["source"].(map[string]any)
	if imageBlock["type"] != "image" || source["type"] != "base64" || source["media_type"] != "image/png" || source["data"] != "aGVsbG8=" {
		t.Fatalf("input image was not converted to Anthropic base64 content: %s", encoded)
	}
	if messages[0].(map[string]any)["output_config"].(map[string]any)["effort"] != "low" || messages[3].(map[string]any)["output_config"].(map[string]any)["effort"] != "high" {
		t.Fatalf("per-message effort controls were not positioned around the user turn: %s", encoded)
	}
	if messages[2].(map[string]any)["role"] != "user" {
		t.Fatalf("the restored effort activated before the new user input: %s", encoded)
	}
}

func TestAnthropicHistoryObservationsUseNativeBlockIdentity(t *testing.T) {
	message := map[string]json.RawMessage{"id": json.RawMessage(`"msg_history"`)}
	decodeBlock := func(raw string) map[string]json.RawMessage {
		t.Helper()
		var block map[string]json.RawMessage
		if err := json.Unmarshal([]byte(raw), &block); err != nil {
			t.Fatal(err)
		}
		return block
	}

	text := decodeBlock(`{"type":"text","text":"hello"}`)
	started := anthropicItemStarted(message, 0, text).GetItemStarted()
	if started == nil || started.GetItemId() != "msg_history#0" || started.GetKind() != eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT {
		t.Fatalf("text block start was not normalized: %+v", started)
	}
	if delta := anthropicBlockDelta(message, 0, text, "text_delta", "hello").GetTextDelta(); delta == nil || delta.GetItemId() != "msg_history#0" || delta.GetText() != "hello" {
		t.Fatalf("text delta was not normalized: %+v", delta)
	}
	if completed := anthropicBlockCompleted(message, 0, text).GetItemCompleted(); completed == nil || completed.GetText() != "hello" {
		t.Fatalf("text block completion was not normalized: %+v", completed)
	}

	thinking := decodeBlock(`{"type":"thinking","thinking":"reasoning","signature":"sig_private"}`)
	started = anthropicItemStarted(message, 1, thinking).GetItemStarted()
	if started == nil || started.GetItemId() != "msg_history#1" || started.GetKind() != eventpb.ItemKind_ITEM_KIND_REASONING {
		t.Fatalf("thinking block start was not normalized: %+v", started)
	}
	if delta := anthropicBlockDelta(message, 1, thinking, "thinking_delta", "reasoning").GetTextDelta(); delta == nil || delta.GetText() != "reasoning" {
		t.Fatalf("thinking delta was not normalized: %+v", delta)
	}
	if anthropicBlockDelta(message, 1, thinking, "signature_delta", "sig_private") != nil {
		t.Fatal("signature delta must remain raw source evidence")
	}
	if anthropicItemStarted(message, 2, decodeBlock(`{"type":"redacted_thinking","data":"opaque"}`)) != nil {
		t.Fatal("redacted thinking must not be projected as plaintext")
	}

	tool := decodeBlock(`{"type":"tool_use","id":"call_native","name":"exec","input":{"code":"text(1)"}}`)
	started = anthropicItemStarted(message, 2, tool).GetItemStarted()
	if started == nil || started.GetItemId() != "call_native" || started.GetKind() != eventpb.ItemKind_ITEM_KIND_TOOL_CALL || started.GetToolName() != "exec" {
		t.Fatalf("tool block start did not use native call identity: %+v", started)
	}
	if delta := anthropicBlockDelta(message, 2, tool, "input_json_delta", `{"code":`).GetToolArgumentsDelta(); delta == nil || delta.GetItemId() != "call_native" || delta.GetPartialJson() != `{"code":` {
		t.Fatalf("tool input delta was not normalized: %+v", delta)
	}
	if args := anthropicToolArguments(message, 2, tool).GetToolArguments(); args == nil || args.GetItemId() != "call_native" || args.GetArgumentsJson() != `{"code":"text(1)"}` {
		t.Fatalf("tool arguments were not normalized: %+v", args)
	}
	if anthropicItemStarted(message, 3, decodeBlock(`{"type":"compaction","content":"opaque","signature":"sig"}`)) != nil {
		t.Fatal("compaction block must remain protocol evidence")
	}
}

func TestAnthropicStreamPersistsNativeHistoryObservationsWithSourceLinks(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	intentID, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling"})
	if err != nil {
		t.Fatal(err)
	}
	stream := anthropicEventStream(
		`{"type":"message_start","message":{"id":"msg_history","role":"assistant","content":[],"usage":{"input_tokens":1}}}`,
		`{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}`,
		`{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}`,
		`{"type":"content_block_stop","index":0}`,
		`{"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":1}}`,
		`{"type":"message_stop"}`,
	)
	response, err := readAnthropicStream(t.Context(), strings.NewReader(stream), &ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}, false)
	if err != nil {
		t.Fatal(err)
	}
	if response == nil || len(response.Message) == 0 {
		t.Fatal("Anthropic response was not reconstructed")
	}

	rows, err := store.DB().QueryContext(t.Context(), `SELECT entry FROM sampler_event WHERE session_id=$1 ORDER BY sequence`, session.ID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	entries := map[uint64]*eventlogpb.EventEntry{}
	var observed []*eventpb.Event
	for rows.Next() {
		var wire []byte
		if err := rows.Scan(&wire); err != nil {
			t.Fatal(err)
		}
		entry := &eventlogpb.EventEntry{}
		if err := proto.Unmarshal(wire, entry); err != nil {
			t.Fatal(err)
		}
		entries[entry.GetOrigin().GetSequence()] = entry
		event := entry.GetEvent()
		if event.GetItemStarted() != nil || event.GetTextDelta() != nil || event.GetToolArgumentsDelta() != nil || event.GetToolArguments() != nil || event.GetItemCompleted() != nil {
			observed = append(observed, event)
		}
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}

	want := []struct {
		kind        string
		observation func(*eventpb.Event) bool
	}{
		{"content_block_start", func(event *eventpb.Event) bool {
			started := event.GetItemStarted()
			return started != nil && started.GetItemId() == "msg_history#0" && started.GetKind() == eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT
		}},
		{"content_block_delta", func(event *eventpb.Event) bool {
			delta := event.GetTextDelta()
			return delta != nil && delta.GetItemId() == "msg_history#0" && delta.GetText() == "Hello"
		}},
		{"content_block_stop", func(event *eventpb.Event) bool {
			completed := event.GetItemCompleted()
			return completed != nil && completed.GetItemId() == "msg_history#0" && completed.GetText() == "Hello"
		}},
	}
	if len(observed) != len(want) {
		t.Fatalf("got %d native observations, want %d: %+v", len(observed), len(want), observed)
	}
	for index, expectation := range want {
		event := observed[index]
		if !expectation.observation(event) {
			t.Fatalf("observation %d was not normalized as expected: %+v", index, event)
		}
		if len(event.GetSourceSequences()) != 1 {
			t.Fatalf("observation %d has no single raw source link: %+v", index, event)
		}
		source := entries[event.GetSourceSequences()[0]]
		if source == nil {
			t.Fatalf("observation %d references missing source sequence %d", index, event.GetSourceSequences()[0])
		}
		boundary := source.GetEvent().GetBoundaryRecord()
		if boundary == nil || boundary.GetBoundary() != "provider_anthropic" {
			t.Fatalf("observation %d source is not a native Anthropic boundary: %+v", index, source)
		}
		var payload struct {
			WireEvent string `json:"wire_event"`
		}
		if err := json.Unmarshal(boundary.GetPayload(), &payload); err != nil {
			t.Fatal(err)
		}
		var raw struct {
			Type string `json:"type"`
		}
		if err := json.Unmarshal([]byte(payload.WireEvent), &raw); err != nil || raw.Type != expectation.kind {
			t.Fatalf("observation %d points to %q, want %q (err=%v)", index, raw.Type, expectation.kind, err)
		}
	}
}

func TestAnthropicInvalidBlockStartIsPersistedWithoutHistoryObservation(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	intentID, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling"})
	if err != nil {
		t.Fatal(err)
	}
	invalidStart := `{"type":"content_block_start","index":0,"content_block":{"type":"text"}}`
	stream := anthropicEventStream(
		`{"type":"message_start","message":{"id":"msg_invalid","role":"assistant","content":[],"usage":{"input_tokens":1}}}`,
		invalidStart,
	)
	response, err := readAnthropicStream(t.Context(), strings.NewReader(stream), &ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}, false)
	var known *KnownFailureError
	if response != nil || !errors.As(err, &known) {
		t.Fatalf("invalid block start outcome=%+v err=%v; want known rejection", response, err)
	}

	rows, err := store.DB().QueryContext(t.Context(), `SELECT entry FROM sampler_event WHERE session_id=$1 ORDER BY sequence`, session.ID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	rawPersisted, observed := false, false
	for rows.Next() {
		var wire []byte
		if err := rows.Scan(&wire); err != nil {
			t.Fatal(err)
		}
		entry := &eventlogpb.EventEntry{}
		if err := proto.Unmarshal(wire, entry); err != nil {
			t.Fatal(err)
		}
		event := entry.GetEvent()
		if boundary := event.GetBoundaryRecord(); boundary != nil {
			var payload struct {
				WireEvent string `json:"wire_event"`
			}
			if err := json.Unmarshal(boundary.GetPayload(), &payload); err != nil {
				t.Fatal(err)
			}
			rawPersisted = rawPersisted || payload.WireEvent == invalidStart
		}
		observed = observed || event.GetItemStarted() != nil || event.GetTextDelta() != nil || event.GetItemCompleted() != nil
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if !rawPersisted || observed {
		t.Fatalf("invalid native event should remain raw evidence only: rawPersisted=%v observed=%v", rawPersisted, observed)
	}
}

func TestBuildAnthropicRequestAcceptsStoredTextMessage(t *testing.T) {
	conversation := &AnthropicContext{}
	if err := conversation.AppendUser([]ContentPart{{Type: "input_text", Text: "stored conversation text"}}); err != nil {
		t.Fatal(err)
	}
	body, err := BuildAnthropicRequest(AnthropicRequestParams{Context: conversation}, anthropicTestConfig(), "")
	if err != nil {
		t.Fatal(err)
	}
	message := body["messages"].([]json.RawMessage)[0]
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(message, &fields); err != nil {
		t.Fatal(err)
	}
	var content []map[string]any
	if err := json.Unmarshal(fields["content"], &content); err != nil {
		t.Fatal(err)
	}
	text := content[0]["text"]
	if text != "stored conversation text" {
		t.Fatalf("stored text was not converted to Anthropic input_text: %+v", message)
	}
}

func TestBuildAnthropicRequestReplaysSignedThinkingBlockUnchanged(t *testing.T) {
	content := json.RawMessage(`{"type":"thinking","thinking":"preserve this","signature":"sig_fixture"}`)
	native := json.RawMessage(`{"id":"msg_signed","type":"message","role":"assistant","model":"claude-test","content":[` + string(content) + `],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":1,"output_tokens":7,"provider_usage":{"fixture":true}},"provider_metadata":{"opaque_sequence":9007199254740993}}`)
	conversation := &AnthropicContext{}
	if err := conversation.AppendRecords([]json.RawMessage{native}); err != nil {
		t.Fatal(err)
	}
	body, err := BuildAnthropicRequest(AnthropicRequestParams{Context: conversation}, anthropicTestConfig(), "msg_signed")
	if err != nil {
		t.Fatal(err)
	}
	replayedMessage := body["messages"].([]json.RawMessage)[0]
	var replayFields map[string]json.RawMessage
	if err := json.Unmarshal(replayedMessage, &replayFields); err != nil {
		t.Fatal(err)
	}
	want, err := json.Marshal([]json.RawMessage{content})
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(replayFields["content"], want) {
		t.Fatalf("signed thinking content changed during replay: got %s want %s", replayFields["content"], want)
	}
	if len(replayFields) != 2 || !bytes.Contains(replayedMessage, []byte(`"role":"assistant"`)) {
		t.Fatalf("response envelope metadata leaked onto the request wire: %s", replayedMessage)
	}
	stored, err := conversation.Records()
	if err != nil || len(stored) != 1 || !bytes.Contains(stored[0], []byte(`"provider_metadata":{"opaque_sequence":9007199254740993}`)) ||
		!bytes.Contains(stored[0], []byte(`"provider_usage":{"fixture":true}`)) {
		t.Fatalf("unknown native response metadata was not retained in storage: records=%s err=%v", mustJSON(stored), err)
	}
	if body["diagnostics"].(map[string]any)["previous_message_id"] != "msg_signed" {
		t.Fatalf("previous message diagnostics were lost: %+v", body["diagnostics"])
	}
}

func TestAnthropicContextUsesNativeRecordsForToolCallsAndResults(t *testing.T) {
	context := &AnthropicContext{}
	assistant := json.RawMessage(`{"id":"msg_tools","type":"message","role":"assistant","content":[{"type":"text","text":"running"},{"type":"tool_use","id":"call_1","name":"exec","input":{"code":"text(1)"},"vendor_field":"kept"}],"stop_reason":"tool_use","usage":{"input_tokens":2,"output_tokens":3},"vendor_metadata":{"fixture":true}}`)
	if err := context.AppendRecords([]json.RawMessage{assistant}); err != nil {
		t.Fatal(err)
	}
	calls, err := context.PendingCalls()
	if err != nil {
		t.Fatal(err)
	}
	if len(calls) != 1 || calls[0].Encoding != ExecEncodingJSON || calls[0].CallID != "call_1" || calls[0].Name != "exec" ||
		calls[0].Input != `{"code":"text(1)"}` || calls[0].SourceRecord != 1 {
		t.Fatalf("native tool_use was not the execution source: %+v", calls)
	}
	records, err := context.Records()
	if err != nil || len(records) != 1 {
		t.Fatalf("tool call created a duplicate canonical record: records=%d err=%v", len(records), err)
	}
	if err := context.AppendToolResult(calls[0], []ContentPart{{Type: "input_text", Text: "done"}}); err != nil {
		t.Fatal(err)
	}
	if err := context.CheckSettled(); err != nil {
		t.Fatalf("tool result did not settle the call: %v", err)
	}
	if err := context.AppendToolResult(calls[0], []ContentPart{{Type: "input_text", Text: "later"}}); err != nil {
		t.Fatal(err)
	}
	records, err = context.Records()
	if err != nil || len(records) != 3 {
		t.Fatalf("native result/notification records=%d err=%v", len(records), err)
	}
	var firstResult, repeatedOutput struct {
		Role    string `json:"role"`
		Content []struct {
			Type    string `json:"type"`
			ToolUse string `json:"tool_use_id"`
			Text    string `json:"text"`
			Content any    `json:"content"`
		} `json:"content"`
	}
	if err := json.Unmarshal(records[1], &firstResult); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(records[2], &repeatedOutput); err != nil {
		t.Fatal(err)
	}
	if firstResult.Role != "user" || len(firstResult.Content) != 1 || firstResult.Content[0].Type != "tool_result" || firstResult.Content[0].ToolUse != "call_1" {
		t.Fatalf("first output was not a native tool_result: %+v", firstResult)
	}
	if repeatedOutput.Role != "user" || len(repeatedOutput.Content) != 1 || repeatedOutput.Content[0].Type != "text" ||
		!strings.HasPrefix(repeatedOutput.Content[0].Text, "Asynchronous output from exec call call_1:\n") {
		t.Fatalf("repeated output was not native user text: %+v", repeatedOutput)
	}
}

func TestAnthropicPendingEffortRestoresAfterNewUserAndPreservesSignedPrefix(t *testing.T) {
	thinking := json.RawMessage(`{"type":"thinking","thinking":"keep this signed prefix","signature":"sig_fixture","unknown_block_field":{"keep":true}}`)
	message := json.RawMessage(`{"id":"msg_signed","type":"message","role":"assistant","content":[` + string(thinking) + `],"stop_reason":"end_turn","usage":{"input_tokens":4,"output_tokens":5,"unknown_usage":17},"provider_unknown":{"keep":"yes"}}`)
	effort := "high"
	context := &AnthropicContext{Messages: []json.RawMessage{message}, PendingEffort: &effort}
	wire, err := json.Marshal(context)
	if err != nil {
		t.Fatal(err)
	}
	var restored AnthropicContext
	if err := json.Unmarshal(wire, &restored); err != nil {
		t.Fatal(err)
	}
	if restored.PendingEffort == nil || *restored.PendingEffort != "high" {
		t.Fatalf("pending effort was not persisted: %s", wire)
	}
	if err := restored.AppendUser([]ContentPart{{Type: "input_text", Text: "new turn"}}); err != nil {
		t.Fatal(err)
	}
	if restored.PendingEffort != nil || len(restored.Messages) != 3 {
		t.Fatalf("restoration did not activate after the user message: %+v", restored)
	}
	var prefix map[string]json.RawMessage
	if err := json.Unmarshal(restored.Messages[0], &prefix); err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(prefix["content"], []byte(`"signature":"sig_fixture"`)) ||
		!bytes.Contains(prefix["content"], []byte(`"unknown_block_field":{"keep":true}`)) ||
		!bytes.Contains(restored.Messages[0], []byte(`"unknown_usage":17`)) ||
		!bytes.Contains(restored.Messages[0], []byte(`"provider_unknown":{"keep":"yes"}`)) {
		t.Fatalf("signed prefix or provider metadata changed: %s", restored.Messages[0])
	}
	var control struct {
		Role         string `json:"role"`
		Content      []any  `json:"content"`
		OutputConfig struct {
			Effort string `json:"effort"`
		} `json:"output_config"`
	}
	if err := json.Unmarshal(restored.Messages[2], &control); err != nil {
		t.Fatal(err)
	}
	if control.Role != "system" || len(control.Content) != 0 || control.OutputConfig.Effort != "high" {
		t.Fatalf("restored effort control has wrong native shape: %+v", control)
	}
}

func TestAnthropicContextRejectsUnsupportedAndDuplicateNativeCalls(t *testing.T) {
	if err := (&AnthropicContext{}).AppendRecords([]json.RawMessage{json.RawMessage(`{"role":"developer","content":[]}`)}); err == nil {
		t.Fatal("unsupported native message role was accepted")
	}
	duplicate := json.RawMessage(`{"role":"assistant","content":[{"type":"tool_use","id":"same","name":"exec","input":{}},{"type":"tool_use","id":"same","name":"exec","input":{}}],"stop_reason":"tool_use"}`)
	context := &AnthropicContext{}
	if err := context.AppendRecords([]json.RawMessage{duplicate}); err != nil {
		t.Fatal(err)
	}
	if _, err := context.PendingCalls(); err == nil {
		t.Fatal("duplicate native tool call IDs were accepted")
	}
	unknown := &AnthropicContext{}
	if err := unknown.AppendRecords([]json.RawMessage{json.RawMessage(`{"role":"user","content":[{"type":"tool_result","tool_use_id":"missing","content":"x"}]}`)}); err != nil {
		t.Fatal(err)
	}
	if _, err := unknown.PendingCalls(); err == nil {
		t.Fatal("orphan native tool result was accepted")
	}
	if err := json.Unmarshal([]byte(`{"messages":[],"unknown_context_field":true}`), &AnthropicContext{}); err == nil {
		t.Fatal("unknown Anthropic context state was accepted")
	}
}

func TestAnthropicExecutionStateAppendsOnlyChangedSnapshots(t *testing.T) {
	context := &AnthropicContext{}
	state := &ExecutionState{Cells: []LiveCell{{ID: "cell-1", Status: "running"}}}
	for range 2 {
		if err := context.AppendExecutionState(state); err != nil {
			t.Fatal(err)
		}
	}
	if len(context.Messages) != 1 {
		t.Fatalf("identical execution snapshots appended %d messages", len(context.Messages))
	}
	state.Cells[0].Status = "completed"
	if err := context.AppendExecutionState(state); err != nil {
		t.Fatal(err)
	}
	state.Cells = nil
	if err := context.AppendExecutionState(state); err != nil {
		t.Fatal(err)
	}
	if len(context.Messages) != 3 {
		t.Fatalf("changed and cleared execution snapshots produced %d messages, want 3", len(context.Messages))
	}
}

func TestAnthropicFragmentedEagerToolInputIsNotPublishedBeforeTerminalMessage(t *testing.T) {
	partial := anthropicEventStream(
		`{"type":"message_start","message":{"id":"msg_partial","role":"assistant","content":[],"usage":{"input_tokens":1}}}`,
		`{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call_partial","name":"exec","input":{}}}`,
		`{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"code\":\"text(1)\""}}`,
	)
	response, err := readAnthropicStream(context.Background(), strings.NewReader(partial), nil, false)
	var unknown *UnknownEffectError
	if response != nil || !errors.As(err, &unknown) {
		t.Fatalf("partial eager input became executable or lost its uncertain outcome: response=%+v err=%v", response, err)
	}

	complete := anthropicEventStream(
		`{"type":"message_start","message":{"id":"msg_complete","role":"assistant","content":[],"usage":{"input_tokens":1}}}`,
		`{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"call_complete","name":"exec","input":{}}}`,
		`{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"code\":"}}`,
		`{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\"text(1)\"}"}}`,
		`{"type":"content_block_stop","index":0}`,
		`{"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":1}}`,
		`{"type":"message_stop"}`,
	)
	response, err = readAnthropicStream(context.Background(), strings.NewReader(complete), nil, false)
	if err != nil {
		t.Fatal(err)
	}
	context := &AnthropicContext{}
	if err := context.AppendRecords([]json.RawMessage{response.Message}); err != nil {
		t.Fatal(err)
	}
	calls, err := context.PendingCalls()
	if err != nil || len(calls) != 1 || calls[0].CallID != "call_complete" || calls[0].Input != `{"code":"text(1)"}` {
		t.Fatalf("complete fragmented input was not derived from the native message: calls=%+v err=%v", calls, err)
	}
}

func TestAnthropicTruncatedStreamHasUnknownEffect(t *testing.T) {
	stream := anthropicEventStream(`{"type":"message_start","message":{"id":"msg_truncated","role":"assistant","content":[],"usage":{"input_tokens":1}}}`)
	response, err := readAnthropicStream(context.Background(), strings.NewReader(stream), nil, false)
	var unknown *UnknownEffectError
	if response != nil || !errors.As(err, &unknown) {
		t.Fatalf("truncated response outcome=%+v err=%v; want unknown effect", response, err)
	}
}

func TestAnthropicRejectsUnsupportedOutputBlocks(t *testing.T) {
	stream := anthropicEventStream(
		`{"type":"message_start","message":{"id":"msg_unknown","role":"assistant","content":[],"usage":{"input_tokens":1}}}`,
		`{"type":"content_block_start","index":0,"content_block":{"type":"server_tool_use","id":"tool_unknown"}}`,
	)
	response, err := readAnthropicStream(context.Background(), strings.NewReader(stream), nil, false)
	var known *KnownFailureError
	if response != nil || !errors.As(err, &known) {
		t.Fatalf("unsupported output block outcome=%+v err=%v; want a known rejection", response, err)
	}
}

func TestAnthropicCompactionRequiresContentAndSignature(t *testing.T) {
	for name, block := range map[string]string{
		"missing content":   `{"type":"compaction","signature":"sig_fixture"}`,
		"missing signature": `{"type":"compaction","content":"summary"}`,
	} {
		t.Run(name, func(t *testing.T) {
			stream := anthropicEventStream(
				`{"type":"message_start","message":{"id":"msg_compaction","role":"assistant","content":[],"usage":{"input_tokens":1}}}`,
				fmt.Sprintf(`{"type":"content_block_start","index":0,"content_block":%s}`, block),
				`{"type":"content_block_stop","index":0}`,
				`{"type":"message_delta","delta":{"stop_reason":"compaction"},"usage":{"output_tokens":1}}`,
				`{"type":"message_stop"}`,
			)
			response, err := readAnthropicStream(context.Background(), strings.NewReader(stream), nil, true)
			var known *KnownFailureError
			if response != nil || !errors.As(err, &known) {
				t.Fatalf("invalid compaction outcome=%+v err=%v; want known rejection", response, err)
			}
		})
	}
}

func anthropicEventStream(events ...string) string {
	var stream strings.Builder
	for _, event := range events {
		fmt.Fprintf(&stream, "data: %s\n\n", event)
	}
	return stream.String()
}

func TestAnthropicRequestLoggingFailurePreventsDispatch(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.AppendInput(t.Context(), owner, "must not dispatch without the request journal"); err != nil {
		t.Fatal(err)
	}
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		requests.Add(1)
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()
	config := anthropicTestConfig()
	settings := fixtureSettings()
	settings.Models = map[string]ModelConfig{session.Model: config}
	settings.Connections = map[string]ConnectionConfig{"anthropic": {BaseURL: server.URL, Auth: AuthConfig{Type: AuthTypeAnthropicAPIKey, Credential: "fixture-key"}}}
	if _, err := store.DB().ExecContext(t.Context(), `ALTER TABLE sampler_intent ADD CONSTRAINT reject_test_intent CHECK (false)`); err != nil {
		t.Fatal(err)
	}
	runtime := NewRuntime(store, settings)
	if _, err := runtime.advance(t.Context(), owner, nil, server.Client(), session); err == nil {
		t.Fatal("request-journal failure was ignored")
	}
	if requests.Load() != 0 {
		t.Fatal("provider was called before request evidence committed")
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if conversation.Pending != nil {
		t.Fatal("failed intent insert left a pending remote effect")
	}
}

func TestAnthropicEvidenceFailureAfterPrefixLeavesIntentUnresolved(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	intent, err := store.Begin(t.Context(), owner, map[string]any{"kind": "sampling"})
	if err != nil {
		t.Fatal(err)
	}
	resume := make(chan struct{})
	var release sync.Once
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, anthropicEventStream(`{"type":"message_start","message":{"id":"msg_evidence_failure","role":"assistant","content":[],"usage":{"input_tokens":1}}}`))
		w.(http.Flusher).Flush()
		select {
		case <-resume:
		case <-r.Context().Done():
			return
		}
		_, _ = io.WriteString(w, anthropicEventStream(`{"type":"content_block_start","index":0,"content_block":{"type":"text","text":"uncommitted"}}`))
		w.(http.Flusher).Flush()
	}))
	defer server.Close()
	defer release.Do(func() { close(resume) })
	finished := make(chan error, 1)
	go func() {
		response, err := SampleAnthropic(t.Context(), server.Client(), ConnectionConfig{BaseURL: server.URL, Auth: AuthConfig{Type: AuthTypeAnthropicAPIKey, Credential: "fixture-key"}}, anthropicTestConfig(), []byte(`{}`), &ResponseEvidence{Store: store, Owner: owner, IntentID: intent}, false)
		if response != nil {
			finished <- errors.New("response completed after evidence failure")
			return
		}
		finished <- err
	}()
	// Wait for a native event, rather than merely the HTTP/transport metadata, to be durable.
	deadline := time.Now().Add(5 * time.Second)
	observed := false
	for !observed && time.Now().Before(deadline) {
		rows, err := store.DB().QueryContext(t.Context(), `SELECT entry FROM sampler_event WHERE session_id=$1 ORDER BY sequence`, session.ID)
		if err != nil {
			t.Fatal(err)
		}
		for rows.Next() {
			var wire []byte
			if err := rows.Scan(&wire); err != nil {
				t.Fatal(err)
			}
			entry := &eventlogpb.EventEntry{}
			if err := proto.Unmarshal(wire, entry); err != nil {
				t.Fatal(err)
			}
			if entry.GetEvent().GetBoundaryRecord().GetBoundary() == "provider_anthropic" {
				observed = true
			}
		}
		if err := rows.Err(); err != nil {
			t.Fatal(err)
		}
		rows.Close()
		if !observed {
			time.Sleep(10 * time.Millisecond)
		}
	}
	if !observed {
		t.Fatal("native prefix was not committed")
	}
	var cursor uint64
	if err := store.DB().QueryRowContext(t.Context(), `SELECT MAX(sequence) FROM sampler_event WHERE session_id=$1`, session.ID).Scan(&cursor); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), fmt.Sprintf(`ALTER TABLE sampler_event ADD CONSTRAINT reject_test_event CHECK (sequence <= %d)`, cursor)); err != nil {
		t.Fatal(err)
	}
	release.Do(func() { close(resume) })
	select {
	case err := <-finished:
		if err == nil {
			t.Fatal("provider evidence failure was ignored")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("provider did not stop after evidence failure")
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if conversation.Pending == nil || conversation.Pending.ID != intent {
		t.Fatal("dispatched request lost its unresolved intent")
	}
	var settled bool
	if err := store.DB().QueryRowContext(t.Context(), `SELECT result IS NOT NULL FROM sampler_intent WHERE id=$1`, intent).Scan(&settled); err != nil {
		t.Fatal(err)
	}
	if settled {
		t.Fatal("evidence failure settled an unobserved result")
	}
}
