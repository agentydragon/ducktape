package centralsampler

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/settings"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
)

type providerTurnRequest struct {
	initialLease time.Time
}

func writeProviderTurnEvent(w http.ResponseWriter, flusher http.Flusher, event any) error {
	wire, err := json.Marshal(event)
	if err != nil {
		return err
	}
	if _, err := fmt.Fprintf(w, "data: %s\n\n", wire); err != nil {
		return err
	}
	flusher.Flush()
	return nil
}

func providerTurnProgressResponse(w http.ResponseWriter, r *http.Request, lastDeltaSent chan<- time.Time, streamDuration chan<- time.Duration) error {
	flusher, ok := w.(http.Flusher)
	if !ok {
		return fmt.Errorf("test HTTP writer %T does not support streaming flush", w)
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	itemID := "long-progress-message"
	item := responses.Item{Type: responses.ItemMessage, ID: itemID, Role: "assistant"}
	startedAt := time.Now()
	if err := writeProviderTurnEvent(w, flusher, map[string]any{"type": responses.EventOutputItemAdded, "item": item}); err != nil {
		return err
	}
	const deltaCount = 8
	for index := 0; index < deltaCount; index++ {
		if index > 0 {
			timer := time.NewTimer(350 * time.Millisecond)
			select {
			case <-timer.C:
			case <-r.Context().Done():
				timer.Stop()
				return r.Context().Err()
			}
		}
		if err := writeProviderTurnEvent(w, flusher, map[string]any{
			"type": "response.output_text.delta", "item_id": itemID, "delta": "progress ",
		}); err != nil {
			return err
		}
	}
	lastDeltaSent <- time.Now()
	streamDuration <- time.Since(startedAt)
	item.Content = json.RawMessage(`[{"type":"output_text","text":"progress progress progress progress progress progress progress progress "}]`)
	if err := writeProviderTurnEvent(w, flusher, map[string]any{"type": responses.EventOutputItemDone, "item": item}); err != nil {
		return err
	}
	if err := writeProviderTurnEvent(w, flusher, map[string]any{
		"type":     responses.EventCompleted,
		"response": map[string]any{"id": "long-progress-response", "status": "completed"},
	}); err != nil {
		return err
	}
	return nil
}

func waitProviderTurnCommandOutcome(t *testing.T, store *Store, sessionID, commandID string, want samplerpb.CommandOutcome) *samplerpb.GetCommandResponse {
	t.Helper()
	deadline := time.NewTimer(10 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		command, err := store.GetCommand(t.Context(), sessionID, commandID)
		if err != nil {
			t.Fatal(err)
		}
		if command.GetOutcome() == want {
			return command
		}
		select {
		case <-deadline.C:
			t.Fatalf("command %q outcome=%v, want %v", commandID, command.GetOutcome(), want)
		case <-ticker.C:
		}
	}
}

func TestRuntimeRenewsProviderAttemptAcrossLongSemanticProgress(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	requestObserved := make(chan providerTurnRequest, 1)
	lastDeltaSent := make(chan time.Time, 1)
	streamDuration := make(chan time.Duration, 1)
	var requestCount atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if requestCount.Add(1) != 1 {
			t.Errorf("runtime dispatched more than one provider request")
			return
		}
		if _, err := io.Copy(io.Discard, r.Body); err != nil {
			t.Errorf("read provider request body: %v", err)
			return
		}
		var initialLease time.Time
		if err := store.DB().QueryRowContext(r.Context(), `SELECT provider_attempt_lease_until FROM sampler_intent
			WHERE provider_command_id=$1 AND request->>'kind'='sampling' AND result IS NULL`, "long-provider-turn").Scan(&initialLease); err != nil {
			t.Errorf("read initial provider attempt lease: %v", err)
			return
		}
		requestObserved <- providerTurnRequest{initialLease: initialLease}
		if err := providerTurnProgressResponse(w, r, lastDeltaSent, streamDuration); err != nil && r.Context().Err() == nil {
			t.Errorf("write provider stream: %v", err)
		}
	}))
	defer server.Close()

	headerSeconds, idleSeconds := 1, 1
	settings := samplersettings.Settings{
		MaxLiveSessions: 1, RPCTimeoutSeconds: 5, LeaseSeconds: 60,
		ProviderResponseHeaderTimeoutSeconds: &headerSeconds,
		ProviderIdleTimeoutSeconds:           &idleSeconds,
		Connections: map[string]samplersettings.ConnectionConfig{
			"provider": {BaseURL: server.URL, Auth: samplersettings.AuthConfig{Type: samplersettings.AuthTypeBearer, Credential: "fixture-credential"}},
		},
		Models: map[string]samplersettings.ModelConfig{
			"fixture-model": {
				Connection: "provider", ProviderModel: "fixture-model", API: protocol.APIStyleResponses,
				Responses: responses.Config{Dialect: responses.DialectLite}, ExecEncoding: protocol.ExecEncodingFreeform,
				ReasoningEfforts: []string{"", "high"},
			},
		},
	}
	runtime := NewRuntime(store, settings, fakeCodeHost(t))
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
		CommandId: "long-provider-turn",
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "keep making semantic progress"}},
	}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
	cancel, done := startRuntimeForTest(t, runtime)
	defer cancel()
	runtime.Wake(session.ID)

	var initial providerTurnRequest
	select {
	case initial = <-requestObserved:
	case <-time.After(10 * time.Second):
		t.Fatal("runtime did not start its provider request")
	}
	waitProviderTurnCommandOutcome(t, store, session.ID, "long-provider-turn", samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)

	var last time.Time
	select {
	case last = <-lastDeltaSent:
	case <-time.After(10 * time.Second):
		t.Fatal("provider stream did not complete its semantic deltas")
	}
	var streamed time.Duration
	select {
	case streamed = <-streamDuration:
	case <-time.After(10 * time.Second):
		t.Fatal("provider server did not report its stream duration")
	}
	if streamed <= 2*time.Second {
		t.Fatalf("semantic stream duration=%s, want more than two one-second idle windows", streamed)
	}
	if got := requestCount.Load(); got != 1 {
		t.Fatalf("provider requests=%d, want one", got)
	}
	var finalLease time.Time
	var leaseLive bool
	if err := store.DB().QueryRowContext(t.Context(), `SELECT provider_attempt_lease_until,provider_attempt_lease_until>clock_timestamp()
		FROM sampler_intent WHERE provider_command_id=$1 AND request->>'kind'='sampling'`, "long-provider-turn").Scan(&finalLease, &leaseLive); err != nil {
		t.Fatal(err)
	}
	// The initial lease includes the header phase (17s total here). A semantic renewal near
	// the end of this 2.45s stream resets it to idle + two RPC budgets (11s), moving the
	// deadline at least two seconds earlier than the initial deadline while still keeping it live.
	if !finalLease.Before(initial.initialLease.Add(-2*time.Second)) || !leaseLive {
		t.Fatalf("provider attempt lease was not renewed by semantic progress: initial=%s final=%s last_delta=%s live=%v", initial.initialLease, finalLease, last, leaseLive)
	}

	events, err := store.Events(t.Context(), session.ID, 0, 4096)
	if err != nil {
		t.Fatal(err)
	}
	deltas := 0
	for _, entry := range events {
		if delta := entry.GetEvent().GetTextDelta(); delta != nil && delta.GetItemId() == "long-progress-message" && delta.GetText() != "" {
			deltas++
		}
	}
	if deltas < 8 {
		t.Fatalf("durable semantic deltas=%d, want all 8", deltas)
	}
	sessionState, err := store.GetSession(t.Context(), session.ID, "")
	if err != nil || sessionState.Blocked {
		t.Fatalf("long-progress turn left Session blocked: state=%+v err=%v", sessionState, err)
	}
	cancel()
	waitRuntimeDone(t, done)
}

func TestRemovedTurnTimeoutAndIndependentSetupTimeoutSettings(t *testing.T) {
	settings := fixtureSettings()
	if got, want := settings.SetupTimeout(), time.Hour; got != want {
		t.Fatalf("default setup timeout=%s, want %s", got, want)
	}
	configuredSetupTimeout := 17
	settings.SetupTimeoutSeconds = &configuredSetupTimeout
	if err := settings.Validate(); err != nil {
		t.Fatal(err)
	}
	if got, want := settings.SetupTimeout(), 17*time.Second; got != want {
		t.Fatalf("configured setup timeout=%s, want %s", got, want)
	}

	encoded, err := json.Marshal(fixtureSettings())
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &fields); err != nil {
		t.Fatal(err)
	}
	fields["turn_timeout_seconds"] = json.RawMessage("30")
	wire, err := json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := samplersettings.DecodeSettings(strings.NewReader(string(wire))); err == nil || !strings.Contains(err.Error(), `unknown field "turn_timeout_seconds"`) {
		t.Fatalf("removed turn_timeout_seconds was not rejected by strict settings decode: %v", err)
	}
}
