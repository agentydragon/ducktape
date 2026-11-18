package executor

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executionstate"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executor/transport"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/outputbuffer"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/toolcontract"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	execfake "github.com/agentydragon/ducktape/agentplane/testing/executor"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestExecutorRejectsMalformedInputBeforeDispatch(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &execfake.Client{OnOpenSession: func(ctx context.Context, _ *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		return &execfake.Stream{Ctx: ctx, Initial: &executorpb.OpenSessionResponse{
			Status: execfake.CompletedStatus(), ExecutionSessionId: "fake-shell-session",
			Shell: &executorpb.ExecutionShell{Name: "bash", Path: "/bin/bash"},
		}}, nil
	}}
	executor := newFakeExecutor(t, store, owner, client)
	if operation, err := executor.transport.EnsureSession(t.Context()); err != nil || operation.Outcome != transport.Completed {
		t.Fatalf("initialize fake shell=%+v error=%v", operation, err)
	}
	starts := 0
	client.OnStartProcess = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		return &executorpb.StartProcessResponse{Status: execfake.CompletedStatus()}, nil
	}
	for _, tc := range []struct{ name, tool, args string }{
		{"negative exec yield", "exec_command", `{"cmd":"touch /tmp/should-not-run","yield_time_ms":-1}`},
		{"missing cmd", "exec_command", `{"workdir":"/tmp"}`},
		{"non-object exec input", "exec_command", `"touch /tmp/should-not-run"`},
		{"shell the executor does not report", "exec_command", `{"cmd":"touch /tmp/should-not-run","shell":"zsh"}`},
		{"string session_id", "write_stdin", `{"session_id":"test-process"}`},
		{"fractional stdin yield", "write_stdin", `{"session_id":1000,"yield_time_ms":1.5}`},
		{"non-string chars", "write_stdin", `{"session_id":1000,"chars":true}`},
		{"missing session_id", "write_stdin", `{"chars":"x"}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, err := executor.Invoke(context.Background(), tc.tool, json.RawMessage(tc.args))
			if !errors.As(err, new(*toolcontract.Rejection)) {
				t.Fatalf("malformed input error=%v, want a tool error", err)
			}
		})
	}
	if starts != 0 {
		t.Fatalf("malformed input dispatched %d process starts", starts)
	}
}

func TestOpenSessionEnvironmentFailureIsCatchableAndNotDispatched(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &execfake.Client{}
	opens, starts := 0, 0
	client.OnOpenSession = func(context.Context, *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		opens++
		if opens == 1 {
			return nil, executionEnvironmentStatusError(t, executorpb.ExecutionEnvironmentFailure_STATE_SUSPENDED)
		}
		return &execfake.Stream{Ctx: t.Context(), Initial: &executorpb.OpenSessionResponse{
			Status: execfake.CompletedStatus(), ExecutionSessionId: "fake-recovered-session",
		}}, nil
	}
	client.OnStartProcess = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		return &executorpb.StartProcessResponse{Status: execfake.CompletedStatus()}, nil
	}
	e := newFakeExecutor(t, store, owner, client)
	e.environment = map[string]string{}

	result, err := e.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"echo must not run"}`))
	var outcome *ToolOutcomeError
	if result != nil || !errors.As(err, &outcome) {
		t.Fatalf("exec_command result=%v error=%v, want a catchable tool outcome", result, err)
	}
	var message map[string]any
	if err := json.Unmarshal([]byte(outcome.Error()), &message); err != nil {
		t.Fatalf("environment failure is not structured JSON: %v: %s", err, outcome.Error())
	}
	if message["environment_state"] != "STATE_SUSPENDED" || message["remote_outcome"] != "not_dispatched" ||
		message["request_outcome"] != "not_dispatched" || message["automatic_replay"] != false ||
		message["method"] != "process/start" {
		t.Fatalf("environment failure facts=%+v", message)
	}
	if opens != 1 || starts != 0 {
		t.Fatalf("OpenSession attempts=%d StartProcess attempts=%d, want one open and no start", opens, starts)
	}
	var processes int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_process WHERE conversation_id=$1`, owner.ConversationID).Scan(&processes); err != nil {
		t.Fatal(err)
	}
	if processes != 0 {
		t.Fatalf("environment refusal before workspace setup registered %d processes, want none", processes)
	}
	if _, err := e.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"echo recovered","yield_time_ms":0}`)); err != nil {
		t.Fatalf("later operation after catchable environment failure: %v", err)
	}
	if opens != 2 || starts != 1 {
		t.Fatalf("later operation after known refusal opens=%d starts=%d, want a fresh open and one start", opens, starts)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestTypedStartEnvironmentFailureIsNotReplayed(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &execfake.Client{}
	starts := 0
	client.OnStartProcess = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		return nil, executionEnvironmentStatusError(t, executorpb.ExecutionEnvironmentFailure_STATE_NOT_READY)
	}
	e := newFakeExecutor(t, store, owner, client)
	e.environment = map[string]string{}

	_, err := e.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"touch should-not-run"}`))
	var outcome *ToolOutcomeError
	if !errors.As(err, &outcome) {
		t.Fatalf("exec_command error=%v, want catchable environment refusal", err)
	}
	var message map[string]any
	if err := json.Unmarshal([]byte(outcome.Error()), &message); err != nil {
		t.Fatalf("environment failure is not structured JSON: %v: %s", err, outcome.Error())
	}
	if message["environment_state"] != "STATE_NOT_READY" || message["remote_outcome"] != "not_dispatched" ||
		message["request_outcome"] != "not_dispatched" || message["automatic_replay"] != false || message["intent_id"] == "" {
		t.Fatalf("environment failure facts=%+v", message)
	}
	if starts != 1 {
		t.Fatalf("StartProcess attempts=%d, want exactly one", starts)
	}
	var processStatus string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT status FROM sampler_process WHERE conversation_id=$1 ORDER BY id DESC LIMIT 1`, owner.ConversationID).Scan(&processStatus); err != nil {
		t.Fatal(err)
	}
	if processStatus != string(executionstate.ProcessExited) {
		t.Fatalf("known not-dispatched process status=%q, want exited", processStatus)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestTypedReadEnvironmentFailurePreservesUnknownProcessEvidence(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	registerRunningModelProcess(t, store, owner, "suspended-process", 1400, 8, "output before suspension\n")
	client := &execfake.Client{}
	reads := 0
	client.OnReadProcess = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads++
		return nil, executionEnvironmentStatusError(t, executorpb.ExecutionEnvironmentFailure_STATE_SUSPENDED)
	}
	e := newFakeExecutor(t, store, owner, client)

	_, err := e.Invoke(t.Context(), "write_stdin", json.RawMessage(`{"session_id":1400}`))
	var outcome *ToolOutcomeError
	if !errors.As(err, &outcome) {
		t.Fatalf("write_stdin error=%v, want catchable environment refusal", err)
	}
	outerEvidence, ok := outcome.Evidence().(executionFailureEvidence)
	if !ok || outerEvidence.RemoteOutcome != sessionstore.NestedUnknown || outerEvidence.RequestOutcome == nil || *outerEvidence.RequestOutcome != sessionstore.NestedNotDispatched ||
		outerEvidence.AfterSeq == nil || *outerEvidence.AfterSeq != 8 || outerEvidence.BufferedOutputBase64 == nil ||
		!bytes.Equal(*outerEvidence.BufferedOutputBase64, []byte("output before suspension\n")) {
		t.Fatalf("outer tool failure lost typed unknown-process evidence: %+v", outcome.Evidence())
	}
	var message struct {
		EnvironmentState   string                `json:"environment_state"`
		RemoteOutcome      string                `json:"remote_outcome"`
		RequestOutcome     string                `json:"request_outcome"`
		AutomaticReplay    bool                  `json:"automatic_replay"`
		Method             string                `json:"method"`
		ProcessID          string                `json:"process_id"`
		ExecutionSessionID string                `json:"execution_session_id"`
		AfterSeq           *uint64               `json:"after_seq"`
		ObservedOutput     observedProcessOutput `json:"observed_output"`
	}
	if err := json.Unmarshal([]byte(outcome.Error()), &message); err != nil {
		t.Fatalf("environment failure is not structured JSON: %v: %s", err, outcome.Error())
	}
	if message.EnvironmentState != "STATE_SUSPENDED" || message.RemoteOutcome != "unknown" || message.RequestOutcome != "not_dispatched" ||
		message.AutomaticReplay || message.Method != "process/read" || message.ProcessID != "suspended-process" ||
		message.ExecutionSessionID != "retained-executor" || message.AfterSeq == nil || *message.AfterSeq != 8 ||
		message.ObservedOutput.Text != "output before suspension\n" {
		t.Fatalf("process read environment failure facts=%+v", message)
	}
	if reads != 1 {
		t.Fatalf("ReadProcess calls=%d, want no automatic retry", reads)
	}
	process, err := store.Process(t.Context(), owner, "suspended-process")
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != executionstate.ProcessAbandoned || process.AfterSequence == nil || *process.AfterSequence != 8 ||
		string(process.BufferedOutput) != "output before suspension\n" {
		t.Fatalf("uncertain process state or output was lost: %+v", process)
	}
	var raw []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT result FROM sampler_intent WHERE conversation_id=$1 AND request->>'method'='process/read' ORDER BY id DESC LIMIT 1`, owner.ConversationID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var evidence map[string]any
	if err := json.Unmarshal(raw, &evidence); err != nil {
		t.Fatal(err)
	}
	if evidence["remote_outcome"] != "not_dispatched" || evidence["request_outcome"] != "not_dispatched" ||
		evidence["environment_state"] != "STATE_SUSPENDED" || evidence["after_seq"] != float64(8) ||
		evidence["buffered_output_base64"] != base64.StdEncoding.EncodeToString([]byte("output before suspension\n")) {
		t.Fatalf("nested read intent did not retain pre-dispatch evidence: %+v", evidence)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestTypedNotDispatchedStartSettlesWithZeroCursor(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	if err := store.RegisterModelProcess(t.Context(), owner, "not-dispatched-start", 1402, false); err != nil {
		t.Fatal(err)
	}
	zero := uint64(0)
	output := outputbuffer.New(16)
	output.Append(nil)
	executor := newFakeExecutor(t, store, owner, &execfake.Client{})
	err := executor.outcomeToolError(t.Context(), &transport.Failure{
		Kind: transport.FailureNotDispatched, Method: transport.MethodProcessStart, ProcessID: "not-dispatched-start", ExecutionSessionID: "retained-executor",
		AfterSequence: &zero, Output: output, Cause: errors.New("executor session unavailable"),
	})
	var outcome *ToolOutcomeError
	if !errors.As(err, &outcome) {
		t.Fatalf("not-dispatched start error=%v, want a catchable tool outcome", err)
	}
	evidence, ok := outcome.Evidence().(executionFailureEvidence)
	if !ok || evidence.RemoteOutcome != sessionstore.NestedNotDispatched || evidence.RequestOutcome == nil || *evidence.RequestOutcome != sessionstore.NestedNotDispatched ||
		evidence.AfterSeq == nil || *evidence.AfterSeq != 0 || evidence.BufferedOutput == nil || *evidence.BufferedOutput != "" ||
		evidence.BufferedOutputBase64 == nil || *evidence.BufferedOutputBase64 == nil || len(*evidence.BufferedOutputBase64) != 0 ||
		evidence.TransportError != "executor session unavailable" {
		t.Fatalf("not-dispatched start lost typed zero/empty evidence: %+v", outcome.Evidence())
	}
	process, err := store.Process(t.Context(), owner, "not-dispatched-start")
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != executionstate.ProcessExited || process.AfterSequence == nil || *process.AfterSequence != 0 {
		t.Fatalf("known not-dispatched start was not settled with its zero cursor: %+v", process)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestExecutionFailureEvidencePreservesCursorAndOutputPresence(t *testing.T) {
	largeCursor := uint64(1<<53 + 17)
	outputBytes := []byte{0, 'x', 0xff}
	output := outputbuffer.New(16)
	output.Append(outputBytes)
	evidence := executionFailureRecord{
		method: transport.MethodProcessRead, remoteOutcome: sessionstore.NestedUnknown,
		afterSequence: &largeCursor, output: output,
	}.toolEvidence()
	encoded, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(encoded, []byte(`"after_seq":9007199254741009`)) {
		t.Fatalf("large cursor was narrowed in JSON projection: %s", encoded)
	}
	var decoded executionFailureEvidence
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.AfterSeq == nil || *decoded.AfterSeq != largeCursor || decoded.BufferedOutputBase64 == nil ||
		!bytes.Equal(*decoded.BufferedOutputBase64, outputBytes) {
		t.Fatalf("typed projection changed cursor or full output: %+v", decoded)
	}

	zero := uint64(0)
	empty := outputbuffer.New(16)
	empty.Append([]byte{})
	emptyEvidence := executionFailureRecord{method: transport.MethodProcessRead, afterSequence: &zero, output: empty}.toolEvidence()
	emptyJSON, err := json.Marshal(emptyEvidence)
	if err != nil {
		t.Fatal(err)
	}
	var emptyFields map[string]json.RawMessage
	if err := json.Unmarshal(emptyJSON, &emptyFields); err != nil {
		t.Fatal(err)
	}
	if string(emptyFields["after_seq"]) != "0" || string(emptyFields["buffered_output"]) != `""` ||
		string(emptyFields["buffered_output_base64"]) != `""` || string(emptyFields["buffered_bytes"]) != "0" {
		t.Fatalf("present empty output or zero cursor changed in JSON projection: %s", emptyJSON)
	}

	absentJSON, err := json.Marshal(executionFailureRecord{method: transport.MethodProcessRead, afterSequence: &zero}.toolEvidence())
	if err != nil {
		t.Fatal(err)
	}
	var absentFields map[string]json.RawMessage
	if err := json.Unmarshal(absentJSON, &absentFields); err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"buffered_output", "buffered_output_base64", "buffered_bytes", "omitted_bytes"} {
		if _, present := absentFields[field]; present {
			t.Fatalf("absent output unexpectedly projected %q: %s", field, absentJSON)
		}
	}
}

func TestTypedWriteEnvironmentFailureDoesNotReplayInput(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	if err := store.RegisterModelProcess(t.Context(), owner, "refused-write-process", 1401, true); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, "refused-write-process", nil, executionstate.ProcessRunning, nil, false); err != nil {
		t.Fatal(err)
	}
	client := &execfake.Client{}
	writes := 0
	client.OnWriteProcess = func(context.Context, *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error) {
		writes++
		return nil, executionEnvironmentStatusError(t, executorpb.ExecutionEnvironmentFailure_STATE_CONTROL_ROUTE_UNAVAILABLE)
	}
	e := newFakeExecutor(t, store, owner, client)

	_, err := e.Invoke(t.Context(), "write_stdin", json.RawMessage(`{"session_id":1401,"chars":"must not be sent"}`))
	var outcome *ToolOutcomeError
	if !errors.As(err, &outcome) {
		t.Fatalf("write_stdin error=%v, want catchable environment refusal", err)
	}
	var message map[string]any
	if err := json.Unmarshal([]byte(outcome.Error()), &message); err != nil {
		t.Fatalf("environment failure is not structured JSON: %v: %s", err, outcome.Error())
	}
	if message["environment_state"] != "STATE_CONTROL_ROUTE_UNAVAILABLE" || message["remote_outcome"] != "not_dispatched" ||
		message["request_outcome"] != "not_dispatched" || message["automatic_replay"] != false || message["method"] != "process/write" {
		t.Fatalf("write refusal facts=%+v", message)
	}
	if writes != 1 {
		t.Fatalf("WriteProcess calls=%d, want exactly one", writes)
	}
	process, err := store.Process(t.Context(), owner, "refused-write-process")
	if err != nil || process.Status != executionstate.ProcessRunning {
		t.Fatalf("not-dispatched write changed process state: process=%+v err=%v", process, err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestApplyPatchStartRejectionIsReturnedAsNestedToolFailure(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &execfake.Client{}
	client.OnStartProcess = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		return &executorpb.StartProcessResponse{Status: rejectedExecutionStatus("patch rejected")}, nil
	}
	e := newFakeExecutor(t, store, owner, client)

	_, err := e.Invoke(t.Context(), "apply_patch", json.RawMessage(`"*** Begin Patch\n*** End Patch"`))
	if !errors.As(err, new(*toolcontract.Rejection)) {
		t.Fatalf("apply_patch error=%v, want a rejected nested tool promise", err)
	}
	if errors.As(err, new(*ToolOutcomeError)) {
		t.Fatalf("known pre-launch rejection reported an uncertain effect: %v", err)
	}
	state, err := store.ExecutionState(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if len(state.Processes) != 0 {
		t.Fatalf("rejected start left live process state: %+v", state.Processes)
	}
	var processStatus string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT status FROM sampler_process WHERE conversation_id=$1`, owner.ConversationID).Scan(&processStatus); err != nil {
		t.Fatal(err)
	}
	if processStatus != "exited" {
		t.Fatalf("rejected start process status=%q, want exited after durable settlement", processStatus)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestRejectedProcessReadDuringToolAbandonsOnlyThatHandle(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	registerRunningModelProcess(t, store, owner, "expired-native-process", 1200, 7, "observed output")
	registerRunningModelProcess(t, store, owner, "unrelated-live-process", 1201, 3, "other output")
	client := &execfake.Client{}
	reads := 0
	client.OnReadProcess = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		if request.ProcessId != "expired-native-process" {
			t.Fatalf("read process ID=%q", request.ProcessId)
		}
		reads++
		if reads == 1 {
			return &executorpb.ReadProcessResponse{
				Status:  execfake.CompletedStatus(),
				Chunks:  []*executorpb.ExecutionOutputChunk{{Seq: 8, Stream: "stdout", Data: []byte("new output")}},
				NextSeq: 9,
			}, nil
		}
		return &executorpb.ReadProcessResponse{Status: rejectedExecutionStatus("process unavailable")}, nil
	}
	e := newFakeExecutor(t, store, owner, client)

	_, err := e.Invoke(t.Context(), "write_stdin", json.RawMessage(`{"session_id":1200}`))
	if !errors.As(err, new(*toolcontract.Rejection)) {
		t.Fatalf("write_stdin error=%v, want a rejected nested tool promise", err)
	}
	var outcome *ToolOutcomeError
	if !errors.As(err, &outcome) {
		t.Fatalf("missing process rejection evidence: %v", err)
	}
	evidence, ok := outcome.Evidence().(rejectedProcessReadEvidence)
	if !ok || evidence.RemoteOutcome != sessionstore.NestedUnknown || evidence.AutomaticReplay {
		t.Fatalf("missing process rejection evidence=%+v", outcome)
	}
	stale, err := store.Process(t.Context(), owner, "expired-native-process")
	if err != nil {
		t.Fatal(err)
	}
	if stale.Status != "abandoned" || stale.AfterSequence == nil || *stale.AfterSequence != 8 || string(stale.BufferedOutput) != "observed outputnew output" {
		t.Fatalf("stale process lost prior evidence or claimed a terminal result: %+v", stale)
	}
	if reads != 2 {
		t.Fatalf("process read calls=%d, want one observed page and a later rejection", reads)
	}
	live, err := store.Process(t.Context(), owner, "unrelated-live-process")
	if err != nil {
		t.Fatal(err)
	}
	if live.Status != "running" || live.AfterSequence == nil || *live.AfterSequence != 3 || string(live.BufferedOutput) != "other output" {
		t.Fatalf("unrelated process changed after rejection: %+v", live)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestReconcileRejectedProcessReadContinuesPastUnrelatedLiveProcess(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	registerRunningModelProcess(t, store, owner, "expired-native-process", 1300, 9, "observed output")
	registerRunningModelProcess(t, store, owner, "unrelated-live-process", 1301, 4, "other output")
	reads := map[string]int{}
	client := &execfake.Client{}
	client.OnReadProcess = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads[request.ProcessId]++
		if request.ProcessId == "expired-native-process" {
			return &executorpb.ReadProcessResponse{Status: rejectedExecutionStatus("process unavailable")}, nil
		}
		return &executorpb.ReadProcessResponse{Status: execfake.CompletedStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}}, nil
	}
	e := newFakeExecutor(t, store, owner, client)

	progressed, err := e.ReconcileProcesses(t.Context(), time.Second)
	if err != nil || !progressed {
		t.Fatalf("progressed=%t error=%v, want handled stale handle", progressed, err)
	}
	if reads["expired-native-process"] != 1 || reads["unrelated-live-process"] != 1 {
		t.Fatalf("reconciliation reads=%v, want both handles visited", reads)
	}
	stale, err := store.Process(t.Context(), owner, "expired-native-process")
	if err != nil {
		t.Fatal(err)
	}
	if stale.Status != "abandoned" || stale.AfterSequence == nil || *stale.AfterSequence != 9 || string(stale.BufferedOutput) != "observed output" {
		t.Fatalf("reconciliation lost stale process evidence: %+v", stale)
	}
	live, err := store.Process(t.Context(), owner, "unrelated-live-process")
	if err != nil {
		t.Fatal(err)
	}
	if live.Status != "running" || live.AfterSequence == nil || *live.AfterSequence != 4 || string(live.BufferedOutput) != "other output" {
		t.Fatalf("unrelated process changed during reconciliation: %+v", live)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestRejectedExecutorOperationRemainsOperationalWhenJournalFails(t *testing.T) {
	t.Run("intent result", func(t *testing.T) {
		store, owner := claimedExecutorStore(t)
		_, err := store.DB().ExecContext(t.Context(), `ALTER TABLE sampler_intent ADD CONSTRAINT test_rejected_executor_intent CHECK (
			result IS NULL OR request->>'method' <> 'process/start' OR result->>'outcome' <> 'rejected')`)
		if err != nil {
			t.Fatal(err)
		}
		client := &execfake.Client{}
		client.OnStartProcess = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
			return &executorpb.StartProcessResponse{Status: rejectedExecutionStatus("patch rejected")}, nil
		}
		e := newFakeExecutor(t, store, owner, client)
		_, err = e.Invoke(t.Context(), "apply_patch", json.RawMessage(`"*** Begin Patch\n*** End Patch"`))
		assertRejectedSettlementFailedOperationally(t, err)
		state, stateErr := store.ExecutionState(t.Context(), owner)
		if stateErr != nil {
			t.Fatal(stateErr)
		}
		if len(state.Processes) != 1 || state.Processes[0].Status != "starting" {
			t.Fatalf("failed intent journaling was converted into a settled tool rejection: %+v", state.Processes)
		}
	})

	t.Run("process settlement", func(t *testing.T) {
		store, owner := claimedExecutorStore(t)
		_, err := store.DB().ExecContext(t.Context(), `ALTER TABLE sampler_process ADD CONSTRAINT test_rejected_process_settlement CHECK (
			status <> 'exited' OR model_session_id IS NULL)`)
		if err != nil {
			t.Fatal(err)
		}
		client := &execfake.Client{}
		client.OnStartProcess = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
			return &executorpb.StartProcessResponse{Status: rejectedExecutionStatus("command rejected")}, nil
		}
		e := newFakeExecutor(t, store, owner, client)
		e.environment = map[string]string{}
		_, err = e.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"true"}`))
		assertRejectedSettlementFailedOperationally(t, err)
		state, stateErr := store.ExecutionState(t.Context(), owner)
		if stateErr != nil {
			t.Fatal(stateErr)
		}
		if len(state.Processes) != 1 || state.Processes[0].Status != "starting" {
			t.Fatalf("failed process settlement was converted into a settled tool rejection: %+v", state.Processes)
		}
		assertNoUnresolvedNestedIntents(t, store, owner)
	})
}

func assertRejectedSettlementFailedOperationally(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("expected settlement failure")
	}
	var rejected *transport.Failure
	if errors.As(err, new(*toolcontract.Rejection)) || errors.As(err, &rejected) && rejected.Kind == transport.FailureRejected {
		t.Fatalf("failed rejection settlement was exposed as a recoverable tool rejection: %v", err)
	}
}

func rejectedExecutionStatus(message string) *executorpb.ExecutionStatus {
	return &executorpb.ExecutionStatus{
		Outcome:      executorpb.ExecutionStatus_OUTCOME_REJECTED,
		UpstreamCode: ptrInt32(-32602),
		Message:      message,
	}
}

func executionEnvironmentStatusError(t *testing.T, state executorpb.ExecutionEnvironmentFailure_State) error {
	t.Helper()
	response, err := status.New(codes.Unavailable, "execution environment refused the request").WithDetails(&executorpb.ExecutionEnvironmentFailure{
		State: state, Outcome: executorpb.ExecutionEnvironmentFailure_OUTCOME_NOT_DISPATCHED,
	})
	if err != nil {
		t.Fatal(err)
	}
	return response.Err()
}

func registerRunningModelProcess(t *testing.T, store *sessionstore.Store, owner sessionstore.Owner, id string, sessionID int, after uint64, output string) {
	t.Helper()
	if err := store.RegisterModelProcess(t.Context(), owner, id, sessionID, false); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, id, nil, "running", nil, false); err != nil {
		t.Fatal(err)
	}
	if err := store.RetainProcessOutput(t.Context(), owner, id, after, []byte(output), false, nil); err != nil {
		t.Fatal(err)
	}
}

func TestAbandonedWriteStdinIsARecoverableToolError(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterModelProcess(t.Context(), owner, "unknown-remote-process", 77, false); err != nil {
		t.Fatal(err)
	}
	if err := store.FailExecution(t.Context(), owner, session.ID, nil, errors.New("executor disconnected")); err != nil {
		t.Fatal(err)
	}
	blocked, err := store.GetSession(t.Context(), session.ID, "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	if !blocked.Blocked {
		t.Fatal("unconfirmed executor process did not block the session")
	}
	if err := store.Release(t.Context(), owner); err != nil {
		t.Fatal(err)
	}
	if _, err := store.RecoverSession(t.Context(), session.ID, "fixture-account", blocked.Cursor, "remote process outcome reviewed", ""); err != nil {
		t.Fatal(err)
	}
	newOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	sends := 0
	client := &execfake.Client{
		OnWriteProcess: func(context.Context, *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error) {
			sends++
			return nil, errors.New("abandoned process unexpectedly attempted a write")
		},
		OnReadProcess: func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
			t.Fatal("abandoned process unexpectedly attempted a read")
			return nil, nil
		},
	}
	executor := newFakeExecutor(t, store, newOwner, client)
	result, err := executor.writeStdin(t.Context(), json.RawMessage(`{"session_id":77,"chars":"must not be sent"}`))
	if result != nil || !errors.As(err, new(*toolcontract.Rejection)) || !strings.Contains(err.Error(), "no input was sent") {
		t.Fatalf("write_stdin result=%v error=%v, want a tool error disclosing no input was sent", result, err)
	}
	if sends != 0 {
		t.Fatalf("write_stdin sent %d executor requests for an abandoned handle", sends)
	}
	process, err := store.ProcessByModelSession(t.Context(), newOwner, 77)
	if err != nil || process.Status != "abandoned" {
		t.Fatalf("abandoned process=%+v error=%v", process, err)
	}
}

func TestNotDispatchedJournalEvidenceKeepsLegacyProjection(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	executor := newFakeExecutor(t, store, owner, &execfake.Client{})
	handled := executor.outcomeToolError(t.Context(), &transport.Failure{
		Kind: transport.FailureNotDispatched, Method: transport.MethodInitialize, Cause: errors.New("connection refused"),
	})
	var outcome *ToolOutcomeError
	if !errors.As(handled, &outcome) {
		t.Fatalf("not-dispatched error=%v, want structured tool error", handled)
	}
	evidence, ok := outcome.Evidence().(executionFailureEvidence)
	if !ok {
		t.Fatalf("not-dispatched evidence has type %T, want executionFailureEvidence", outcome.Evidence())
	}
	if evidence.Method != transport.MethodInitialize || evidence.RemoteOutcome != sessionstore.NestedNotDispatched ||
		evidence.RequestOutcome == nil || *evidence.RequestOutcome != sessionstore.NestedNotDispatched ||
		evidence.AutomaticReplay || evidence.TransportError != "connection refused" {
		t.Fatalf("not-dispatched evidence lost legacy fields: %+v", evidence)
	}
	wire, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(wire, &fields); err != nil {
		t.Fatal(err)
	}
	for _, omitted := range []string{"message", "phase", "observed_output"} {
		if _, ok := fields[omitted]; ok {
			t.Errorf("legacy not-dispatched journal projection unexpectedly contains %q: %s", omitted, wire)
		}
	}
	if got := string(fields["transport_error"]); got != `"connection refused"` {
		t.Errorf("legacy transport_error=%s, want %q (wire %s)", got, "connection refused", wire)
	}
}

func TestTransportFailureExposesObservedProcessOutputAndCursor(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	if err := store.RegisterModelProcess(t.Context(), owner, "observed-process", 78, false); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, "observed-process", nil, executionstate.ProcessRunning, nil, false); err != nil {
		t.Fatal(err)
	}
	output := outputbuffer.New(outputbuffer.MaxBytes)
	output.Append([]byte("stdout before failure\n"))
	cursor := uint64(8)
	failure := &transport.Failure{
		Kind: transport.FailureTransport, Method: transport.MethodProcessRead, Phase: "grpc", IntentID: "read-intent", ProcessID: "observed-process",
		ExecutionSessionID: "retained-executor", AfterSequence: &cursor, Output: output,
		Cause: status.Error(codes.Unavailable, "read reply lost"),
	}
	executor := newFakeExecutor(t, store, owner, &execfake.Client{})
	err := executor.outcomeToolError(t.Context(), failure)
	var rejected *ToolOutcomeError
	if !errors.As(err, &rejected) {
		t.Fatalf("observed read failure=%v, want nested promise rejection", err)
	}
	var message observedProcessFailure
	if err := json.Unmarshal([]byte(rejected.Error()), &message); err != nil {
		t.Fatalf("rejected promise is not parseable JSON: %v: %s", err, rejected.Error())
	}
	if message.RemoteOutcome != sessionstore.NestedUnknown || message.AutomaticReplay || message.Method != transport.MethodProcessRead ||
		message.IntentID != "read-intent" || message.ProcessID != "observed-process" || message.AfterSeq == nil || *message.AfterSeq != cursor ||
		message.ObservedOutput == nil || message.ObservedOutput.Text != "stdout before failure\n" || message.ObservedOutput.Truncated ||
		!strings.Contains(message.Message, "read reply lost") || !strings.Contains(message.Message, "mutation was not replayed") {
		t.Fatalf("observed read rejection=%+v", message)
	}
	process, err := store.Process(t.Context(), owner, "observed-process")
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != executionstate.ProcessAbandoned || process.AfterSequence == nil || *process.AfterSequence != cursor ||
		string(process.BufferedOutput) != "stdout before failure\n" {
		t.Fatalf("observed output/cursor were not retained: %+v", process)
	}
	evidence, ok := rejected.Evidence().(executionFailureEvidence)
	if !ok || evidence.BufferedOutputBase64 == nil || *evidence.BufferedOutputBase64 == nil ||
		!bytes.Equal(*evidence.BufferedOutputBase64, output.RetainedBytes()) || evidence.OmittedBytes == nil || *evidence.OmittedBytes != 0 {
		t.Fatalf("full output is missing from FollowEvents evidence: %+v", rejected.Evidence())
	}
}

func TestExecutionFailureEvidencePreservesZeroCursorAndPresentEmptyOutput(t *testing.T) {
	cursor := uint64(0)
	requestOutcome := sessionstore.NestedNotDispatched
	record := executionFailureRecord{
		message: "stale read", method: transport.MethodProcessRead, intentID: "read-intent",
		processID: "process-1", executionSessionID: "session-1", afterSequence: &cursor,
		remoteOutcome: sessionstore.NestedUnknown, requestOutcome: &requestOutcome, automaticReplay: false,
		currentIncarnationUID: "current-incarnation", output: outputbuffer.New(64),
	}
	evidence := record.toolEvidence()
	if evidence.AfterSeq == nil || *evidence.AfterSeq != 0 || evidence.BufferedOutput == nil || *evidence.BufferedOutput != "" ||
		evidence.BufferedOutputBase64 == nil || *evidence.BufferedOutputBase64 == nil || len(*evidence.BufferedOutputBase64) != 0 ||
		evidence.BufferedBytes == nil || *evidence.BufferedBytes != 0 || evidence.OmittedBytes == nil || *evidence.OmittedBytes != 0 {
		t.Fatalf("typed evidence lost zero cursor or present empty buffer: %+v", evidence)
	}
	journalEvidence := record.intentEvidence()
	if journalEvidence.Message != "" {
		t.Fatalf("journal evidence should not repeat the model error text: %+v", journalEvidence)
	}
	wire, err := json.Marshal(journalEvidence)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(wire, &fields); err != nil {
		t.Fatal(err)
	}
	for name, want := range map[string]string{
		"after_seq": "0", "buffered_output": `""`, "buffered_output_base64": `""`,
		"buffered_bytes": "0", "omitted_bytes": "0",
	} {
		if got := string(fields[name]); got != want {
			t.Errorf("typed evidence %s=%s, want %s (full evidence %s)", name, got, want, wire)
		}
	}
	if _, ok := fields["observed_output"]; ok {
		t.Fatalf("empty output should not fabricate observed output: %s", wire)
	}
	if _, ok := fields["message"]; ok {
		t.Fatalf("intent evidence should not add a message field: %s", wire)
	}

	message, err := executionFailureMessage(record)
	if err != nil {
		t.Fatal(err)
	}
	var modelFailure observedProcessFailure
	if err := json.Unmarshal([]byte(message), &modelFailure); err != nil {
		t.Fatalf("decode JS-facing failure record %q: %v", message, err)
	}
	if modelFailure.AfterSeq == nil || *modelFailure.AfterSeq != 0 || modelFailure.CurrentIncarnationUID != "current-incarnation" ||
		modelFailure.RequestOutcome == nil || *modelFailure.RequestOutcome != sessionstore.NestedNotDispatched || modelFailure.ObservedOutput != nil {
		t.Fatalf("JS-facing typed failure lost cursor/outcome or fabricated output: %+v", modelFailure)
	}
}

func TestStoreBindingsJournalFencesStaleOwner(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	var workspacePrepared atomic.Bool
	frame := transport.NativeFrame{
		OperationID: "fake-active-notification", ExecutionSession: "fake-execution-session",
		Direction: "notification", Payload: []byte("fake native notification"),
	}
	if err := (&storeBindings{store: store, owner: owner, workspacePrepared: &workspacePrepared}).RecordExecutorNativeFrame(t.Context(), frame); err != nil {
		t.Fatalf("active owner could not journal native notification: %v", err)
	}
	if err := store.Release(t.Context(), owner); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Claim(t.Context(), session.ConversationID, time.Minute); err != nil {
		t.Fatal(err)
	}
	var lost *sessionstore.LostOwnershipError
	frame.OperationID = "fake-stale-notification"
	if err := (&storeBindings{store: store, owner: owner, workspacePrepared: &workspacePrepared}).RecordExecutorNativeFrame(t.Context(), frame); !errors.As(err, &lost) {
		t.Fatalf("stale journal error=%v, want owner fence", err)
	}
	events, err := store.Events(t.Context(), session.ID, 0, 4096)
	if err != nil {
		t.Fatal(err)
	}
	active, stale := 0, 0
	for _, entry := range events {
		boundary := entry.GetEvent().GetBoundaryRecord()
		if boundary == nil || boundary.GetBoundary() != "executor" {
			continue
		}
		switch boundary.GetCorrelationId() {
		case "fake-active-notification":
			active++
		case "fake-stale-notification":
			stale++
		}
	}
	if active != 1 || stale != 0 {
		t.Fatalf("native notification journal active=%d stale=%d, want one durable active frame and no stale frame", active, stale)
	}
}

func TestObservedProcessFailureMarksBothOutputTruncationLayers(t *testing.T) {
	output := outputbuffer.New(16 * 1024)
	output.Append(bytes.Repeat([]byte("a"), 32*1024))
	cursor := uint64(12)
	message, err := executionFailureMessage(executionFailureRecord{
		message: "typed execution process/read grpc failed; remote effect outcome is unknown; mutation was not replayed: connection closed",
		method:  transport.MethodProcessRead, phase: "grpc", intentID: "read-intent", processID: "observed-process",
		afterSequence: &cursor, output: output,
	})
	if err != nil {
		t.Fatal(err)
	}
	var parsed observedProcessFailure
	if err := json.Unmarshal([]byte(message), &parsed); err != nil {
		t.Fatal(err)
	}
	if parsed.ObservedOutput == nil {
		t.Fatal("failure omitted observed process output")
	}
	view := *parsed.ObservedOutput
	if !view.Truncated || view.RetainedBytes != 16*1024 || view.OmittedBytes != 16*1024 ||
		view.DisplayOmittedBytes != 8*1024 || view.ShownHeadBytes != 4*1024 || view.ShownTailBytes != 4*1024 ||
		!strings.Contains(view.Text, "8192 retained bytes not shown; 16384 earlier bytes omitted") {
		t.Fatalf("truncation metadata=%+v", view)
	}
	evidence := executionFailureRecord{
		method: transport.MethodProcessRead, phase: "grpc", intentID: "read-intent", processID: "observed-process",
		afterSequence: &cursor, output: output,
	}.intentEvidence()
	if evidence.BufferedOutputBase64 == nil || *evidence.BufferedOutputBase64 == nil ||
		!bytes.Equal(*evidence.BufferedOutputBase64, output.RetainedBytes()) || evidence.OmittedBytes == nil || *evidence.OmittedBytes != 16*1024 {
		t.Fatalf("truncated output cannot be retrieved exactly from evidence: %+v", evidence)
	}
}

func TestExecutorDrainsOutputThatArrivesAfterExit(t *testing.T) {
	const (
		setupOperation     = "setup"
		commandOperation   = "command"
		reconcileOperation = "reconcile"
	)
	for _, operation := range []string{setupOperation, commandOperation, reconcileOperation} {
		t.Run(operation, func(t *testing.T) {
			store, owner := claimedExecutorStore(t)
			reads := 0
			client := &execfake.Client{}
			var e *Executor
			client.OnStartProcess = func(_ context.Context, request *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
				return &executorpb.StartProcessResponse{Status: execfake.CompletedStatus(), ProcessId: request.ProcessId}, nil
			}
			client.OnReadProcess = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
				if reads == 0 && request.AfterSeq != nil || reads > 0 && (request.AfterSeq == nil || *request.AfterSeq != uint64(reads)) {
					t.Fatalf("read %d cursor=%v", reads, request.AfterSeq)
				}
				page := &executorpb.ReadProcessResponse{Status: execfake.CompletedStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}, Exited: true, ExitCode: ptrInt32(23), NextSeq: uint64(reads + 2)}
				switch reads {
				case 0: // Exit arrived before stdout readers published their final bytes.
					if operation != reconcileOperation {
						// A notification can arrive while the read response is in flight.
						e.signalProcessChange()
					}
				case 1:
					page.Chunks = []*executorpb.ExecutionOutputChunk{{Seq: 2, Stream: "stdout", Data: []byte("late stdout\n")}}
					page.Closed = true
				case 2:
					page.Closed = true
				default:
					t.Fatal("read after terminal empty page")
				}
				reads++
				return page, nil
			}
			e = newFakeExecutor(t, store, owner, client)
			var output string
			var code int
			var err error
			if operation == setupOperation {
				code, err = e.Setup(t.Context(), "printf 'late stdout\\n'; exit 23", func(stream string, value []byte) error {
					if stream != "stdout" {
						t.Fatalf("unexpected stream %s", stream)
					}
					output += string(value)
					return nil
				})
			} else {
				if err := store.RegisterProcess(t.Context(), owner, "late-output", false); err != nil {
					t.Fatal(err)
				}
				if err := store.ProcessResult(t.Context(), owner, "late-output", nil, "running", nil, false); err != nil {
					t.Fatal(err)
				}
				if operation == reconcileOperation {
					for range 3 {
						if _, err := e.ReconcileProcesses(t.Context(), time.Second); err != nil {
							t.Fatal(err)
						}
					}
				}
				observed, readErr := e.readProcess(t.Context(), "late-output", 1000)
				err = readErr
				if err == nil {
					output, code = string(observed.output.RetainedBytes()), *observed.exitCode
				}
			}
			if err != nil || code != 23 || output != "late stdout\n" || reads != 3 {
				t.Fatalf("exit=%d output=%q reads=%d error=%v", code, output, reads, err)
			}
		})
	}
}

func TestExitedOpenProcessYieldsAndRetainsLateOutput(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	const processID = "exited-open"
	const modelID = 4242
	if err := store.RegisterModelProcess(t.Context(), owner, processID, modelID, true); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, processID, nil, executionstate.ProcessRunning, nil, false); err != nil {
		t.Fatal(err)
	}

	var late atomic.Bool
	var reads, writes atomic.Int32
	responses := make(chan *executorpb.OpenSessionResponse, 1)
	client := &execfake.Client{OnOpenSession: func(ctx context.Context, _ *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		responses <- &executorpb.OpenSessionResponse{Status: execfake.CompletedStatus(), ExecutionSessionId: "retained-executor", Shell: &executorpb.ExecutionShell{Name: "bash", Path: "/bin/bash"}}
		return &execfake.NotificationStream{Ctx: ctx, Responses: responses}, nil
	}}
	client.OnReadProcess = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads.Add(1)
		page := &executorpb.ReadProcessResponse{Status: execfake.CompletedStatus(), Exited: true, ExitCode: ptrInt32(0), NextSeq: 2}
		if late.Load() {
			page.Closed = true
			page.NextSeq = 4
			if request.AfterSeq != nil && *request.AfterSeq == 1 {
				page.Chunks = []*executorpb.ExecutionOutputChunk{{Seq: 2, Stream: "stdout", Data: []byte("late output")}}
			}
		}
		return page, nil
	}
	client.OnWriteProcess = func(context.Context, *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error) {
		writes.Add(1)
		return &executorpb.WriteProcessResponse{Status: execfake.CompletedStatus(), WriteStatus: "accepted"}, nil
	}
	e := newFakeExecutor(t, store, owner, client)

	observed, err := e.readProcess(t.Context(), processID, 30)
	if err != nil {
		t.Fatal(err)
	}
	result := observed.unifiedExecResult(ptrInt(modelID), nil, "chunk")
	if result.ExitCode == nil || *result.ExitCode != 0 || result.SessionID == nil || *result.SessionID != modelID {
		t.Fatalf("exited but open result=%+v, want exit code and an output handle", result)
	}
	if got := reads.Load(); got != 1 {
		t.Fatalf("empty exited process read %d times before the yield, want one", got)
	}
	for range 2 {
		progressed, err := e.ReconcileProcesses(t.Context(), time.Second)
		if err != nil || progressed {
			t.Fatalf("unchanged exited process progressed=%v err=%v", progressed, err)
		}
	}
	if _, err := e.writeStdin(t.Context(), json.RawMessage(`{"session_id":4242,"chars":"must not be sent","yield_time_ms":1}`)); err != nil {
		t.Fatal(err)
	}
	if writes.Load() != 0 {
		t.Fatal("sent input after the process exited")
	}

	changed := e.ProcessChanges()
	late.Store(true)
	responses <- &executorpb.OpenSessionResponse{ExecutionSessionId: "retained-executor", NativeFrames: []*executorpb.ExecutionNativeFrame{{Direction: "notification", Payload: []byte(`{"method":"process/output","params":{"processId":"exited-open","seq":2}}`)}}}
	select {
	case <-changed:
	case <-time.After(time.Second):
		t.Fatal("notification did not wake process reconciliation")
	}
	for range 2 {
		progressed, err := e.ReconcileProcesses(t.Context(), time.Second)
		if err != nil || !progressed {
			t.Fatalf("late output reconciliation progressed=%v err=%v", progressed, err)
		}
	}
	retained, err := e.writeStdin(t.Context(), json.RawMessage(`{"session_id":4242,"yield_time_ms":1}`))
	if err != nil || retained.Output != "late output" || retained.ExitCode == nil || *retained.ExitCode != 0 || retained.SessionID != nil {
		t.Fatalf("retained late output=%+v err=%v", retained, err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestCompletedReadIsJournaledAfterCallerCancellation(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	ctx, cancel := context.WithCancel(t.Context())
	client := &execfake.Client{}
	client.OnReadProcess = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		cancel()
		return &executorpb.ReadProcessResponse{
			Status:       execfake.CompletedStatus(),
			NativeFrames: []*executorpb.ExecutionNativeFrame{{Direction: "response", Payload: []byte(`{"result":{}}`)}},
		}, nil
	}
	e := newFakeExecutor(t, store, owner, client)
	operation, err := e.transport.Read(ctx, transport.ReadRequest{ProcessID: "known-result", MaxBytes: 65536})
	if err != nil || operation.Outcome != transport.Completed {
		t.Fatalf("completed read lost to caller cancellation: %v", err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
	var completed int
	if err := store.DB().QueryRowContext(t.Context(), `SELECT count(*) FROM sampler_intent WHERE conversation_id=$1 AND request->>'method'='process/read' AND result->>'outcome'='completed'`, owner.ConversationID).Scan(&completed); err != nil {
		t.Fatal(err)
	}
	if completed != 1 {
		t.Fatalf("completed read intents=%d, want one", completed)
	}
	session, err := store.GetSessionForConversation(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	events, err := store.Events(t.Context(), session.ID, 0, 4096)
	if err != nil {
		t.Fatal(err)
	}
	var responseFrames int
	encodedFrame := base64.StdEncoding.EncodeToString([]byte(`{"result":{}}`))
	for _, entry := range events {
		boundary := entry.GetEvent().GetBoundaryRecord()
		if boundary != nil && boundary.GetBoundary() == "executor" && boundary.GetDirection() == "response" && bytes.Contains(boundary.GetPayload(), []byte(encodedFrame)) {
			responseFrames++
		}
	}
	if responseFrames != 1 {
		t.Fatalf("durable native response frames=%d, want one", responseFrames)
	}
}

func TestExecutorReplacesDefinitivelyLostSessionWithoutReplayingCurrentOperation(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	if err := store.RegisterProcess(t.Context(), owner, "old-process", false); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, "old-process", nil, "running", nil, false); err != nil {
		t.Fatal(err)
	}
	if err := store.RetainProcessOutput(t.Context(), owner, "old-process", 5, []byte("observed output"), false, nil); err != nil {
		t.Fatal(err)
	}
	if err := store.CellResult(t.Context(), owner, "live-cell", "live"); err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterModelProcess(t.Context(), owner, "drained-process", 99, false); err != nil {
		t.Fatal(err)
	}
	exitCode := 0
	if err := store.RetainProcessOutput(t.Context(), owner, "drained-process", 8, []byte("final buffered output"), true, &exitCode); err != nil {
		t.Fatal(err)
	}

	client := &execfake.Client{}
	var resumes []*executorpb.OpenSessionRequest
	starts := 0
	var startedWithSession string
	client.OnOpenSession = func(ctx context.Context, request *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		resumes = append(resumes, proto.Clone(request).(*executorpb.OpenSessionRequest))
		if len(resumes) == 1 {
			return &execfake.Stream{Ctx: ctx, Initial: &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{
				Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, UpstreamCode: ptrInt32(-32600), Message: "unknown session id retained-executor",
			}}}, nil
		}
		if request.ResumeSessionId != nil {
			t.Fatalf("fresh session request resumed %q", *request.ResumeSessionId)
		}
		return &execfake.Stream{Ctx: ctx, Initial: &executorpb.OpenSessionResponse{
			Status: execfake.CompletedStatus(), ExecutionSessionId: "fresh-executor",
			Shell: &executorpb.ExecutionShell{Name: "bash", Path: "/bin/bash"},
		}}, nil
	}
	client.OnStartProcess = func(_ context.Context, request *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		startedWithSession = request.ExecutionSessionId
		return &executorpb.StartProcessResponse{Status: execfake.CompletedStatus(), ProcessId: request.ProcessId}, nil
	}
	reads := 0
	client.OnReadProcess = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads++
		if request.ProcessId == "old-process" || request.ProcessId == "drained-process" {
			t.Fatalf("retired process %q reached the fresh executor", request.ProcessId)
		}
		return &executorpb.ReadProcessResponse{Status: execfake.CompletedStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}, Exited: true, Closed: true, ExitCode: ptrInt32(0)}, nil
	}
	e := newFakeExecutor(t, store, owner, client)
	e.environment = map[string]string{}
	_, err := e.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"echo must not run","yield_time_ms":0}`))
	if err == nil || !strings.Contains(err.Error(), "fresh session was opened") || !strings.Contains(err.Error(), "not dispatched") {
		t.Fatalf("first model-facing operation error=%v, want visible not-dispatched session-loss error", err)
	}
	if starts != 0 || reads != 0 || len(resumes) != 2 || resumes[0].GetResumeSessionId() != "retained-executor" || resumes[1].ResumeSessionId != nil {
		t.Fatalf("session recovery unexpectedly replayed work: starts=%d reads=%d resumes=%+v", starts, reads, resumes)
	}
	if _, err := e.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"true","yield_time_ms":0}`)); err != nil {
		t.Fatal(err)
	}
	if starts != 1 || reads != 1 || startedWithSession != "fresh-executor" {
		t.Fatalf("subsequent operation did not use the fresh session: starts=%d reads=%d session=%q", starts, reads, startedWithSession)
	}
	buffered, err := e.writeStdin(t.Context(), json.RawMessage(`{"session_id":99,"chars":""}`))
	if err != nil || buffered.Output != "final buffered output" || buffered.ExitCode == nil || *buffered.ExitCode != 0 {
		t.Fatalf("drained output was not served locally after session loss: result=%+v error=%v", buffered, err)
	}
	process, err := store.Process(t.Context(), owner, "old-process")
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != "abandoned" || process.AfterSequence == nil || *process.AfterSequence != 5 || string(process.BufferedOutput) != "observed output" {
		t.Fatalf("old process evidence was not retained: %+v", process)
	}
	state, err := store.ExecutionState(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if len(state.Processes) != 0 || len(state.Cells) != 1 || state.Cells[0].ID != "live-cell" {
		t.Fatalf("executor loss did not retire only executor handles: %+v", state)
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if conversation.ExecutorSession == nil || *conversation.ExecutorSession != "fresh-executor" {
		t.Fatalf("stored executor session=%v, want fresh-executor", conversation.ExecutorSession)
	}
	contextJSON, err := json.Marshal(conversation.Transcript)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(contextJSON), "retained exec-server session was definitively lost") || !strings.Contains(string(contextJSON), "no mutation was replayed") {
		t.Fatalf("model-visible loss notice missing: %s", contextJSON)
	}
	if err := e.close(); err != nil {
		t.Fatal(err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestReconcileProcessWaitsForSameSessionAcrossGatewayOutage(t *testing.T) {
	store, owner, request := resumedExecutorStore(t, protocol.APIStyleResponses)
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterProcess(t.Context(), owner, "running-process", false); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, "running-process", nil, "running", nil, false); err != nil {
		t.Fatal(err)
	}
	if err := store.RetainProcessOutput(t.Context(), owner, "running-process", 5, []byte("before "), false, nil); err != nil {
		t.Fatal(err)
	}
	client := &execfake.Client{}
	var reads []*executorpb.ReadProcessRequest
	client.OnReadProcess = func(_ context.Context, r *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads = append(reads, proto.Clone(r).(*executorpb.ReadProcessRequest))
		if len(reads) == 1 {
			return nil, status.Error(codes.Unavailable, "Gateway went away after read dispatch")
		}
		return &executorpb.ReadProcessResponse{Status: execfake.CompletedStatus(), Chunks: []*executorpb.ExecutionOutputChunk{{Seq: 6, Stream: "stdout", Data: []byte("after")}}, NextSeq: 7}, nil
	}
	gatewayUnavailable := make(chan struct{})
	controlRouteUnavailable := make(chan struct{})
	restored := make(chan struct{})
	var resumes []*executorpb.OpenSessionRequest
	var starts atomic.Int32
	client.OnStartProcess = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts.Add(1)
		return nil, errors.New("retained-session recovery must not start a process")
	}
	routeUnavailableError := executionEnvironmentStatusError(t, executorpb.ExecutionEnvironmentFailure_STATE_CONTROL_ROUTE_UNAVAILABLE)
	client.OnOpenSession = func(ctx context.Context, r *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		resumes = append(resumes, proto.Clone(r).(*executorpb.OpenSessionRequest))
		if len(resumes) == 1 {
			close(gatewayUnavailable)
			return nil, status.Error(codes.Unavailable, "Gateway unavailable")
		}
		if len(resumes) == 2 {
			close(controlRouteUnavailable)
			return nil, routeUnavailableError
		}
		select {
		case <-restored:
			return &execfake.Stream{Ctx: ctx, Initial: &executorpb.OpenSessionResponse{Status: execfake.CompletedStatus(), ExecutionSessionId: "retained-executor"}}, nil
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	reconnectTimeout := 2 * time.Second
	e := newFakeExecutor(t, store, owner, client, fakeExecutorConfig{
		scope:      &executorpb.ExecutionScope{Destination: request.Executor, ExpectedIncarnationUid: "original", SessionId: request.SessionId},
		rpcTimeout: 30 * time.Millisecond, reconnectTimeout: reconnectTimeout,
	})
	establishFakeExecutorSession(t, e, client)
	result := make(chan error, 1)
	go func() {
		_, err := e.ReconcileProcesses(t.Context(), 30*time.Millisecond)
		result <- err
	}()
	<-gatewayUnavailable
	select {
	case <-controlRouteUnavailable:
	case <-time.After(reconnectTimeout):
		t.Fatal("structured control-route refusal was not retried")
	}
	process, err := store.Process(t.Context(), owner, "running-process")
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != executionstate.ProcessRunning || process.AfterSequence == nil || *process.AfterSequence != 5 || string(process.BufferedOutput) != "before " {
		t.Fatalf("transient outage abandoned or advanced the process: %+v", process)
	}
	close(restored)
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	if len(reads) != 2 || !proto.Equal(reads[0], reads[1]) || reads[0].RequestId == "" {
		t.Fatalf("read intent or cursor changed across reconnect: %+v", reads)
	}
	if len(resumes) != 3 {
		t.Fatalf("expected generic and structured resume refusals followed by one resume: %+v", resumes)
	}
	for _, resume := range resumes {
		if resume.GetResumeSessionId() != "retained-executor" || resume.Scope.GetExpectedIncarnationUid() != "original" || resume.Scope.GetDestination().GetTarget().GetUid() != "sandbox-uid" {
			t.Fatalf("resume changed native session or identity: %+v", resume)
		}
	}
	process, err = store.Process(t.Context(), owner, "running-process")
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != executionstate.ProcessRunning || process.AfterSequence == nil || *process.AfterSequence != 6 || string(process.BufferedOutput) != "before after" {
		t.Fatalf("reconnected read lost output or handle: %+v", process)
	}
	if starts.Load() != 0 {
		t.Fatalf("Gateway recovery replayed process/start %d times", starts.Load())
	}
	if err := e.close(); err != nil {
		t.Fatal(err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestProcessReadsKeepShortPerAttemptDeadlines(t *testing.T) {
	for _, tc := range []struct {
		name           string
		attemptTimeout time.Duration
		read           func(*Executor) error
	}{
		{"foreground", 5 * time.Second, func(e *Executor) error {
			_, err := e.readProcess(t.Context(), "running-process", 0)
			return err
		}},
		{"reconciliation", 50 * time.Millisecond, func(e *Executor) error {
			_, err := e.ReconcileProcesses(t.Context(), 50*time.Millisecond)
			return err
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store, owner := claimedExecutorStore(t)
			if err := store.RegisterProcess(t.Context(), owner, "running-process", false); err != nil {
				t.Fatal(err)
			}
			if err := store.ProcessResult(t.Context(), owner, "running-process", nil, "running", nil, false); err != nil {
				t.Fatal(err)
			}
			reads := 0
			client := &execfake.Client{OnReadProcess: func(ctx context.Context, _ *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
				reads++
				deadline, ok := ctx.Deadline()
				if !ok || time.Until(deadline) > tc.attemptTimeout+time.Second {
					t.Fatalf("read RPC lost its short page deadline: deadline=%v, expected within %s", deadline, tc.attemptTimeout)
				}
				return &executorpb.ReadProcessResponse{Status: execfake.CompletedStatus()}, nil
			}}
			e := newFakeExecutor(t, store, owner, client, fakeExecutorConfig{rpcTimeout: 30 * time.Second})
			if err := tc.read(e); err != nil {
				t.Fatal(err)
			}
			if reads != 1 {
				t.Fatalf("unexpected read count: %d", reads)
			}
			assertNoUnresolvedNestedIntents(t, store, owner)
		})
	}
}

func TestReadReportsDefinitiveSessionLossAfterGatewayReturns(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	if err := store.RegisterProcess(t.Context(), owner, "old-process", false); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, "old-process", nil, "running", nil, false); err != nil {
		t.Fatal(err)
	}
	if err := store.RetainProcessOutput(t.Context(), owner, "old-process", 5, []byte("already observed"), false, nil); err != nil {
		t.Fatal(err)
	}
	client := &execfake.Client{}
	reads, opens := 0, 0
	client.OnReadProcess = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads++
		return nil, status.Error(codes.Unavailable, "Gateway lost read reply")
	}
	client.OnOpenSession = func(ctx context.Context, r *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		opens++
		switch opens {
		case 1:
			if r.GetResumeSessionId() != "retained-executor" {
				t.Fatalf("first resume changed native ID: %+v", r)
			}
			return nil, status.Error(codes.Unavailable, "Gateway unavailable")
		case 2:
			if r.GetResumeSessionId() != "retained-executor" {
				t.Fatalf("second resume changed native ID: %+v", r)
			}
			return &execfake.Stream{Ctx: ctx, Initial: &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{
				Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, UpstreamCode: ptrInt32(-32600), Message: "unknown session id retained-executor",
			}}}, nil
		default:
			if r.ResumeSessionId != nil {
				t.Fatalf("fresh open tried the lost native ID: %+v", r)
			}
			return &execfake.Stream{Ctx: ctx, Initial: &executorpb.OpenSessionResponse{Status: execfake.CompletedStatus(), ExecutionSessionId: "fresh-executor"}}, nil
		}
	}
	e := newFakeExecutor(t, store, owner, client, fakeExecutorConfig{
		rpcTimeout: 30 * time.Millisecond, reconnectTimeout: 2 * time.Second,
	})
	establishFakeExecutorSession(t, e, client)
	_, err := e.ReconcileProcesses(t.Context(), 30*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	if opens != 3 || reads != 1 {
		t.Fatalf("loss recovery replayed the read or skipped fresh binding: opens=%d reads=%d", opens, reads)
	}
	process, err := store.Process(t.Context(), owner, "old-process")
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != executionstate.ProcessAbandoned || process.AfterSequence == nil || *process.AfterSequence != 5 || string(process.BufferedOutput) != "already observed" {
		t.Fatalf("loss recovery discarded process evidence: %+v", process)
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if conversation.ExecutorSession == nil || *conversation.ExecutorSession != "fresh-executor" {
		t.Fatalf("definitive loss did not bind fresh session: %+v", conversation.ExecutorSession)
	}
	transcript, err := json.Marshal(conversation.Transcript)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(string(transcript), "retained exec-server session was definitively lost") != 1 {
		t.Fatalf("model did not receive one truthful loss notice: %s", transcript)
	}
	if err := e.close(); err != nil {
		t.Fatal(err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestReconcileAbandonsProcessesAfterTransportLoss(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	for _, id := range []string{"live-process-1", "live-process-2"} {
		if err := store.RegisterProcess(t.Context(), owner, id, false); err != nil {
			t.Fatal(err)
		}
		if err := store.ProcessResult(t.Context(), owner, id, nil, "running", nil, false); err != nil {
			t.Fatal(err)
		}
		if err := store.RetainProcessOutput(t.Context(), owner, id, 4, []byte("already observed"), false, nil); err != nil {
			t.Fatal(err)
		}
	}
	client := &execfake.Client{}
	client.OnReadProcess = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		return nil, status.Error(codes.Unavailable, "executor transport failed")
	}
	client.OnOpenSession = func(context.Context, *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		return nil, status.Error(codes.Unavailable, "session resume failed")
	}
	e := newFakeExecutor(t, store, owner, client, fakeExecutorConfig{reconnectTimeout: 150 * time.Millisecond})
	establishFakeExecutorSession(t, e, client)
	for range 2 {
		progressed, err := e.ReconcileProcesses(t.Context(), time.Second)
		if err != nil || !progressed {
			t.Fatalf("progressed=%t error=%v", progressed, err)
		}
	}
	for _, id := range []string{"live-process-1", "live-process-2"} {
		process, err := store.Process(t.Context(), owner, id)
		if err != nil {
			t.Fatal(err)
		}
		if process.Status != "abandoned" || process.AfterSequence == nil || *process.AfterSequence != 4 || string(process.BufferedOutput) != "already observed" {
			t.Fatalf("abandoned process lost evidence: %+v", process)
		}
	}
	state, err := store.ExecutionState(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if len(state.Processes) != 0 {
		t.Fatalf("abandoned process remains scheduled: %+v", state.Processes)
	}
	var raw []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT result FROM sampler_intent WHERE conversation_id=$1 AND request->>'method'='process/read' ORDER BY id LIMIT 1`, owner.ConversationID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var evidence map[string]any
	if err := json.Unmarshal(raw, &evidence); err != nil {
		t.Fatal(err)
	}
	if evidence["remote_outcome"] != "unknown" || evidence["automatic_replay"] != false {
		t.Fatalf("transport result evidence=%+v", evidence)
	}
}

func claimedExecutorStore(t *testing.T) (*sessionstore.Store, sessionstore.Owner) {
	t.Helper()
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	return store, owner
}

type fakeExecutorConfig struct {
	scope             *executorpb.ExecutionScope
	rpcTimeout        time.Duration
	initializeTimeout time.Duration
	reconnectTimeout  time.Duration
}

func newFakeExecutor(t *testing.T, store *sessionstore.Store, owner sessionstore.Owner, client *execfake.Client, configs ...fakeExecutorConfig) *Executor {
	t.Helper()
	if client.OnOpenSession == nil {
		client.OnOpenSession = func(ctx context.Context, request *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
			sessionID := request.GetResumeSessionId()
			if sessionID == "" {
				sessionID = "retained-executor"
			}
			return &execfake.Stream{Ctx: ctx, Initial: &executorpb.OpenSessionResponse{
				Status: execfake.CompletedStatus(), ExecutionSessionId: sessionID,
				Shell: &executorpb.ExecutionShell{Name: "bash", Path: "/bin/bash"},
			}}, nil
		}
	}
	var config fakeExecutorConfig
	if len(configs) != 0 {
		config = configs[0]
	}
	if config.scope == nil {
		config.scope = &executorpb.ExecutionScope{SessionId: owner.ConversationID}
	}
	if config.initializeTimeout == 0 {
		config.initializeTimeout = time.Minute
	}
	if config.reconnectTimeout == 0 {
		config.reconnectTimeout = 2 * time.Minute
	}
	state, err := store.ExecutionState(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	needsRemote := false
	for _, process := range state.Processes {
		if process.Status != executionstate.ProcessExited && process.Status != executionstate.ProcessExitedUnread && process.Status != executionstate.ProcessAbandoned {
			needsRemote = true
			break
		}
	}
	initialSessionID := ""
	if needsRemote {
		conversation, err := store.Read(t.Context(), owner)
		if err != nil {
			t.Fatal(err)
		}
		if conversation.ExecutorSession != nil {
			initialSessionID = *conversation.ExecutorSession
		}
	}
	processChanges := &processChangeBroadcaster{}
	executor := &Executor{
		store: store, owner: owner, cwd: "/tmp", processLocks: map[string]*sync.Mutex{}, processChanges: processChanges,
	}
	executor.workspacePrepared.Store(true)
	bindings := &storeBindings{store: store, owner: owner, workspacePrepared: &executor.workspacePrepared}
	executor.transport = transport.New(transport.Config{
		Client: client, Journal: bindings, Bindings: bindings, ProcessChanges: processChanges,
		Scope: config.scope, Lifetime: t.Context(), InitialSessionID: initialSessionID,
		RPCTimeout: config.rpcTimeout, InitializeTimeout: config.initializeTimeout, ReconnectTimeout: config.reconnectTimeout,
	})
	t.Cleanup(func() { _ = executor.close() })
	return executor
}

// Establish the live session before a test simulates losing its read reply.
func establishFakeExecutorSession(t *testing.T, e *Executor, client *execfake.Client) {
	t.Helper()
	recoverSession := client.OnOpenSession
	client.OnOpenSession = func(ctx context.Context, request *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		return &execfake.Stream{Ctx: ctx, Initial: &executorpb.OpenSessionResponse{
			Status: execfake.CompletedStatus(), ExecutionSessionId: request.GetResumeSessionId(),
		}}, nil
	}
	_, err := operationValue(e.transport.EnsureSession(t.Context()))
	client.OnOpenSession = recoverSession
	if err != nil {
		t.Fatal(err)
	}
}

func ptrInt32(value int32) *int32 { return &value }
func ptrInt(value int) *int       { return &value }

func assertNoUnresolvedNestedIntents(t *testing.T, store *sessionstore.Store, owner sessionstore.Owner) {
	t.Helper()
	var count int
	if err := store.DB().QueryRowContext(t.Context(), "SELECT count(*) FROM sampler_intent WHERE conversation_id=$1 AND result IS NULL", owner.ConversationID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("unresolved nested intents=%d", count)
	}
}

func TestRetainedTerminalOutputCanBeConsumedAfterTakeover(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterModelProcess(t.Context(), owner, "retained", 4242, false); err != nil {
		t.Fatal(err)
	}
	if err := store.RetainProcessOutput(t.Context(), owner, "retained", 7, []byte("retained-output"), true, ptrInt(0)); err != nil {
		t.Fatal(err)
	}
	if err := store.Release(t.Context(), owner); err != nil {
		t.Fatal(err)
	}
	successor, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), successor, "retained", nil, "running", nil, false); !errors.As(err, new(*sessionstore.LostOwnershipError)) {
		t.Fatalf("successor changed old remote process state: %v", err)
	}
	executor := &Executor{store: store, owner: successor, processLocks: map[string]*sync.Mutex{}, processChanges: &processChangeBroadcaster{}}
	invoked, err := executor.Invoke(t.Context(), "write_stdin", json.RawMessage(`{"session_id":4242}`))
	result, _ := invoked.(*unifiedExecResult)
	if err != nil || result == nil || result.Output != "retained-output" || result.ExitCode == nil || *result.ExitCode != 0 || result.SessionID != nil {
		t.Fatalf("retained process result=%v err=%v", result, err)
	}
	process, err := store.Process(t.Context(), successor, "retained")
	if err != nil || process.Status != "exited" || len(process.BufferedOutput) != 0 {
		t.Fatalf("consumed process=%+v err=%v", process, err)
	}
}
