package centralsampler

import (
	"bytes"
	"database/sql"
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

func seedProviderCompactionCommand(t *testing.T, store *Store, session *Session, commandID string) {
	t.Helper()
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.AppendInput(t.Context(), owner, strings.Repeat("existing context that should be compacted ", 64)); err != nil {
		t.Fatal(err)
	}
	if err := store.Release(t.Context(), owner); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
		CommandId: commandID, Operation: &commandpb.Command_Compact{Compact: &commandpb.Compact{}},
	}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
}

func providerCompactionRuntime(t *testing.T, store *Store, server *httptest.Server) *Runtime {
	t.Helper()
	manager, _ := newFakeCodeModeManager(t)
	settings := samplersettings.Settings{
		MaxLiveSessions: 1, RPCTimeoutSeconds: 5, LeaseSeconds: 60, TurnTimeoutSeconds: 30,
		Connections: map[string]samplersettings.ConnectionConfig{
			"provider": {BaseURL: server.URL, Auth: samplersettings.AuthConfig{Type: samplersettings.AuthTypeBearer, Credential: "fixture-credential"}},
		},
		Models: map[string]samplersettings.ModelConfig{
			"fixture-model": {
				Connection: "provider", ProviderModel: "fixture-model", API: protocol.APIStyleResponses,
				Responses: responses.Config{Dialect: responses.DialectLite}, ExecEncoding: protocol.ExecEncodingFreeform,
			},
		},
	}
	return NewRuntime(store, settings, manager)
}

func receiveCompactionProviderRequest(t *testing.T, requests <-chan []byte) []byte {
	t.Helper()
	select {
	case body := <-requests:
		return body
	case <-time.After(10 * time.Second):
		t.Fatal("runtime did not dispatch the provider request")
		return nil
	}
}

func waitCompactionProviderAttemptStatus(t *testing.T, store *Store, commandID string, attemptNo int, want string) string {
	t.Helper()
	deadline := time.NewTimer(10 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		var id, status string
		err := store.DB().QueryRowContext(t.Context(), `SELECT id,COALESCE(result->>'status','') FROM sampler_intent
			WHERE provider_command_id=$1 AND provider_attempt_no=$2`, commandID, attemptNo).Scan(&id, &status)
		if err == nil && status == want {
			return id
		}
		if err != nil && err != sql.ErrNoRows {
			t.Fatal(err)
		}
		select {
		case <-deadline.C:
			t.Fatalf("provider attempt %d for command %q did not reach status %q (last status %q, err %v)", attemptNo, commandID, want, status, err)
		case <-ticker.C:
		}
	}
}

func waitCompactionCommandOutcome(t *testing.T, store *Store, sessionID, commandID string, want samplerpb.CommandOutcome) *samplerpb.GetCommandResponse {
	t.Helper()
	deadline := time.NewTimer(10 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		command, err := store.GetCommand(t.Context(), sessionID, commandID)
		if err == nil && command.GetOutcome() == want {
			return command
		}
		if err != nil {
			t.Fatal(err)
		}
		select {
		case <-deadline.C:
			t.Fatalf("command %q outcome=%v, want %v", commandID, command.GetOutcome(), want)
		case <-ticker.C:
		}
	}
}

func writeProviderEvent(w http.ResponseWriter, event any) error {
	wire, err := json.Marshal(event)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(w, "data: %s\n\n", wire)
	return err
}

func writeProviderCompactionSuccess(w http.ResponseWriter) error {
	w.Header().Set("Content-Type", "text/event-stream")
	item := responses.Item{Type: responses.ItemCompaction, ID: "compact-result", EncryptedContent: "opaque-compact-summary"}
	if err := writeProviderEvent(w, map[string]any{"type": responses.EventOutputItemDone, "item": item}); err != nil {
		return err
	}
	return writeProviderEvent(w, map[string]any{"type": responses.EventCompleted, "response": map[string]any{"id": "compact-response", "status": "completed"}})
}

func writeProviderAssistantSuccess(w http.ResponseWriter) error {
	w.Header().Set("Content-Type", "text/event-stream")
	item := responses.Item{
		Type: responses.ItemMessage, ID: "assistant-result", Role: "assistant",
		Content: json.RawMessage(`[{"type":"output_text","text":"usable after compaction failure"}]`),
	}
	if err := writeProviderEvent(w, map[string]any{"type": responses.EventOutputItemDone, "item": item}); err != nil {
		return err
	}
	return writeProviderEvent(w, map[string]any{"type": responses.EventCompleted, "response": map[string]any{"id": "assistant-response", "status": "completed"}})
}

type compactionAttempt struct {
	id, stepID, previousID, commandID, status string
	number, budget                            int
	resultPresent                             bool
}

func compactionAttempts(t *testing.T, store *Store, commandID string) []compactionAttempt {
	t.Helper()
	rows, err := store.DB().QueryContext(t.Context(), `SELECT id,provider_step_id::text,COALESCE(previous_provider_intent_id::text,''),
		COALESCE(provider_command_id,''),provider_attempt_no,provider_attempt_budget,COALESCE(result->>'status',''),result IS NOT NULL
		FROM sampler_intent WHERE provider_command_id=$1 AND request->>'kind'='compaction' ORDER BY provider_attempt_no`, commandID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var attempts []compactionAttempt
	for rows.Next() {
		var attempt compactionAttempt
		if err := rows.Scan(&attempt.id, &attempt.stepID, &attempt.previousID, &attempt.commandID,
			&attempt.number, &attempt.budget, &attempt.status, &attempt.resultPresent); err != nil {
			t.Fatal(err)
		}
		attempts = append(attempts, attempt)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return attempts
}

func TestRuntimeCompactionRetriesFrozenProviderRequestAndPersistsOnce(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	const commandID = "manual-compact-retry"
	seedProviderCompactionCommand(t, store, session, commandID)
	requests := make(chan []byte, 2)
	var count atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read provider request: %v", err)
			return
		}
		requests <- body
		if count.Add(1) == 1 {
			http.Error(w, "retry this read-only compaction", http.StatusServiceUnavailable)
			return
		}
		if err := writeProviderCompactionSuccess(w); err != nil {
			t.Errorf("write provider response: %v", err)
		}
	}))
	defer server.Close()
	runtime := providerCompactionRuntime(t, store, server)
	cancel, done := startRuntimeForTest(t, runtime)
	defer cancel()
	runtime.Wake(session.ID)

	firstBody := receiveCompactionProviderRequest(t, requests)
	firstIntentID := waitCompactionProviderAttemptStatus(t, store, commandID, 1, string(IntentResultAbandoned))
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_intent SET provider_retry_not_before=clock_timestamp()-interval '1 millisecond' WHERE id=$1`, firstIntentID); err != nil {
		t.Fatal(err)
	}
	runtime.Wake(session.ID)
	secondBody := receiveCompactionProviderRequest(t, requests)
	if !bytes.Equal(firstBody, secondBody) {
		t.Fatalf("retry changed frozen compaction request body\nfirst:  %s\nsecond: %s", firstBody, secondBody)
	}
	waitCompactionCommandOutcome(t, store, session.ID, commandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)

	attempts := compactionAttempts(t, store, commandID)
	if len(attempts) != 2 {
		t.Fatalf("compaction attempts=%+v, want exactly two", attempts)
	}
	first, second := attempts[0], attempts[1]
	if first.number != 1 || second.number != 2 || first.budget != 3 || second.budget != 3 ||
		first.commandID != commandID || second.commandID != commandID || first.stepID != first.id || second.stepID != first.stepID ||
		first.previousID != "" || second.previousID != first.id || first.status != string(IntentResultAbandoned) ||
		!first.resultPresent || second.status != "" || !second.resultPresent {
		t.Fatalf("compaction attempt lineage/results=%+v", attempts)
	}
	var completed int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_intent
		WHERE provider_command_id=$1 AND request->>'kind'='compaction' AND result IS NOT NULL AND NOT (result ? 'status')`, commandID).Scan(&completed); err != nil {
		t.Fatal(err)
	}
	if completed != 1 {
		t.Fatalf("persisted successful compaction results=%d, want one", completed)
	}
	conversationOwner := runtimeOwner(t, runtime, session.ID)
	conversation, err := store.Read(t.Context(), conversationOwner)
	if err != nil {
		t.Fatal(err)
	}
	items := responsesItems(t, conversation.Context())
	compactions := 0
	for _, item := range items {
		if item.Type == responses.ItemCompaction && item.EncryptedContent == "opaque-compact-summary" {
			compactions++
		}
	}
	if compactions != 1 {
		t.Fatalf("active context contains %d copies of the successful summary: %+v", compactions, items)
	}
	cancel()
	waitRuntimeDone(t, done)
}

func TestRuntimePermanentCompactionFailureLeavesNextInputUsable(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	const compactCommandID = "manual-compact-permanent-failure"
	seedProviderCompactionCommand(t, store, session, compactCommandID)
	requests := make(chan []byte, 2)
	var count atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read provider request: %v", err)
			return
		}
		requests <- body
		if count.Add(1) == 1 {
			http.Error(w, "invalid compaction route", http.StatusBadRequest)
			return
		}
		if err := writeProviderAssistantSuccess(w); err != nil {
			t.Errorf("write provider response: %v", err)
		}
	}))
	defer server.Close()
	runtime := providerCompactionRuntime(t, store, server)
	cancel, done := startRuntimeForTest(t, runtime)
	defer cancel()
	runtime.Wake(session.ID)
	_ = receiveCompactionProviderRequest(t, requests)
	waitCompactionProviderAttemptStatus(t, store, compactCommandID, 1, string(IntentResultAbandoned))
	failed := waitCompactionCommandOutcome(t, store, session.ID, compactCommandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED)
	if failed.GetFailureReason() == "" {
		t.Fatal("failed compaction did not retain a failure reason")
	}
	sessionState, err := store.GetSession(t.Context(), session.ID, "")
	if err != nil || sessionState.Blocked {
		t.Fatalf("permanent provider rejection blocked the Session: state=%+v err=%v", sessionState, err)
	}
	var retryAt sql.NullTime
	if err := store.DB().QueryRowContext(t.Context(), `SELECT provider_retry_not_before FROM sampler_intent WHERE provider_command_id=$1`, compactCommandID).Scan(&retryAt); err != nil {
		t.Fatal(err)
	}
	if retryAt.Valid {
		t.Fatalf("permanent compaction failure scheduled a retry at %v", retryAt.Time)
	}

	const inputCommandID = "input-after-permanent-compact-failure"
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
		CommandId: inputCommandID, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "usable after compaction failure"}},
	}, "fixture-account", nil); err != nil {
		t.Fatalf("admit input after failed compaction: %v", err)
	}
	runtime.Wake(session.ID)
	inputBody := receiveCompactionProviderRequest(t, requests)
	if !strings.Contains(string(inputBody), "usable after compaction failure") {
		t.Fatalf("next provider request omitted the newly admitted input: %s", inputBody)
	}
	waitCompactionCommandOutcome(t, store, session.ID, inputCommandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)
	sessionState, err = store.GetSession(t.Context(), session.ID, "")
	if err != nil || sessionState.Blocked {
		t.Fatalf("Session was not usable after next input: state=%+v err=%v", sessionState, err)
	}
	if got := count.Load(); got != 2 {
		t.Fatalf("provider requests=%d, want failed compaction plus one next-input sample", got)
	}
	cancel()
	waitRuntimeDone(t, done)
}

func runtimeOwner(t *testing.T, runtime *Runtime, sessionID string) Owner {
	t.Helper()
	runtime.mu.Lock()
	defer runtime.mu.Unlock()
	task := runtime.tasks[sessionID]
	if task == nil || task.owner == nil {
		t.Fatal("runtime has no owner for active Session")
	}
	return *task.owner
}
