package residentruntime

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	runtimetesting "github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime/testing"

	residentsession "github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime/session"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"
	"github.com/agentydragon/ducktape/agentplane/testing/eventually"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	samplerpb "github.com/agentydragon/ducktape/agentplane/sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"google.golang.org/grpc"
)

func seedProviderCompactionCommand(t *testing.T, store *sessionstore.Store, session *sessionstore.Session, commandID string) {
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

func writeProviderCompactionSuccess(w http.ResponseWriter) error {
	w.Header().Set("Content-Type", "text/event-stream")
	item := responses.Item{Type: responses.ItemCompaction, ID: "compact-result", EncryptedContent: "opaque-compact-summary"}
	if err := writeSamplingEvent(w, map[string]any{"type": responses.EventOutputItemDone, "item": item}); err != nil {
		return err
	}
	return writeSamplingEvent(w, map[string]any{"type": responses.EventCompleted, "response": map[string]any{"id": "compact-response", "status": "completed"}})
}

func TestRuntimeCompactsWithLiveCellAndInFlightNestedTool(t *testing.T) {
	store, session, seed := runtimetesting.ClaimedSession(t, integrationDatabase)
	// The fake workspace is already ready. Every process start below is one of
	// the two delegated tool calls, so the first can be held across compaction.
	if err := store.PreparedWorkspace(t.Context(), seed, session.ID); err != nil {
		t.Fatal(err)
	}
	if err := store.Release(t.Context(), seed); err != nil {
		t.Fatal(err)
	}
	const commandID = "live-cell-compaction"
	runtimetesting.AdmitInput(t, store, session.ID, commandID, strings.Repeat("retain this task ", 64))

	nestedGate := make(chan struct{})
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	executor := grpc.NewServer()
	executorpb.RegisterExecutorServer(executor, &drainExecutorServer{startGate: nestedGate})
	go func() { _ = executor.Serve(listener) }()
	t.Cleanup(executor.Stop)

	requests := make(chan []byte, 4)
	compactionGate := make(chan struct{})
	handle := make(chan string, 1)
	var requestNumber atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read provider request: %v", err)
			return
		}
		number := requestNumber.Add(1)
		select {
		case requests <- body:
		case <-r.Context().Done():
			return
		}
		var responseErr error
		switch number {
		case 1:
			item := responses.Item{Type: responses.ItemCustomToolCall, ID: "exec-item", CallID: "exec-call", Name: "exec", Input: "text('start the work')"}
			responseErr = writeCompactionFixtureToolCall(w, item, 100)
		case 2:
			select {
			case <-compactionGate:
				responseErr = writeProviderCompactionSuccess(w)
			case <-r.Context().Done():
			}
		case 3:
			select {
			case cellID := <-handle:
				item := responses.Item{Type: responses.ItemFunctionCall, ID: "wait-item", CallID: "wait-call", Name: "wait", Arguments: fmt.Sprintf(`{"cell_id":%q}`, cellID)}
				responseErr = writeCompactionFixtureToolCall(w, item, 10)
			case <-r.Context().Done():
			}
		case 4:
			responseErr = writeSamplingSuccess(w, "finished-after-compact", "the original task is complete")
		default:
			t.Errorf("unexpected provider request %d", number)
		}
		if responseErr != nil {
			t.Errorf("write provider response: %v", responseErr)
		}
	}))
	t.Cleanup(server.Close)

	settings := providerSamplingSettings(server.URL, 1)
	model := settings.Models["fixture-model"]
	model.AutoCompactTokens = new(50)
	settings.Models["fixture-model"] = model
	settings.DirectExecutorTarget = listener.Addr().String()
	settings.DirectExecutorInsecureLocaltest = true
	manager, peers := newFakeCodeModeManager(t)
	runtime := New(store, settings, residentsession.NewCodeModeHost(manager.Manager, store, nil))
	cancel, done := startRuntimeForTest(t, runtime)
	t.Cleanup(func() { cancel(); waitRuntimeDone(t, done) })
	runtime.Wake(session.ID)

	_ = receiveProviderRequest(t, requests)
	eventually.Within(t, 3*time.Second, func() (bool, string) {
		return len(peers()) == 1, "expected the Code Mode host to open"
	})
	peer := peers()[0]
	execute := runtimetesting.ReceiveCodeOperationRequest(t, peer, "", "session/execute")
	sendNestedExecCommand(t, peer, execute.SessionID, 9001)
	eventually.Within(t, 3*time.Second, func() (bool, string) {
		count := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_intent
			WHERE conversation_id=$1 AND request->>'method'='process/start' AND result IS NULL`, session.ConversationID)
		return count == 1, "expected one unsettled process start from the running cell"
	})
	yieldCell(t, peer, execute.ID)

	compact := receiveProviderRequest(t, requests)
	if !bytes.Contains(compact, []byte(`"type":"compaction_trigger"`)) {
		t.Fatalf("usage above threshold did not trigger compaction: %s", compact)
	}
	if bytes.Contains(compact, []byte("Active execution handles: ")) {
		t.Fatalf("pre-compaction context contained an ordinary inventory: %s", compact)
	}
	close(nestedGate)
	receiveDelegateResponse(t, peer, 9001)
	// The yielded cell can begin and settle a second effect during compaction.
	sendNestedExecCommand(t, peer, execute.SessionID, 9002)
	receiveDelegateResponse(t, peer, 9002)
	if err := peer.Send(codemode.Frame{
		Type: codemode.DelegateRequest, ID: new(int64(9003)), SessionID: execute.SessionID,
		Request: &codemode.Delegate{Type: "notification/send", CallID: "exec-call", CellID: "cell-1", Text: "nested work completed during compaction"},
	}); err != nil {
		t.Fatal(err)
	}
	receiveDelegateResponse(t, peer, 9003)
	close(compactionGate)

	cellID := execute.SessionID + ":cell-1"
	continuation := receiveProviderRequest(t, requests)
	if !bytes.Contains(continuation, []byte("opaque-compact-summary")) ||
		bytes.Count(continuation, []byte("Active execution handles: ")) != 1 ||
		!bytes.Contains(continuation, []byte(cellID)) ||
		bytes.Count(continuation, []byte("nested work completed during compaction")) != 1 {
		t.Fatalf("first continuation lost the summary, live handle, or notification: %s", continuation)
	}
	handle <- cellID
	wait := runtimetesting.ReceiveCodeOperationRequest(t, peer, execute.SessionID, "session/wait")
	completeCell(t, peer, execute.SessionID, wait.ID)

	result := receiveProviderRequest(t, requests)
	input := func(wire []byte) []json.RawMessage {
		var request struct{ Input []json.RawMessage }
		if err := json.Unmarshal(wire, &request); err != nil {
			t.Fatal(err)
		}
		return request.Input
	}
	before, after := input(continuation), input(result)
	if len(after) != len(before)+2 || !slices.EqualFunc(before, after[:len(before)], func(a, b json.RawMessage) bool {
		return bytes.Equal(a, b)
	}) {
		t.Fatalf("wait changed the sampled replacement prefix or added records beyond its call and result: %s", result)
	}
	var output struct {
		Type   string
		CallID string `json:"call_id"`
		Output json.RawMessage
	}
	if err := json.Unmarshal(after[len(after)-1], &output); err != nil {
		t.Fatal(err)
	}
	if output.Type != "function_call_output" || output.CallID != "wait-call" ||
		!bytes.Contains(output.Output, []byte("Script completed")) || !bytes.Contains(output.Output, []byte("done")) {
		t.Fatalf("sampling after wait omitted the linked completion: %s", result)
	}
	waitProviderCommandOutcome(t, store, session.ID, commandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)
	if starts := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_intent
		WHERE conversation_id=$1 AND request->>'method'='process/start'`, session.ConversationID); starts != 2 {
		t.Fatalf("recorded process starts=%d, want one per delegate", starts)
	}
}

func writeCompactionFixtureToolCall(w http.ResponseWriter, item responses.Item, tokens int) error {
	w.Header().Set("Content-Type", "text/event-stream")
	for _, event := range []responses.Event{
		{Type: responses.EventOutputItemAdded, Item: &item},
		{Type: responses.EventOutputItemDone, Item: &item},
	} {
		if err := writeSamplingEvent(w, event); err != nil {
			return err
		}
	}
	return writeSamplingEvent(w, map[string]any{"type": responses.EventCompleted, "response": map[string]any{
		"id": item.ID + "-response", "status": "completed", "usage": map[string]int{"total_tokens": tokens},
	}})
}

type compactionAttempt struct {
	id, stepID, previousID, commandID, status string
	number, budget                            int
	resultPresent                             bool
}

func compactionAttempts(t *testing.T, store *sessionstore.Store, commandID string) []compactionAttempt {
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
	store := runtimetesting.NewStore(t, integrationDatabase)
	session := runtimetesting.OpenSessionForStore(t, store, "")
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
	t.Cleanup(server.Close)
	runtime, stop := startProviderSamplingRuntime(t, store, providerSamplingSettings(server.URL, 1))
	runtime.Wake(session.ID)

	firstBody := receiveProviderRequest(t, requests)
	firstIntentID := waitProviderAttemptStatus(t, store, sessionstore.IntentCompaction, commandID, 1, string(sessionstore.IntentResultAbandoned))
	setProviderRetryDueNow(t, store, firstIntentID)
	runtime.Wake(session.ID)
	secondBody := receiveProviderRequest(t, requests)
	if !bytes.Equal(firstBody, secondBody) {
		t.Fatalf("retry changed frozen compaction request body\nfirst:  %s\nsecond: %s", firstBody, secondBody)
	}
	waitProviderCommandOutcome(t, store, session.ID, commandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)

	attempts := compactionAttempts(t, store, commandID)
	if len(attempts) != 2 {
		t.Fatalf("compaction attempts=%+v, want exactly two", attempts)
	}
	first, second := attempts[0], attempts[1]
	if first.number != 1 || second.number != 2 || first.budget != 3 || second.budget != 3 ||
		first.commandID != commandID || second.commandID != commandID || first.stepID != first.id || second.stepID != first.stepID ||
		first.previousID != "" || second.previousID != first.id || first.status != string(sessionstore.IntentResultAbandoned) ||
		!first.resultPresent || second.status != "" || !second.resultPresent {
		t.Fatalf("compaction attempt lineage/results=%+v", attempts)
	}
	completed := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_intent
		WHERE provider_command_id=$1 AND request->>'kind'='compaction' AND result IS NOT NULL AND NOT (result ? 'status')`, commandID)
	if completed != 1 {
		t.Fatalf("persisted successful compaction results=%d, want one", completed)
	}
	conversationOwner := runtimeOwner(t, runtime, session.ID)
	conversation, err := store.Read(t.Context(), conversationOwner)
	if err != nil {
		t.Fatal(err)
	}
	items := testsupport.ResponsesItems(t, conversation.Context())
	compactions := 0
	for _, item := range items {
		if item.Type == responses.ItemCompaction && item.EncryptedContent == "opaque-compact-summary" {
			compactions++
		}
	}
	if compactions != 1 {
		t.Fatalf("active context contains %d copies of the successful summary: %+v", compactions, items)
	}
	stop()
}

func TestRuntimePermanentCompactionFailureLeavesNextInputUsable(t *testing.T) {
	store := runtimetesting.NewStore(t, integrationDatabase)
	session := runtimetesting.OpenSessionForStore(t, store, "")
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
		if err := writeSamplingSuccess(w, "assistant-response", "usable after compaction failure"); err != nil {
			t.Errorf("write provider response: %v", err)
		}
	}))
	t.Cleanup(server.Close)
	runtime, stop := startProviderSamplingRuntime(t, store, providerSamplingSettings(server.URL, 1))
	runtime.Wake(session.ID)
	_ = receiveProviderRequest(t, requests)
	waitProviderAttemptStatus(t, store, sessionstore.IntentCompaction, compactCommandID, 1, string(sessionstore.IntentResultAbandoned))
	failed := waitProviderCommandOutcome(t, store, session.ID, compactCommandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED)
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
	runtimetesting.AdmitInput(t, store, session.ID, inputCommandID, "usable after compaction failure")
	runtime.Wake(session.ID)
	inputBody := receiveProviderRequest(t, requests)
	if !strings.Contains(string(inputBody), "usable after compaction failure") {
		t.Fatalf("next provider request omitted the newly admitted input: %s", inputBody)
	}
	waitProviderCommandOutcome(t, store, session.ID, inputCommandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)
	sessionState, err = store.GetSession(t.Context(), session.ID, "")
	if err != nil || sessionState.Blocked {
		t.Fatalf("Session was not usable after next input: state=%+v err=%v", sessionState, err)
	}
	if got := count.Load(); got != 2 {
		t.Fatalf("provider requests=%d, want failed compaction plus one next-input sample", got)
	}
	stop()
}

func runtimeOwner(t *testing.T, runtime *Runtime, sessionID string) sessionstore.Owner {
	t.Helper()
	runtime.mu.Lock()
	defer runtime.mu.Unlock()
	task := runtime.tasks[sessionID]
	if task == nil || task.owner == nil {
		t.Fatal("runtime has no owner for active Session")
	}
	return *task.owner
}
