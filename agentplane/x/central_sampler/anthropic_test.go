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
	items := []ResponsesItem{
		{Type: ResponsesItemEffort, Input: "low"},
		{
			Type:    ResponsesItemMessage,
			Role:    "user",
			Content: json.RawMessage(`[{"type":"input_text","text":"inspect this"},{"type":"input_image","image_url":"data:image/png;base64,aGVsbG8="}]`),
		},
		{Type: ResponsesItemEffortRestore, Input: "high"},
	}
	body, err := BuildAnthropicRequest(ResponsesRequestParams{Effort: "high", Items: items}, config, "msg_previous")
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
	image := content[1].(map[string]any)
	source := image["source"].(map[string]any)
	if image["type"] != "image" || source["type"] != "base64" || source["media_type"] != "image/png" || source["data"] != "aGVsbG8=" {
		t.Fatalf("input image was not converted to Anthropic base64 content: %s", encoded)
	}
	if messages[0].(map[string]any)["output_config"].(map[string]any)["effort"] != "low" || messages[2].(map[string]any)["output_config"].(map[string]any)["effort"] != "high" {
		t.Fatalf("per-message effort controls were not positioned around the user turn: %s", encoded)
	}
}

func TestBuildAnthropicRequestAcceptsStoredTextMessage(t *testing.T) {
	item, err := newResponsesMessage("user", "stored conversation text")
	if err != nil {
		t.Fatal(err)
	}
	body, err := BuildAnthropicRequest(ResponsesRequestParams{Items: []ResponsesItem{item}}, anthropicTestConfig(), "")
	if err != nil {
		t.Fatal(err)
	}
	messages := body["messages"].([]any)
	content := messages[0].(map[string]any)["content"].([]any)
	text := content[0].(map[string]any)["text"]
	if text != "stored conversation text" {
		t.Fatalf("stored text was not converted to Anthropic input_text: %+v", messages[0])
	}
}

func TestBuildAnthropicRequestReplaysSignedThinkingBlockUnchanged(t *testing.T) {
	content := json.RawMessage(`{"type":"thinking","thinking":"preserve this","signature":"sig_fixture"}`)
	native, err := json.Marshal(AnthropicMessage{
		ID: "msg_signed", Role: "assistant", Content: []json.RawMessage{content}, StopReason: "end_turn",
		Usage: map[string]json.RawMessage{"input_tokens": json.RawMessage(`1`)},
	})
	if err != nil {
		t.Fatal(err)
	}
	body, err := BuildAnthropicRequest(ResponsesRequestParams{
		Items: []ResponsesItem{{Type: ResponsesItemAnthropicMessage, Content: native}},
	}, anthropicTestConfig(), "msg_signed")
	if err != nil {
		t.Fatal(err)
	}
	messages := body["messages"].([]any)
	replayed, err := json.Marshal(messages[0].(map[string]any)["content"])
	if err != nil {
		t.Fatal(err)
	}
	want, err := json.Marshal([]json.RawMessage{content})
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(replayed, want) {
		t.Fatalf("signed thinking content changed during replay: got %s want %s", replayed, want)
	}
	if body["diagnostics"].(map[string]any)["previous_message_id"] != "msg_signed" {
		t.Fatalf("previous message diagnostics were lost: %+v", body["diagnostics"])
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
	if len(response.Output) != 2 || response.Output[1].Type != ResponsesItemFunctionCall || response.Output[1].Arguments != `{"code":"text(1)"}` {
		t.Fatalf("complete fragmented input was not published at message_stop: %+v", response)
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
