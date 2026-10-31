package centralsampler

import (
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
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

func anthropicTestConfig() ModelConfig {
	return ModelConfig{
		Connection: "anthropic", ProviderModel: "claude-test", API: protocol.APIStyleAnthropicMessages,
		ExecEncoding: protocol.ExecEncodingJSON, ReasoningEfforts: []string{"low", "high"},
		AnthropicMessages: &anthropic.Config{
			MaxTokens:   4096,
			Thinking:    &anthropic.ThinkingConfig{Type: "adaptive", BlockBinding: &anthropic.BlockBindingConfig{PrefixMismatchBehavior: "error"}},
			PromptCache: &anthropic.PromptCacheConfig{TTL: "1h"}, CacheDiagnostics: true, PerMessageEffort: true,
			StrictToolInput: true, EagerInputStreaming: true,
		},
	}
}

func TestAnthropicStreamPersistsNativeHistoryObservationsWithSourceLinks(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	intentID, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentSampling})
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
	response, err := anthropic.ReadStream(t.Context(), strings.NewReader(stream), &ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}, false)
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

func TestAnthropicRequestLoggingFailurePreventsDispatch(t *testing.T) {
	store := newTestStore(t)
	setup := ""
	request := &samplerpb.OpenSessionRequest{SessionId: uuid.NewString(), Spec: &runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CENTRAL_SAMPLER, Cwd: "/fixture", Model: "fixture-model"}, SetupScript: &setup}
	session, err := store.OpenSession(t.Context(), request, "fixture-account", protocol.APIStyleAnthropicMessages)
	if err != nil {
		t.Fatal(err)
	}
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
	_, dispatchErr := runtime.advance(t.Context(), owner, nil, server.Client(), session)
	var constraint *pq.Error
	if !errors.As(dispatchErr, &constraint) || constraint.Code != "23514" || constraint.Constraint != "reject_test_intent" {
		t.Fatalf("expected the injected intent-journal constraint failure, got %v", dispatchErr)
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
	intent, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentSampling})
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
		response, err := anthropic.Sample(t.Context(), server.Client(), server.URL, "fixture-key", *anthropicTestConfig().AnthropicMessages, []byte(`{}`), &ResponseEvidence{Store: store, Owner: owner, IntentID: intent}, false)
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

func TestAnthropicInvalidBlockStartIsPersistedWithoutHistoryObservation(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	intentID, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentSampling})
	if err != nil {
		t.Fatal(err)
	}
	invalidStart := `{"type":"content_block_start","index":0,"content_block":{"type":"text"}}`
	stream := anthropicEventStream(
		`{"type":"message_start","message":{"id":"msg_invalid","role":"assistant","content":[],"usage":{"input_tokens":1}}}`,
		invalidStart,
	)
	response, err := anthropic.ReadStream(t.Context(), strings.NewReader(stream), &ResponseEvidence{Store: store, Owner: owner, IntentID: intentID}, false)
	var known *protocol.KnownFailureError
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

func anthropicEventStream(events ...string) string {
	var stream strings.Builder
	for _, event := range events {
		fmt.Fprintf(&stream, "data: %s\n\n", event)
	}
	return stream.String()
}
