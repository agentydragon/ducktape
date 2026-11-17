package residentruntime

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	residentsession "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/residentruntime/session"
	runtimetesting "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/residentruntime/testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
)

type providerTurnRequest struct {
	initialLease time.Time
}

type providerTurnExpiryDiagnostics struct {
	RequestCount           int32                                `json:"request_count"`
	CompletedResponseCount int32                                `json:"completed_response_count"`
	TurnExpiry             []residentsession.TurnExpiryEvidence `json:"turn_expiry"`
	Session                *providerTurnSessionState            `json:"session,omitempty"`
	CommandOutcome         int32                                `json:"command_outcome,omitempty"`
	CommandFailureReason   string                               `json:"command_failure_reason,omitempty"`
	ProviderAttempts       []providerTurnAttemptState           `json:"provider_attempts,omitempty"`
	CaptureErrors          []string                             `json:"capture_errors,omitempty"`
}

type providerTurnSessionState struct {
	ConversationID string `json:"conversation_id"`
	ActiveCommand  string `json:"active_command,omitempty"`
	ActiveTurn     string `json:"active_turn,omitempty"`
	Blocked        bool   `json:"blocked"`
	BlockedReason  string `json:"blocked_reason,omitempty"`
}

type providerTurnAttemptState struct {
	CommandID      string `json:"command_id"`
	Attempt        int    `json:"attempt"`
	ResultStatus   string `json:"result_status"`
	FailureClass   string `json:"failure_class,omitempty"`
	RetryScheduled bool   `json:"retry_scheduled"`
}

func writeProviderTurnExpiryDiagnostics(t *testing.T, evidence <-chan residentsession.TurnExpiryEvidence, store *sessionstore.Store,
	sessionID string, requests, completedResponses *atomic.Int32,
) {
	t.Helper()
	if !t.Failed() {
		return
	}
	snapshot := providerTurnExpiryDiagnostics{
		RequestCount: requests.Load(), CompletedResponseCount: completedResponses.Load(),
	}
	for {
		select {
		case item := <-evidence:
			snapshot.TurnExpiry = append(snapshot.TurnExpiry, item)
		default:
			goto evidenceCollected
		}
	}

evidenceCollected:
	ctx := context.Background()
	if state, err := store.GetSession(ctx, sessionID, ""); err != nil {
		snapshot.CaptureErrors = append(snapshot.CaptureErrors, "read Session: "+err.Error())
	} else {
		snapshot.Session = &providerTurnSessionState{
			ConversationID: state.ConversationID, ActiveCommand: pointerText(state.ActiveCommand),
			ActiveTurn: pointerText(state.ActiveTurn), Blocked: state.Blocked, BlockedReason: pointerText(state.BlockedReason),
		}
	}
	if command, err := store.GetCommand(ctx, sessionID, "after-limited-backoff"); err != nil {
		snapshot.CaptureErrors = append(snapshot.CaptureErrors, "read command: "+err.Error())
	} else {
		snapshot.CommandOutcome = int32(command.GetOutcome())
		snapshot.CommandFailureReason = command.GetFailureReason()
	}
	if snapshot.Session != nil {
		rows, err := store.DB().QueryContext(ctx, `SELECT provider_command_id,provider_attempt_no,COALESCE(result->>'status',''),
			COALESCE(result->>'failure_class',''),provider_retry_not_before IS NOT NULL
			FROM sampler_intent WHERE conversation_id=$1 AND request->>'kind'='sampling' ORDER BY id`, snapshot.Session.ConversationID)
		if err != nil {
			snapshot.CaptureErrors = append(snapshot.CaptureErrors, "read provider attempts: "+err.Error())
		} else {
			for rows.Next() {
				var attempt providerTurnAttemptState
				if err := rows.Scan(&attempt.CommandID, &attempt.Attempt, &attempt.ResultStatus, &attempt.FailureClass, &attempt.RetryScheduled); err != nil {
					snapshot.CaptureErrors = append(snapshot.CaptureErrors, "scan provider attempt: "+err.Error())
					break
				}
				snapshot.ProviderAttempts = append(snapshot.ProviderAttempts, attempt)
			}
			if err := rows.Err(); err != nil {
				snapshot.CaptureErrors = append(snapshot.CaptureErrors, "iterate provider attempts: "+err.Error())
			}
			_ = rows.Close()
		}
	}
	outputDirectory := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if outputDirectory == "" {
		outputDirectory = t.TempDir()
	}
	if err := os.MkdirAll(outputDirectory, 0o750); err != nil {
		t.Errorf("create provider expiry diagnostics directory: %v", err)
		return
	}
	wire, err := json.MarshalIndent(snapshot, "", "  ")
	if err != nil {
		t.Errorf("marshal provider expiry diagnostics: %v", err)
		return
	}
	path := filepath.Join(outputDirectory, "provider-turn-expiry.json")
	if err := os.WriteFile(path, wire, 0o640); err != nil {
		t.Errorf("write provider expiry diagnostics: %v", err)
		return
	}
	t.Logf("wrote provider expiry diagnostics to %s", path)
}

func pointerText(value *string) string {
	if value == nil {
		return ""
	}
	return *value
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

func TestRuntimeRenewsProviderAttemptAcrossLongSemanticProgress(t *testing.T) {
	t.Run("unlimited", func(t *testing.T) { checkRuntimeTurnProgress(t, nil) })
	t.Run("configured limit", func(t *testing.T) {
		seconds := 1
		checkRuntimeTurnProgress(t, &seconds)
	})
}

func checkRuntimeTurnProgress(t *testing.T, turnTimeoutSeconds *int) {
	t.Helper()
	store := runtimetesting.NewStore(t, integrationDatabase)
	session := runtimetesting.OpenSessionForStore(t, store, "")
	requestObserved := make(chan providerTurnRequest, 1)
	lastDeltaSent := make(chan time.Time, 1)
	streamDuration := make(chan time.Duration, 1)
	streamStopped := make(chan struct{})
	var requestCount atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if requestCount.Add(1) != 1 {
			t.Errorf("runtime dispatched more than one provider request")
			return
		}
		defer close(streamStopped)
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
		TurnTimeoutSeconds:                   turnTimeoutSeconds,
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
	runtime := New(store, settings, fakeCodeHost(t))
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
		CommandId: "long-provider-turn",
		Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "keep making semantic progress"}},
	}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
	cancel, done := startRuntimeForTest(t, runtime)
	defer func() { cancel(); waitRuntimeDone(t, done) }()
	runtime.Wake(session.ID)

	var initial providerTurnRequest
	select {
	case initial = <-requestObserved:
	case <-time.After(10 * time.Second):
		t.Fatal("runtime did not start its provider request")
	}
	want := samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED
	if turnTimeoutSeconds != nil {
		want = samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED
	}
	waitProviderCommandOutcome(t, store, session.ID, "long-provider-turn", want)
	if turnTimeoutSeconds != nil {
		select {
		case <-streamStopped:
		case <-time.After(5 * time.Second):
			t.Fatal("turn expiry did not cancel the progressing provider stream")
		}
		var status, failureClass string
		if err := store.DB().QueryRowContext(t.Context(), `SELECT result->>'status',result->>'failure_class' FROM sampler_intent
			WHERE provider_command_id=$1`, "long-provider-turn").Scan(&status, &failureClass); err != nil {
			t.Fatal(err)
		}
		if status != string(sessionstore.IntentResultAbandoned) || failureClass != string(sessionstore.ProviderAttemptPermanent) {
			t.Fatalf("expired attempt: status=%s failure_class=%s", status, failureClass)
		}
		state, err := store.GetSession(t.Context(), session.ID, "")
		if err != nil || state.Blocked || requestCount.Load() != 1 {
			t.Fatalf("expired provider turn: state=%+v requests=%d err=%v", state, requestCount.Load(), err)
		}
		return
	}

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
}

func TestRuntimeTurnLimitIncludesProviderBackoffAndAllowsNextInput(t *testing.T) {
	store := runtimetesting.NewStore(t, integrationDatabase)
	session := runtimetesting.OpenSessionForStore(t, store, "")
	var requests atomic.Int32
	var completedResponses atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, err := io.Copy(io.Discard, r.Body); err != nil {
			t.Error(err)
			return
		}
		if requests.Add(1) == 1 {
			w.Header().Set("Retry-After", "30")
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		flusher := w.(http.Flusher)
		item := responses.Item{Type: responses.ItemMessage, ID: "after-turn-limit", Role: "assistant", Content: json.RawMessage(`[{"type":"output_text","text":"continued"}]`)}
		if err := writeProviderTurnEvent(w, flusher, map[string]any{"type": responses.EventOutputItemDone, "item": item}); err != nil {
			t.Error(err)
			return
		}
		if err := writeProviderTurnEvent(w, flusher, map[string]any{
			"type":     responses.EventCompleted,
			"response": map[string]any{"id": "after-limit-response", "status": "completed"},
		}); err != nil {
			t.Error(err)
			return
		}
		completedResponses.Add(1)
	}))
	defer server.Close()
	settings := providerSamplingSettings(server.URL, 1)
	seconds := 1
	settings.TurnTimeoutSeconds = &seconds
	manager, _ := newFakeCodeModeManager(t)
	runtime := New(store, settings, manager)
	turnExpiry := make(chan residentsession.TurnExpiryEvidence, 8)
	runtime.turnExpiryObserver = func(evidence residentsession.TurnExpiryEvidence) {
		turnExpiry <- evidence
	}
	cancelRuntime, done := startRuntimeForTest(t, runtime)
	defer func() {
		cancelRuntime()
		waitRuntimeDone(t, done)
	}()
	defer writeProviderTurnExpiryDiagnostics(t, turnExpiry, store, session.ID, &requests, &completedResponses)
	admitProviderSamplingCommand(t, store, session.ID, "limited-backoff", "start")
	runtime.Wake(session.ID)
	waitProviderCommandOutcome(t, store, session.ID, "limited-backoff", samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED)
	var attempts, due int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT COUNT(*),COUNT(provider_retry_not_before) FROM sampler_intent
		WHERE provider_command_id=$1`, "limited-backoff").Scan(&attempts, &due); err != nil {
		t.Fatal(err)
	}
	if attempts != 1 || due != 0 || requests.Load() != 1 {
		t.Fatalf("turn limit retried during backoff: attempts=%d due=%d requests=%d", attempts, due, requests.Load())
	}
	admitProviderSamplingCommand(t, store, session.ID, "after-limited-backoff", "continue")
	runtime.Wake(session.ID)
	waitProviderCommandOutcome(t, store, session.ID, "after-limited-backoff", samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)
	state, err := store.GetSession(t.Context(), session.ID, "")
	if err != nil || state.Blocked || requests.Load() != 2 {
		t.Fatalf("next input after turn expiry: state=%+v requests=%d err=%v", state, requests.Load(), err)
	}
}
