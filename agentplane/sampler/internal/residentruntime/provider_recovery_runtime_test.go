package residentruntime

import (
	"bytes"
	"database/sql"
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

	runtimetesting "github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime/testing"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	samplerpb "github.com/agentydragon/ducktape/agentplane/sampler/api/samplerpb"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"github.com/agentydragon/ducktape/agentplane/testing/eventually"
)

func providerSamplingSettings(baseURL string, maxLiveSessions int) samplersettings.Settings {
	return samplersettings.Settings{
		MaxLiveSessions: maxLiveSessions, RPCTimeoutSeconds: 5, LeaseSeconds: 60,
		Connections: map[string]samplersettings.ConnectionConfig{
			"provider": {BaseURL: baseURL, Auth: samplersettings.AuthConfig{Type: samplersettings.AuthTypeBearer, Credential: "fixture-credential"}},
		},
		Models: map[string]samplersettings.ModelConfig{
			"fixture-model": {
				Connection: "provider", ProviderModel: "fixture-model", API: protocol.APIStyleResponses,
				Responses: responses.Config{Dialect: responses.DialectLite}, ExecEncoding: protocol.ExecEncodingFreeform,
			},
		},
	}
}

func setProviderRetryDueNow(t *testing.T, store *sessionstore.Store, intentID string) {
	t.Helper()
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_intent SET provider_retry_not_before=clock_timestamp()-interval '1 millisecond' WHERE id=$1`, intentID); err != nil {
		t.Fatal(err)
	}
}

func startProviderSamplingRuntime(t *testing.T, store *sessionstore.Store, settings samplersettings.Settings) (*Runtime, func()) {
	t.Helper()
	manager, _ := newFakeCodeModeManager(t)
	runtime := New(store, settings, manager)
	cancel, done := startRuntimeForTest(t, runtime)
	var once sync.Once
	stop := func() {
		once.Do(func() {
			cancel()
			waitRuntimeDone(t, done)
		})
	}
	t.Cleanup(stop)
	return runtime, stop
}

func waitProviderAttemptStatus(t *testing.T, store *sessionstore.Store, kind sessionstore.IntentKind, commandID string, attemptNo int, want string) string {
	t.Helper()
	var id string
	eventually.Within(t, 10*time.Second, func() (bool, string) {
		var status string
		err := store.DB().QueryRowContext(t.Context(), `SELECT id,COALESCE(result->>'status','') FROM sampler_intent
			WHERE provider_command_id=$1 AND provider_attempt_no=$2 AND request->>'kind'=$3`, commandID, attemptNo, string(kind)).Scan(&id, &status)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			t.Fatal(err)
		}
		return err == nil && status == want, fmt.Sprintf("%s attempt %d for command %q did not reach status %q (last status %q, err %v)", kind, attemptNo, commandID, want, status, err)
	})
	return id
}

func waitProviderCommandOutcome(t *testing.T, store *sessionstore.Store, sessionID, commandID string, want samplerpb.CommandOutcome) *samplerpb.GetCommandResponse {
	t.Helper()
	var command *samplerpb.GetCommandResponse
	eventually.Within(t, 10*time.Second, func() (bool, string) {
		var err error
		if command, err = store.GetCommand(t.Context(), sessionID, commandID); err != nil {
			t.Fatal(err)
		}
		return command.GetOutcome() == want, fmt.Sprintf("command %q outcome=%v, want %v", commandID, command.GetOutcome(), want)
	})
	return command
}

func receiveProviderRequest(t *testing.T, requests <-chan []byte) []byte {
	t.Helper()
	select {
	case body := <-requests:
		return body
	case <-time.After(10 * time.Second):
		t.Fatal("runtime did not dispatch the provider request")
		return nil
	}
}

func writeSamplingEvent(w http.ResponseWriter, event any) error {
	wire, err := json.Marshal(event)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(w, "data: %s\n\n", wire)
	return err
}

func writeSamplingSuccess(w http.ResponseWriter, responseID, text string) error {
	w.Header().Set("Content-Type", "text/event-stream")
	item := responses.Item{
		Type: responses.ItemMessage, ID: responseID + "-item", Role: "assistant",
		Content: json.RawMessage(fmt.Sprintf(`[{"type":"output_text","text":%q}]`, text)),
	}
	if err := writeSamplingEvent(w, map[string]any{"type": responses.EventOutputItemDone, "item": item}); err != nil {
		return err
	}
	return writeSamplingEvent(w, map[string]any{"type": responses.EventCompleted, "response": map[string]any{"id": responseID, "status": "completed"}})
}

func countUserMessage(t *testing.T, state protocol.Context, text string) int {
	t.Helper()
	count := 0
	for _, item := range testsupport.ResponsesItems(t, state) {
		if item.Type != responses.ItemMessage || item.Role != "user" {
			continue
		}
		var got string
		if err := json.Unmarshal(item.Content, &got); err != nil {
			t.Fatal(err)
		}
		if got == text {
			count++
		}
	}
	return count
}

func waitProviderTaskReleased(t *testing.T, runtime *Runtime, sessionID string) {
	t.Helper()
	eventually.Within(t, 10*time.Second, func() (bool, string) {
		runtime.mu.Lock()
		task := runtime.tasks[sessionID]
		waiting, busy := task != nil && task.waitingProvider, task != nil && task.busy
		runtime.mu.Unlock()
		permits := len(runtime.executing)
		return waiting && !busy && permits == 0, fmt.Sprintf("provider wait did not release execution: waiting=%v busy=%v permits=%d", waiting, busy, permits)
	})
}

func TestRuntimeSamplingRetriesFrozenBodyAfter429AndPartialStream(t *testing.T) {
	store := runtimetesting.NewStore(t, integrationDatabase)
	session := runtimetesting.OpenSessionForStore(t, store, "")
	const commandID = "sampling-retry-after-partial-stream"
	const input = "original sampling input must appear once"
	runtimetesting.AdmitInput(t, store, session.ID, commandID, input)

	requests := make(chan []byte, 3)
	var count atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read provider request: %v", err)
			return
		}
		requests <- body
		switch count.Add(1) {
		case 1:
			w.Header().Set("Retry-After", "30")
			http.Error(w, "temporarily rate limited", http.StatusTooManyRequests)
		case 2:
			w.Header().Set("Content-Type", "text/event-stream")
			partialCall := responses.Item{
				Type: responses.ItemFunctionCall, ID: "partial-call-item", CallID: "partial-provider-call",
				Name: "exec", Arguments: `{"cmd":"must not be dispatched"}`,
			}
			if err := writeSamplingEvent(w, map[string]any{"type": responses.EventOutputItemDone, "item": partialCall}); err != nil {
				t.Errorf("write partial provider item: %v", err)
				return
			}
			w.(http.Flusher).Flush()
		default:
			if err := writeSamplingSuccess(w, "sampling-retry-response", "sampling recovered"); err != nil {
				t.Errorf("write provider success: %v", err)
			}
		}
	}))
	t.Cleanup(server.Close)
	settings := providerSamplingSettings(server.URL, 1)
	const attemptBudget = 5
	settings.ProviderAttemptBudget = new(attemptBudget)
	runtime, stop := startProviderSamplingRuntime(t, store, settings)
	runtime.Wake(session.ID)

	firstBody := receiveProviderRequest(t, requests)
	firstID := waitProviderAttemptStatus(t, store, sessionstore.IntentSampling, commandID, 1, string(sessionstore.IntentResultAbandoned))
	setProviderRetryDueNow(t, store, firstID)
	runtime.Wake(session.ID)
	secondBody := receiveProviderRequest(t, requests)
	if !bytes.Equal(firstBody, secondBody) {
		t.Fatalf("retry after 429 changed frozen sampling request\nfirst:  %s\nsecond: %s", firstBody, secondBody)
	}

	secondID := waitProviderAttemptStatus(t, store, sessionstore.IntentSampling, commandID, 2, string(sessionstore.IntentResultAbandoned))
	setProviderRetryDueNow(t, store, secondID)
	runtime.Wake(session.ID)
	thirdBody := receiveProviderRequest(t, requests)
	if !bytes.Equal(firstBody, thirdBody) {
		t.Fatalf("retry after partial stream changed frozen sampling request\nfirst:  %s\nthird: %s", firstBody, thirdBody)
	}
	waitProviderCommandOutcome(t, store, session.ID, commandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)

	var attempts, abandoned, committed, configuredBudget int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*),count(*) FILTER (WHERE result->>'status'='abandoned'),
		count(*) FILTER (WHERE result IS NOT NULL AND NOT (result ? 'status')),
		count(*) FILTER (WHERE provider_attempt_budget=$2) FROM sampler_intent
		WHERE provider_command_id=$1 AND request->>'kind'='sampling'`, commandID, attemptBudget).Scan(&attempts, &abandoned, &committed, &configuredBudget); err != nil {
		t.Fatal(err)
	}
	if attempts != 3 || abandoned != 2 || committed != 1 || configuredBudget != 3 || count.Load() != 3 {
		t.Fatalf("provider attempts=%d abandoned=%d committed=%d with budget %d=%d requests=%d, want 3/2/1/3/3", attempts, abandoned, committed, attemptBudget, configuredBudget, count.Load())
	}
	owner := runtimeOwner(t, runtime, session.ID)
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if got := countUserMessage(t, conversation.Context(), input); got != 1 {
		t.Fatalf("original accepted input appears %d times after retries, want once", got)
	}
	for _, item := range testsupport.ResponsesItems(t, conversation.Context()) {
		if item.CallID == "partial-provider-call" {
			t.Fatal("tool call from the incomplete provider stream entered committed context")
		}
	}
	var cells, processes int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_cell WHERE conversation_id=$1`, session.ConversationID).Scan(&cells); err != nil {
		t.Fatal(err)
	}
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_process WHERE conversation_id=$1`, session.ConversationID).Scan(&processes); err != nil {
		t.Fatal(err)
	}
	if cells != 0 || processes != 0 {
		t.Fatalf("incomplete provider call dispatched executor work: cells=%d processes=%d", cells, processes)
	}
	stop()
}

func TestRuntimeTerminalSamplingFailureLeavesNextInputUsable(t *testing.T) {
	for _, tc := range []struct {
		name             string
		status, attempts int
	}{
		{name: "permanent", status: http.StatusBadRequest, attempts: 1},
		{name: "exhausted", status: http.StatusServiceUnavailable, attempts: 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := runtimetesting.NewStore(t, integrationDatabase)
			session := runtimetesting.OpenSessionForStore(t, store, "")
			const failedCommandID = "permanent-sampling-failure"
			const failedInput = "input whose provider route is rejected"
			runtimetesting.AdmitInput(t, store, session.ID, failedCommandID, failedInput)

			requests := make(chan []byte, tc.attempts+1)
			var count atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				body, err := io.ReadAll(r.Body)
				if err != nil {
					t.Errorf("read provider request: %v", err)
					return
				}
				requests <- body
				if count.Add(1) <= int32(tc.attempts) {
					w.Header().Set("Retry-After", "30")
					http.Error(w, "provider unavailable", tc.status)
					return
				}
				if err := writeSamplingSuccess(w, "next-input-response", "next input succeeded"); err != nil {
					t.Errorf("write provider success: %v", err)
				}
			}))
			t.Cleanup(server.Close)
			runtime, stop := startProviderSamplingRuntime(t, store, providerSamplingSettings(server.URL, 1))
			runtime.Wake(session.ID)
			for attempt := 1; attempt <= tc.attempts; attempt++ {
				_ = receiveProviderRequest(t, requests)
				intentID := waitProviderAttemptStatus(t, store, sessionstore.IntentSampling, failedCommandID, attempt, string(sessionstore.IntentResultAbandoned))
				if attempt < tc.attempts {
					setProviderRetryDueNow(t, store, intentID)
					runtime.Wake(session.ID)
				}
			}
			failed := waitProviderCommandOutcome(t, store, session.ID, failedCommandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED)
			if failed.GetFailureReason() == "" {
				t.Fatal("terminal provider failure did not retain a command failure reason")
			}
			sessionState, err := store.GetSession(t.Context(), session.ID, "")
			if err != nil || sessionState.Blocked {
				t.Fatalf("terminal provider failure blocked the Session: state=%+v err=%v", sessionState, err)
			}

			const nextCommandID = "input-after-permanent-sampling-failure"
			const nextInput = "next accepted input remains usable"
			runtimetesting.AdmitInput(t, store, session.ID, nextCommandID, nextInput)
			runtime.Wake(session.ID)
			nextBody := receiveProviderRequest(t, requests)
			if !strings.Contains(string(nextBody), nextInput) {
				t.Fatalf("next provider request omitted newly accepted input: %s", nextBody)
			}
			waitProviderCommandOutcome(t, store, session.ID, nextCommandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)
			if got := count.Load(); got != int32(tc.attempts+1) {
				t.Fatalf("provider requests=%d, want %d failed attempts and one next-input request", got, tc.attempts)
			}
			owner := runtimeOwner(t, runtime, session.ID)
			conversation, err := store.Read(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			if got := countUserMessage(t, conversation.Context(), failedInput); got != 1 {
				t.Fatalf("failed command input appears %d times, want once", got)
			}
			if got := countUserMessage(t, conversation.Context(), nextInput); got != 1 {
				t.Fatalf("next command input appears %d times, want once", got)
			}
			stop()
		})
	}
}

func TestRuntimeProviderBackoffReleasesPermitAndIgnoresRepeatedWake(t *testing.T) {
	store := runtimetesting.NewStore(t, integrationDatabase)
	waitingSession := runtimetesting.OpenSessionForStore(t, store, "")
	const waitingCommandID = "provider-waiting-command"
	const waitingInput = "provider attempt waiting for backoff"
	runtimetesting.AdmitInput(t, store, waitingSession.ID, waitingCommandID, waitingInput)

	var requestCount atomic.Int32
	firstRequest := make(chan []byte, 1)
	secondStarted := make(chan []byte, 1)
	allowSecondResponse := make(chan struct{})
	var releaseOnce sync.Once
	releaseSecond := func() { releaseOnce.Do(func() { close(allowSecondResponse) }) }
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read provider request: %v", err)
			return
		}
		requestNumber := requestCount.Add(1)
		if requestNumber == 1 {
			firstRequest <- body
			w.Header().Set("Retry-After", "30")
			http.Error(w, "temporarily rate limited", http.StatusTooManyRequests)
			return
		}
		if strings.Contains(string(body), "separate ready Session input") {
			select {
			case secondStarted <- body:
			default:
			}
			select {
			case <-allowSecondResponse:
			case <-r.Context().Done():
				return
			}
			if err := writeSamplingSuccess(w, "ready-session-response", "ready session completed"); err != nil {
				t.Errorf("write provider success: %v", err)
			}
			return
		}
		http.Error(w, "unexpected retry while backoff is pending", http.StatusServiceUnavailable)
	}))
	t.Cleanup(server.Close)
	runtime, stop := startProviderSamplingRuntime(t, store, providerSamplingSettings(server.URL, 1))
	runtime.Wake(waitingSession.ID)
	_ = receiveProviderRequest(t, firstRequest)
	intentID := waitProviderAttemptStatus(t, store, sessionstore.IntentSampling, waitingCommandID, 1, string(sessionstore.IntentResultAbandoned))
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_intent SET provider_retry_not_before=clock_timestamp()+interval '1 hour' WHERE id=$1`, intentID); err != nil {
		t.Fatal(err)
	}
	waitProviderTaskReleased(t, runtime, waitingSession.ID)
	for range 20 {
		runtime.Wake(waitingSession.ID)
	}
	runtime.mu.Lock()
	waitingTask := runtime.tasks[waitingSession.ID]
	waitingStillIdle := waitingTask != nil && waitingTask.waitingProvider && !waitingTask.busy && runtime.busyLocked() == 0
	runtime.mu.Unlock()
	if !waitingStillIdle || len(runtime.executing) != 0 {
		t.Fatalf("repeated work wakes reserved provider backoff capacity: idle=%v permits=%d", waitingStillIdle, len(runtime.executing))
	}

	readySession := runtimetesting.OpenSessionForStore(t, store, "")
	const readyCommandID = "ready-session-command"
	runtimetesting.AdmitInput(t, store, readySession.ID, readyCommandID, "separate ready Session input")
	runtime.Wake(readySession.ID)
	var readyBody []byte
	select {
	case readyBody = <-secondStarted:
	case <-time.After(10 * time.Second):
		t.Fatal("ready Session did not reach provider while another Session waited in backoff")
	}
	if !strings.Contains(string(readyBody), "separate ready Session input") || strings.Contains(string(readyBody), waitingInput) {
		t.Fatalf("backoff freed no usable provider capacity; ready request body=%s", readyBody)
	}
	runtime.mu.Lock()
	waitingTask = runtime.tasks[waitingSession.ID]
	readyTask := runtime.tasks[readySession.ID]
	busyCount, permits := runtime.busyLocked(), len(runtime.executing)
	waitingStillIdle = waitingTask != nil && waitingTask.waitingProvider && !waitingTask.busy
	readyRunning := readyTask != nil && readyTask.busy
	runtime.mu.Unlock()
	if !waitingStillIdle || !readyRunning || busyCount != 1 || permits != 1 {
		t.Fatalf("execution capacity while ready provider is held: waitingIdle=%v readyBusy=%v busy=%d permits=%d", waitingStillIdle, readyRunning, busyCount, permits)
	}
	for range 20 {
		runtime.Wake(waitingSession.ID)
	}
	runtime.mu.Lock()
	waitingStillIdle = runtime.tasks[waitingSession.ID] != nil && runtime.tasks[waitingSession.ID].waitingProvider && !runtime.tasks[waitingSession.ID].busy
	busyCount = runtime.busyLocked()
	runtime.mu.Unlock()
	if !waitingStillIdle || busyCount != 1 {
		t.Fatalf("repeated wakes for backoff Session displaced ready work: waitingIdle=%v busy=%d", waitingStillIdle, busyCount)
	}
	releaseSecond()
	waitProviderCommandOutcome(t, store, readySession.ID, readyCommandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)
	if got := requestCount.Load(); got != 2 {
		t.Fatalf("provider requests=%d, want only initial backoff attempt plus ready Session request", got)
	}
	stop()
}

func TestInterruptWakesInFlightProviderRequest(t *testing.T) {
	store := runtimetesting.NewStore(t, integrationDatabase)
	session := runtimetesting.OpenSessionForStore(t, store, "")
	runtimetesting.AdmitInput(t, store, session.ID, "turn", "begin sampling")
	requested := make(chan struct{}, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, err := io.Copy(io.Discard, r.Body); err != nil {
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.(http.Flusher).Flush()
		select {
		case requested <- struct{}{}:
		default:
		}
		select {
		case <-r.Context().Done():
		case <-t.Context().Done():
		}
	}))
	t.Cleanup(server.Close)
	runtime, _ := startProviderSamplingRuntime(t, store, providerSamplingSettings(server.URL, 1))
	select {
	case <-requested:
	case <-time.After(10 * time.Second):
		t.Fatal("provider request never started")
	}
	owner := runtimeOwner(t, runtime, session.ID)
	id, err := store.RequestInterrupt(t.Context(), owner.ConversationID, owner.Generation)
	if err != nil {
		t.Fatal(err)
	}
	eventually.Within(t, 10*time.Second, func() (bool, string) {
		var cancelled bool
		err := store.DB().QueryRowContext(t.Context(), `SELECT COALESCE((result->>'provider_cancelled')::boolean, false)
			FROM sampler_interrupt WHERE id=$1`, id).Scan(&cancelled)
		if err != nil {
			t.Fatal(err)
		}
		return cancelled, "waiting for the active turn to acknowledge the interrupt"
	})
}

func TestRuntimeInterruptDuringProviderBackoffPreventsRetry(t *testing.T) {
	store := runtimetesting.NewStore(t, integrationDatabase)
	session := runtimetesting.OpenSessionForStore(t, store, "")
	const commandID = "interrupted-provider-turn"
	runtimetesting.AdmitInput(t, store, session.ID, commandID, "provider request canceled by user")
	var requestCount atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, err := io.Copy(io.Discard, r.Body); err != nil {
			t.Errorf("read provider request: %v", err)
			return
		}
		requestCount.Add(1)
		w.Header().Set("Retry-After", "30")
		http.Error(w, "temporarily rate limited", http.StatusTooManyRequests)
	}))
	t.Cleanup(server.Close)
	runtime, stop := startProviderSamplingRuntime(t, store, providerSamplingSettings(server.URL, 1))
	runtime.Wake(session.ID)
	// The durable attempt state is the synchronization point; the response is already settled.
	intentID := waitProviderAttemptStatus(t, store, sessionstore.IntentSampling, commandID, 1, string(sessionstore.IntentResultAbandoned))
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_intent SET provider_retry_not_before=clock_timestamp()+interval '1 hour' WHERE id=$1`, intentID); err != nil {
		t.Fatal(err)
	}
	waitProviderTaskReleased(t, runtime, session.ID)
	const interruptID = "interrupt-provider-turn"
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
		CommandId: interruptID, Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: commandID}},
	}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
	runtime.Wake(session.ID)
	waitProviderCommandOutcome(t, store, session.ID, commandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)
	waitProviderCommandOutcome(t, store, session.ID, interruptID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)
	if got := requestCount.Load(); got != 1 {
		t.Fatalf("provider requests=%d after explicit interrupt, want no retry", got)
	}
	var retryDue sql.NullTime
	var suppressed string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT provider_retry_not_before,result->>'provider_retry_suppressed' FROM sampler_intent WHERE id=$1`, intentID).Scan(&retryDue, &suppressed); err != nil {
		t.Fatal(err)
	}
	if retryDue.Valid || suppressed != string(sessionstore.ProviderRetrySuppressionInterrupted) {
		t.Fatalf("interrupted attempt retry due=%v suppression=%q", retryDue, suppressed)
	}
	state, err := store.Read(t.Context(), runtimeOwner(t, runtime, session.ID))
	if err != nil || !state.Stopped {
		t.Fatalf("explicit interrupt did not stop the Session: state=%+v err=%v", state, err)
	}
	stop()
}

func TestRuntimeReconcilesLostProviderOwnerWithFreshSamplingContext(t *testing.T) {
	store := runtimetesting.NewStore(t, integrationDatabase)
	session := runtimetesting.OpenSessionForStore(t, store, "")
	const commandID = "sampling-after-owner-loss"
	const input = "continue after provider owner loss"
	runtimetesting.AdmitInput(t, store, session.ID, commandID, input)
	manager, _ := newFakeCodeModeManager(t)
	priorHostID := "00000000-0000-4000-8000-000000000001"
	if manager.InstanceID() == priorHostID {
		priorHostID = "00000000-0000-4000-8000-000000000002"
	}
	requests := make(chan []byte, 1)
	var requestCount atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read provider request: %v", err)
			return
		}
		requestCount.Add(1)
		requests <- body
		if err := writeSamplingSuccess(w, "host-loss-recovery-response", "fresh context sampled"); err != nil {
			t.Errorf("write provider success: %v", err)
		}
	}))
	t.Cleanup(server.Close)
	oldOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	command, err := store.NextCommand(t.Context(), oldOwner, session.ID)
	if err != nil || command == nil || command.ID != commandID {
		t.Fatalf("active command=%v err=%v", command, err)
	}
	if err := store.StampCodeHostInstance(t.Context(), oldOwner, priorHostID); err != nil {
		t.Fatal(err)
	}
	if _, err := store.PrepareSamplingContext(t.Context(), oldOwner); err != nil {
		t.Fatal(err)
	}
	settings := providerSamplingSettings(server.URL, 1)
	config := settings.Models[session.Model]
	connection, err := settings.Connection(session.Model)
	if err != nil {
		t.Fatal(err)
	}
	metadata, err := store.ProviderMetadata(t.Context(), oldOwner)
	if err != nil {
		t.Fatal(err)
	}
	intentID, frozenWire, err := store.Begin(t.Context(), oldOwner, sessionstore.ProviderRequestIntent(sessionstore.PendingIntentSampling, connection, config),
		&providerrequest.Preparation{ModelConfig: config, SessionEffort: session.Effort, SessionConfiguration: append([]byte(nil), session.Configuration...), Attribution: metadata}, func() *sessionstore.ProviderAttemptPolicy {
			policy := sessionstore.ProviderAttemptPolicy{Budget: 3, Lease: settings.ProviderAttemptLease(config)}
			return &policy
		}())
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Release(t.Context(), oldOwner); err != nil {
		t.Fatal(err)
	}

	// A new deployment may configure a different budget. Recovery keeps the step's durable budget.
	settings.ProviderAttemptBudget = new(5)
	runtime := New(store, settings, manager)
	cancel, done := startRuntimeForTest(t, runtime)
	var stopOnce sync.Once
	stop := func() {
		stopOnce.Do(func() {
			cancel()
			waitRuntimeDone(t, done)
		})
	}
	t.Cleanup(stop)
	runtime.Wake(session.ID)
	freshWire := receiveProviderRequest(t, requests)
	if bytes.Equal(frozenWire, freshWire) {
		t.Fatal("restarted owner resent the pre-loss frozen request body")
	}
	if !strings.Contains(string(freshWire), "The JavaScript host that held this Session's store()/load() values has been lost") {
		t.Fatalf("fresh provider request omitted the host-loss notice: %s", freshWire)
	}
	if bytes.Contains(freshWire, []byte("Active execution handles: ")) {
		t.Fatalf("host loss added an inventory alongside the reset notice: %s", freshWire)
	}
	retained, err := store.LoadProviderRequest(t.Context(), session.ID, "fixture-account", intentID)
	if err != nil || !bytes.Equal(retained.Body, frozenWire) {
		t.Fatalf("host loss changed the frozen historical request: err=%v", err)
	}
	waitProviderAttemptStatus(t, store, sessionstore.IntentSampling, commandID, 1, string(sessionstore.IntentResultAbandoned))
	waitProviderCommandOutcome(t, store, session.ID, commandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED)
	if got := requestCount.Load(); got != 1 {
		t.Fatalf("fresh owner provider requests=%d, want one new-context request", got)
	}
	var attempts int
	var secondID, previousID, stepID string
	var number, budget int
	var firstSuppression string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*),
		max(id::text) FILTER (WHERE provider_attempt_no=2),max(previous_provider_intent_id::text) FILTER (WHERE provider_attempt_no=2),
		max(provider_step_id::text) FILTER (WHERE provider_attempt_no=2),max(provider_attempt_no) FILTER (WHERE provider_attempt_no=2),
		max(provider_attempt_budget) FILTER (WHERE provider_attempt_no=2),
		max(result->>'provider_retry_suppressed') FILTER (WHERE provider_attempt_no=1)
		FROM sampler_intent WHERE provider_command_id=$1 AND request->>'kind'='sampling'`, commandID).Scan(&attempts, &secondID, &previousID, &stepID, &number, &budget, &firstSuppression); err != nil {
		t.Fatal(err)
	}
	if attempts != 2 || previousID != intentID || stepID != intentID || number != 2 || budget != 3 || firstSuppression != string(sessionstore.ProviderRetrySuppressionHostLoss) || secondID == "" {
		t.Fatalf("host-loss attempt lineage: attempts=%d second=%q previous=%q step=%q number=%d budget=%d suppression=%q", attempts, secondID, previousID, stepID, number, budget, firstSuppression)
	}
	owner := runtimeOwner(t, runtime, session.ID)
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if got := countUserMessage(t, conversation.Context(), input); got != 1 {
		t.Fatalf("accepted input appears %d times after owner recovery, want once", got)
	}
	var cells, processes int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_cell WHERE conversation_id=$1`, session.ConversationID).Scan(&cells); err != nil {
		t.Fatal(err)
	}
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_process WHERE conversation_id=$1`, session.ConversationID).Scan(&processes); err != nil {
		t.Fatal(err)
	}
	if cells != 0 || processes != 0 {
		t.Fatalf("provider recovery dispatched executor work: cells=%d processes=%d", cells, processes)
	}
	stop()
}
