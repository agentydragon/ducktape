package centralsampler

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/outputbuffer"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestExecutorRejectsMalformedInputBeforeDispatch(t *testing.T) {
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
			executor := &Executor{shell: &execShell{Name: "bash", Path: "/bin/bash"}}
			_, err := executor.Invoke(context.Background(), tc.tool, json.RawMessage(tc.args))
			if !errors.As(err, new(*toolError)) {
				t.Fatalf("malformed input error=%v, want a tool error", err)
			}
		})
	}
}

func TestExecutorInitializationWaitIsIndependentOfGeneralRPCTimeout(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &fakeExecutionClient{}
	stream := &gatedExecutorStream{
		started: make(chan struct{}), release: make(chan struct{}), finished: make(chan struct{}),
		response: &executorpb.OpenSessionResponse{Status: completedExecutionStatus(), ExecutionSessionId: "retained-executor"},
	}
	client.open = func(ctx context.Context, _ *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		stream.ctx = ctx
		return stream, nil
	}
	executor := newFakeExecutor(t, store, owner, client)
	executor.rpcTimeout = 5 * time.Millisecond
	executor.initializeTimeout = time.Minute
	result := make(chan error, 1)
	go func() { result <- executor.openSession(t.Context(), nil) }()
	<-stream.started
	select {
	case err := <-result:
		t.Fatalf("executor initialization ended at the general RPC timeout: %v", err)
	case <-stream.ctx.Done():
		t.Fatalf("executor initialization stream ended at the general RPC timeout: %v", stream.ctx.Err())
	case <-time.After(50 * time.Millisecond):
	}
	close(stream.release)
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	<-stream.finished
	if err := executor.close(); err != nil {
		t.Fatal(err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestExecutorInitializationTimeoutKeepsUnknownOutcome(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &fakeExecutionClient{}
	stream := &gatedExecutorStream{started: make(chan struct{}), release: make(chan struct{}), finished: make(chan struct{})}
	client.open = func(ctx context.Context, _ *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		stream.ctx = ctx
		return stream, nil
	}
	executor := newFakeExecutor(t, store, owner, client)
	executor.rpcTimeout = time.Minute
	executor.initializeTimeout = 5 * time.Millisecond
	result := make(chan error, 1)
	go func() { result <- executor.openSession(t.Context(), nil) }()
	<-stream.started
	var transport *executorTransportError
	select {
	case err := <-result:
		if !errors.As(err, &transport) || transport.phase != "receive" {
			t.Fatalf("executor initialization error=%v, want unknown receive outcome", err)
		}
	case <-time.After(time.Second):
		t.Fatal("executor initialization did not honor its configured timeout")
	}
	close(stream.release)
	<-stream.finished
	var raw []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT result FROM sampler_intent WHERE conversation_id=$1 AND request->>'method'='initialize' ORDER BY id LIMIT 1`, owner.ConversationID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var evidence map[string]any
	if err := json.Unmarshal(raw, &evidence); err != nil {
		t.Fatal(err)
	}
	if evidence["remote_outcome"] != "unknown" || evidence["automatic_replay"] != false || evidence["phase"] != "receive" {
		t.Fatalf("initialization timeout evidence=%+v", evidence)
	}
	if err := executor.close(); err != nil {
		t.Fatal(err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestOpenSessionTransportFailurePreservesFailurePhase(t *testing.T) {
	for _, test := range []struct {
		name  string
		phase string
	}{
		{name: "open", phase: "open"},
		{name: "receive", phase: "receive"},
	} {
		t.Run(test.name, func(t *testing.T) {
			store, owner := claimedExecutorStore(t)
			client := &fakeExecutionClient{}
			failure := status.Error(codes.Unavailable, "executor connection lost")
			client.open = func(context.Context, *executorpb.OpenSessionRequest) (executorSessionStream, error) {
				if test.phase == "open" {
					return nil, failure
				}
				return failedExecutionStream{err: failure}, nil
			}
			executor := newFakeExecutor(t, store, owner, client)
			var transport *executorTransportError
			if err := executor.openSession(t.Context(), nil); !errors.As(err, &transport) || transport.phase != test.phase {
				t.Fatalf("OpenSession error=%v, want transport phase %q", err, test.phase)
			}
			var raw []byte
			if err := store.DB().QueryRowContext(t.Context(), `SELECT result FROM sampler_intent WHERE conversation_id=$1 AND request->>'method'='initialize' ORDER BY id LIMIT 1`, owner.ConversationID).Scan(&raw); err != nil {
				t.Fatal(err)
			}
			var evidence map[string]any
			if err := json.Unmarshal(raw, &evidence); err != nil {
				t.Fatal(err)
			}
			if evidence["phase"] != test.phase || evidence["remote_outcome"] != "unknown" || evidence["automatic_replay"] != false {
				t.Fatalf("OpenSession evidence=%+v, want unknown non-replayed %s failure", evidence, test.phase)
			}
			assertNoUnresolvedNestedIntents(t, store, owner)
		})
	}
}

func TestOpenSessionEnvironmentFailureIsCatchableAndNotDispatched(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &fakeExecutionClient{}
	opens, starts := 0, 0
	client.open = func(context.Context, *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		opens++
		return nil, executionEnvironmentStatusError(t, executorpb.ExecutionEnvironmentFailure_STATE_SUSPENDED)
	}
	client.start = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		return &executorpb.StartProcessResponse{Status: completedExecutionStatus()}, nil
	}
	e := newFakeExecutor(t, store, owner, client)
	e.shell = &execShell{Name: "bash", Path: "/bin/bash"}
	e.sessionID = ""
	e.environment = map[string]string{}

	result, err := e.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"echo must not run"}`))
	var outcome *toolOutcomeError
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
	if e.sessionError != nil {
		t.Fatalf("catchable environment failure poisoned the executor Session: %v", e.sessionError)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestTypedStartEnvironmentFailureIsNotReplayed(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &fakeExecutionClient{}
	starts := 0
	client.start = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		return nil, executionEnvironmentStatusError(t, executorpb.ExecutionEnvironmentFailure_STATE_NOT_READY)
	}
	e := newFakeExecutor(t, store, owner, client)
	e.shell = &execShell{Name: "bash", Path: "/bin/bash"}
	e.environment = map[string]string{}

	_, err := e.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"touch should-not-run"}`))
	var outcome *toolOutcomeError
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
	if processStatus != string(protocol.ProcessExited) {
		t.Fatalf("known not-dispatched process status=%q, want exited", processStatus)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestTypedReadEnvironmentFailurePreservesUnknownProcessEvidence(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	registerRunningModelProcess(t, store, owner, "suspended-process", 1400, 8, "output before suspension\n")
	client := &fakeExecutionClient{}
	reads := 0
	client.read = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads++
		return nil, executionEnvironmentStatusError(t, executorpb.ExecutionEnvironmentFailure_STATE_SUSPENDED)
	}
	e := newFakeExecutor(t, store, owner, client)

	_, err := e.Invoke(t.Context(), "write_stdin", json.RawMessage(`{"session_id":1400}`))
	var outcome *toolOutcomeError
	if !errors.As(err, &outcome) {
		t.Fatalf("write_stdin error=%v, want catchable environment refusal", err)
	}
	outerEvidence, ok := outcome.evidence.(executionFailureEvidence)
	if !ok || outerEvidence.RemoteOutcome != NestedUnknown || outerEvidence.RequestOutcome == nil || *outerEvidence.RequestOutcome != NestedNotDispatched ||
		outerEvidence.AfterSeq == nil || *outerEvidence.AfterSeq != 8 || outerEvidence.BufferedOutputBase64 == nil ||
		!bytes.Equal(*outerEvidence.BufferedOutputBase64, []byte("output before suspension\n")) {
		t.Fatalf("outer tool failure lost typed unknown-process evidence: %+v", outcome.evidence)
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
	if process.Status != protocol.ProcessAbandoned || process.AfterSequence == nil || *process.AfterSequence != 8 ||
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
	executor := newFakeExecutor(t, store, owner, &fakeExecutionClient{})
	err := executor.outcomeToolError(t.Context(), &executorNotDispatchedError{
		method: executorMethodProcessStart, processID: "not-dispatched-start", retainedSession: "retained-executor",
		afterSequence: &zero, output: output, cause: errors.New("executor session unavailable"),
	})
	var outcome *toolOutcomeError
	if !errors.As(err, &outcome) {
		t.Fatalf("not-dispatched start error=%v, want a catchable tool outcome", err)
	}
	evidence, ok := outcome.evidence.(executionFailureEvidence)
	if !ok || evidence.RemoteOutcome != NestedNotDispatched || evidence.RequestOutcome == nil || *evidence.RequestOutcome != NestedNotDispatched ||
		evidence.AfterSeq == nil || *evidence.AfterSeq != 0 || evidence.BufferedOutput == nil || *evidence.BufferedOutput != "" ||
		evidence.BufferedOutputBase64 == nil || *evidence.BufferedOutputBase64 == nil || len(*evidence.BufferedOutputBase64) != 0 ||
		evidence.TransportError != "executor session unavailable" {
		t.Fatalf("not-dispatched start lost typed zero/empty evidence: %+v", outcome.evidence)
	}
	process, err := store.Process(t.Context(), owner, "not-dispatched-start")
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != protocol.ProcessExited || process.AfterSequence == nil || *process.AfterSequence != 0 {
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
		method: executorMethodProcessRead, remoteOutcome: NestedUnknown,
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
	emptyEvidence := executionFailureRecord{method: executorMethodProcessRead, afterSequence: &zero, output: empty}.toolEvidence()
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

	absentJSON, err := json.Marshal(executionFailureRecord{method: executorMethodProcessRead, afterSequence: &zero}.toolEvidence())
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
	if err := store.ProcessResult(t.Context(), owner, "refused-write-process", nil, protocol.ProcessRunning, nil, false); err != nil {
		t.Fatal(err)
	}
	client := &fakeExecutionClient{}
	writes := 0
	client.write = func(context.Context, *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error) {
		writes++
		return nil, executionEnvironmentStatusError(t, executorpb.ExecutionEnvironmentFailure_STATE_CONTROL_ROUTE_UNAVAILABLE)
	}
	e := newFakeExecutor(t, store, owner, client)

	_, err := e.Invoke(t.Context(), "write_stdin", json.RawMessage(`{"session_id":1401,"chars":"must not be sent"}`))
	var outcome *toolOutcomeError
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
	if err != nil || process.Status != protocol.ProcessRunning {
		t.Fatalf("not-dispatched write changed process state: process=%+v err=%v", process, err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestApplyPatchStartRejectionIsReturnedAsNestedToolFailure(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &fakeExecutionClient{}
	client.start = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		return &executorpb.StartProcessResponse{Status: rejectedExecutionStatus("patch rejected")}, nil
	}
	e := newFakeExecutor(t, store, owner, client)

	_, err := e.Invoke(t.Context(), "apply_patch", json.RawMessage(`"*** Begin Patch\n*** End Patch"`))
	if !errors.As(err, new(*toolError)) {
		t.Fatalf("apply_patch error=%v, want a rejected nested tool promise", err)
	}
	if errors.As(err, new(*toolOutcomeError)) {
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
	client := &fakeExecutionClient{}
	reads := 0
	client.read = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		if request.ProcessId != "expired-native-process" {
			t.Fatalf("read process ID=%q", request.ProcessId)
		}
		reads++
		if reads == 1 {
			return &executorpb.ReadProcessResponse{
				Status:  completedExecutionStatus(),
				Chunks:  []*executorpb.ExecutionOutputChunk{{Seq: 8, Stream: "stdout", Data: []byte("new output")}},
				NextSeq: 9,
			}, nil
		}
		return &executorpb.ReadProcessResponse{Status: rejectedExecutionStatus("process unavailable")}, nil
	}
	e := newFakeExecutor(t, store, owner, client)

	_, err := e.Invoke(t.Context(), "write_stdin", json.RawMessage(`{"session_id":1200}`))
	if !errors.As(err, new(*toolError)) {
		t.Fatalf("write_stdin error=%v, want a rejected nested tool promise", err)
	}
	var outcome *toolOutcomeError
	if !errors.As(err, &outcome) {
		t.Fatalf("missing process rejection evidence: %v", err)
	}
	evidence, ok := outcome.evidence.(rejectedProcessReadEvidence)
	if !ok || evidence.RemoteOutcome != NestedUnknown || evidence.AutomaticReplay {
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
	client := &fakeExecutionClient{}
	client.read = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads[request.ProcessId]++
		if request.ProcessId == "expired-native-process" {
			return &executorpb.ReadProcessResponse{Status: rejectedExecutionStatus("process unavailable")}, nil
		}
		return &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}}, nil
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
		client := &fakeExecutionClient{}
		client.start = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
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
		client := &fakeExecutionClient{}
		client.start = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
			return &executorpb.StartProcessResponse{Status: rejectedExecutionStatus("command rejected")}, nil
		}
		e := newFakeExecutor(t, store, owner, client)
		e.environment = map[string]string{}
		e.shell = &execShell{Name: "bash", Path: "/bin/bash"}
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
	if errors.As(err, new(*toolError)) || errors.As(err, new(*executorRejectedError)) {
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

func registerRunningModelProcess(t *testing.T, store *Store, owner Owner, id string, sessionID int, after uint64, output string) {
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

func TestTypedReadResponseKeepsEmptyChunksAsAnArray(t *testing.T) {
	response := &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}}
	value, err := responseObject(executorMethodProcessRead, response)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	var result executorReadResult
	if err := json.Unmarshal(encoded, &result); err != nil {
		t.Fatal(err)
	}
	if result.Chunks == nil {
		t.Fatalf("empty chunks decoded as nil: %s", encoded)
	}
}

func TestTypedReadResponsesKeepEmptyBytesAsEmptyStrings(t *testing.T) {
	readProcess, err := responseObject(executorMethodProcessRead, &executorpb.ReadProcessResponse{
		Status: completedExecutionStatus(),
		Chunks: []*executorpb.ExecutionOutputChunk{{Seq: 1, Stream: "stdout"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if chunks, ok := readProcess["chunks"].([]any); !ok || len(chunks) != 1 || chunks[0].(map[string]any)["chunk"] != "" {
		t.Fatalf("empty process output chunk=%+v, want base64 empty string", readProcess["chunks"])
	}

	readFile, err := responseObject(executorMethodFSReadBlock, &executorpb.ReadFileResponse{Status: completedExecutionStatus(), Eof: true})
	if err != nil {
		t.Fatal(err)
	}
	if readFile["chunk"] != "" {
		t.Fatalf("empty file block=%+v, want base64 empty string", readFile)
	}
}

func TestReadFileReturnsBytesWhileJournalingOnlyDigest(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	chunk := []byte("opaque image block")
	client := &fakeExecutionClient{}
	client.readFile = func(context.Context, *executorpb.ReadFileRequest) (*executorpb.ReadFileResponse, error) {
		return &executorpb.ReadFileResponse{Status: completedExecutionStatus(), Data: chunk, Eof: true}, nil
	}
	e := newFakeExecutor(t, store, owner, client)
	result, err := executorRPCWithEvidence[executorFSBlock](t.Context(), e, executorMethodFSReadBlock, executorFSReadBlock{HandleID: "image", Len: len(chunk)}, blockEvidence)
	if err != nil {
		t.Fatal(err)
	}
	if string(result.Chunk) != string(chunk) || !result.EOF {
		t.Fatalf("RPC result=%+v, want original bytes and EOF", result)
	}
	var raw []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT result FROM sampler_intent WHERE conversation_id=$1 AND request->>'method'='fs/readBlock' ORDER BY id LIMIT 1`, owner.ConversationID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var evidence map[string]any
	if err := json.Unmarshal(raw, &evidence); err != nil {
		t.Fatal(err)
	}
	encoded := base64.StdEncoding.EncodeToString(chunk)
	if strings.Contains(string(raw), encoded) || evidence["response"] != nil {
		t.Fatalf("intent leaked block bytes or wrapped the prior result shape: %s", raw)
	}
	chunkEvidence, ok := evidence["chunk"].(map[string]any)
	if !ok || chunkEvidence["base64_bytes"] != float64(len(encoded)) {
		t.Fatalf("intent chunk evidence=%+v", evidence["chunk"])
	}
	digest := sha256.Sum256([]byte(encoded))
	if chunkEvidence["base64_sha256"] != hex.EncodeToString(digest[:]) || evidence["outcome"] != "completed" {
		t.Fatalf("intent result evidence=%+v", evidence)
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
	client := &fakeExecutionClient{
		write: func(context.Context, *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error) {
			sends++
			return nil, errors.New("abandoned process unexpectedly attempted a write")
		},
		read: func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
			t.Fatal("abandoned process unexpectedly attempted a read")
			return nil, nil
		},
	}
	executor := newFakeExecutor(t, store, newOwner, client)
	result, err := executor.writeStdin(t.Context(), json.RawMessage(`{"session_id":77,"chars":"must not be sent"}`))
	if result != nil || !errors.As(err, new(*toolError)) || !strings.Contains(err.Error(), "no input was sent") {
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
	executor := newFakeExecutor(t, store, owner, &fakeExecutionClient{})
	handled := executor.outcomeToolError(t.Context(), &executorNotDispatchedError{
		method: executorMethodInitialize, cause: errors.New("connection refused"),
	})
	var outcome *toolOutcomeError
	if !errors.As(handled, &outcome) {
		t.Fatalf("not-dispatched error=%v, want structured tool error", handled)
	}
	evidence, ok := outcome.evidence.(executionFailureEvidence)
	if !ok {
		t.Fatalf("not-dispatched evidence has type %T, want executionFailureEvidence", outcome.evidence)
	}
	if evidence.Method != executorMethodInitialize || evidence.RemoteOutcome != NestedNotDispatched ||
		evidence.RequestOutcome == nil || *evidence.RequestOutcome != NestedNotDispatched ||
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
	if err := store.ProcessResult(t.Context(), owner, "observed-process", nil, protocol.ProcessRunning, nil, false); err != nil {
		t.Fatal(err)
	}
	output := outputbuffer.New(outputbuffer.MaxBytes)
	output.Append([]byte("stdout before failure\n"))
	cursor := uint64(8)
	transport := &executorTransportError{
		method: executorMethodProcessRead, phase: "grpc", intentID: "read-intent", processID: "observed-process",
		retainedSession: "retained-executor", afterSequence: &cursor, output: output,
		cause: status.Error(codes.Unavailable, "read reply lost"),
	}
	executor := newFakeExecutor(t, store, owner, &fakeExecutionClient{})
	err := executor.outcomeToolError(t.Context(), transport)
	var rejected *toolOutcomeError
	if !errors.As(err, &rejected) {
		t.Fatalf("observed read failure=%v, want nested promise rejection", err)
	}
	var message observedProcessFailure
	if err := json.Unmarshal([]byte(rejected.Error()), &message); err != nil {
		t.Fatalf("rejected promise is not parseable JSON: %v: %s", err, rejected.Error())
	}
	if message.RemoteOutcome != NestedUnknown || message.AutomaticReplay || message.Method != executorMethodProcessRead ||
		message.IntentID != "read-intent" || message.ProcessID != "observed-process" || message.AfterSeq == nil || *message.AfterSeq != cursor ||
		message.ObservedOutput == nil || message.ObservedOutput.Text != "stdout before failure\n" || message.ObservedOutput.Truncated ||
		!strings.Contains(message.Message, "read reply lost") || !strings.Contains(message.Message, "mutation was not replayed") {
		t.Fatalf("observed read rejection=%+v", message)
	}
	process, err := store.Process(t.Context(), owner, "observed-process")
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != protocol.ProcessAbandoned || process.AfterSequence == nil || *process.AfterSequence != cursor ||
		string(process.BufferedOutput) != "stdout before failure\n" {
		t.Fatalf("observed output/cursor were not retained: %+v", process)
	}
	evidence, ok := rejected.evidence.(executionFailureEvidence)
	if !ok || evidence.BufferedOutputBase64 == nil || *evidence.BufferedOutputBase64 == nil ||
		!bytes.Equal(*evidence.BufferedOutputBase64, output.RetainedBytes()) || evidence.OmittedBytes == nil || *evidence.OmittedBytes != 0 {
		t.Fatalf("full output is missing from FollowEvents evidence: %+v", rejected.evidence)
	}
}

func TestExecutionFailureEvidencePreservesZeroCursorAndPresentEmptyOutput(t *testing.T) {
	cursor := uint64(0)
	requestOutcome := NestedNotDispatched
	record := executionFailureRecord{
		message: "stale read", method: executorMethodProcessRead, intentID: "read-intent",
		processID: "process-1", executionSessionID: "session-1", afterSequence: &cursor,
		remoteOutcome: NestedUnknown, requestOutcome: &requestOutcome, automaticReplay: false,
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
		modelFailure.RequestOutcome == nil || *modelFailure.RequestOutcome != NestedNotDispatched || modelFailure.ObservedOutput != nil {
		t.Fatalf("JS-facing typed failure lost cursor/outcome or fabricated output: %+v", modelFailure)
	}
}

func TestExecutionFailureEvidencePreservesFullWidthCursorAtJSONBoundary(t *testing.T) {
	cursor := ^uint64(0)
	record := executionFailureRecord{
		method: executorMethodProcessRead, afterSequence: &cursor, remoteOutcome: NestedUnknown,
		automaticReplay: false,
	}
	wire, err := json.Marshal(record.intentEvidence())
	if err != nil {
		t.Fatal(err)
	}
	var decoded executionFailureEvidence
	if err := json.Unmarshal(wire, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.AfterSeq == nil || *decoded.AfterSeq != cursor {
		t.Fatalf("full-width cursor did not survive JSON boundary: wire=%s decoded=%+v", wire, decoded)
	}
}

func TestSessionNotificationJournalSurvivesStreamCancellationAndFencesStaleOwner(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	frame := &executorpb.ExecutionNativeFrame{Direction: "notification", Payload: []byte("native notification")}
	journal := func(owner Owner, operationID string) error {
		t.Helper()
		streamCtx := &cancelDuringJournalContext{Context: context.Background(), done: make(chan struct{})}
		close(streamCtx.done)
		stream := &fakeExecutionStream{ctx: streamCtx, initial: &executorpb.OpenSessionResponse{NativeFrames: []*executorpb.ExecutionNativeFrame{frame}}}
		executor := newFakeExecutor(t, store, owner, &fakeExecutionClient{})
		executor.sessionGeneration = 1
		done := make(chan struct{})
		executor.watchSession(streamCtx, stream, operationID, "retained-executor", 1, func() {}, done)
		<-done
		return executor.sessionError
	}
	if err := journal(owner, "active-notification"); err != nil {
		t.Fatalf("received notification lost to stream cancellation: %v", err)
	}
	if err := store.Release(t.Context(), owner); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Claim(t.Context(), session.ConversationID, time.Minute); err != nil {
		t.Fatal(err)
	}
	var lost *LostOwnershipError
	if err := journal(owner, "stale-notification"); !errors.As(err, &lost) {
		t.Fatalf("stale watcher journal error=%v, want owner fence", err)
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
		case "active-notification":
			active++
		case "stale-notification":
			stale++
		}
	}
	if active != 1 || stale != 0 {
		t.Fatalf("native notification journal active=%d stale=%d, want one durable active frame and no stale frame", active, stale)
	}
}

// The first Err check models a stream cancellation just after the caller checked
// the context and before the evidence transaction begins.
type cancelDuringJournalContext struct {
	context.Context
	checks atomic.Uint32
	done   chan struct{}
}

func (c *cancelDuringJournalContext) Err() error {
	if c.checks.Add(1) == 1 {
		return nil
	}
	return context.Canceled
}

func (c *cancelDuringJournalContext) Done() <-chan struct{} { return c.done }

func TestObservedProcessFailureMarksBothOutputTruncationLayers(t *testing.T) {
	output := outputbuffer.New(16 * 1024)
	output.Append(bytes.Repeat([]byte("a"), 32*1024))
	cursor := uint64(12)
	message, err := observedProcessFailureMessage(&executorTransportError{
		method: executorMethodProcessRead, phase: "grpc", intentID: "read-intent", processID: "observed-process",
		afterSequence: &cursor, output: output, cause: errors.New("connection closed"),
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
	transport := &executorTransportError{
		method: executorMethodProcessRead, phase: "grpc", intentID: "read-intent", processID: "observed-process",
		afterSequence: &cursor, output: output, cause: errors.New("connection closed"),
	}
	evidence := transport.evidence("")
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
			client := &fakeExecutionClient{}
			var e *Executor
			client.start = func(_ context.Context, request *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
				return &executorpb.StartProcessResponse{Status: completedExecutionStatus(), ProcessId: request.ProcessId}, nil
			}
			client.read = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
				if reads == 0 && request.AfterSeq != nil || reads > 0 && (request.AfterSeq == nil || *request.AfterSeq != uint64(reads)) {
					t.Fatalf("read %d cursor=%v", reads, request.AfterSeq)
				}
				page := &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}, Exited: true, ExitCode: ptrInt32(23), NextSeq: uint64(reads + 2)}
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
	if err := store.ProcessResult(t.Context(), owner, processID, nil, protocol.ProcessRunning, nil, false); err != nil {
		t.Fatal(err)
	}

	var late atomic.Bool
	var reads, writes atomic.Int32
	client := &fakeExecutionClient{}
	client.read = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads.Add(1)
		page := &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Exited: true, ExitCode: ptrInt32(0), NextSeq: 2}
		if late.Load() {
			page.Closed = true
			page.NextSeq = 4
			if request.AfterSeq != nil && *request.AfterSeq == 1 {
				page.Chunks = []*executorpb.ExecutionOutputChunk{{Seq: 2, Stream: "stdout", Data: []byte("late output")}}
			}
		}
		return page, nil
	}
	client.write = func(context.Context, *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error) {
		writes.Add(1)
		return &executorpb.WriteProcessResponse{Status: completedExecutionStatus(), WriteStatus: "accepted"}, nil
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

	streamCtx, cancel := context.WithCancel(t.Context())
	stream := &notificationExecutorStream{ctx: streamCtx, responses: make(chan *executorpb.OpenSessionResponse, 1)}
	e.sessionGeneration = 1
	done := make(chan struct{})
	go e.watchSession(streamCtx, stream, "notification", "retained-executor", 1, cancel, done)
	defer func() { cancel(); <-done }()
	changed := e.processChanges()
	late.Store(true)
	stream.responses <- &executorpb.OpenSessionResponse{ExecutionSessionId: "retained-executor", NativeFrames: []*executorpb.ExecutionNativeFrame{{Direction: "notification", Payload: []byte(`{"method":"process/output","params":{"processId":"exited-open","seq":2}}`)}}}
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
	client := &fakeExecutionClient{}
	client.read = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		cancel()
		return &executorpb.ReadProcessResponse{
			Status:       completedExecutionStatus(),
			NativeFrames: []*executorpb.ExecutionNativeFrame{{Direction: "response", Payload: []byte(`{"result":{}}`)}},
		}, nil
	}
	e := newFakeExecutor(t, store, owner, client)
	if _, err := executorRPC[executorReadResult](ctx, e, executorMethodProcessRead, executorRead{ProcessID: "known-result"}); err != nil {
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

func TestExecutorRetriesOnlyCursorReadWithSameIdentity(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &fakeExecutionClient{}
	var reads []*executorpb.ReadProcessRequest
	client.read = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads = append(reads, proto.Clone(request).(*executorpb.ReadProcessRequest))
		if len(reads) == 1 {
			return nil, status.Error(codes.Unavailable, "reply lost after dispatch")
		}
		return &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}, NextSeq: 8, Exited: true, ExitCode: ptrInt32(0), Closed: true}, nil
	}
	var resumed []*executorpb.OpenSessionRequest
	client.open = func(ctx context.Context, request *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		resumed = append(resumed, proto.Clone(request).(*executorpb.OpenSessionRequest))
		return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{Status: completedExecutionStatus(), ExecutionSessionId: "retained-executor"}}, nil
	}
	e := newFakeExecutor(t, store, owner, client)
	e.reconnectRequired = false
	cursor := uint64(7)
	_, err := executorRPC[executorReadResult](t.Context(), e, executorMethodProcessRead, executorRead{ProcessID: "existing-process", AfterSeq: &cursor, MaxBytes: 65536})
	if err != nil {
		t.Fatal(err)
	}
	if len(reads) != 2 || !proto.Equal(reads[0], reads[1]) {
		t.Fatalf("cursor read changed on retry: %+v", reads)
	}
	if reads[0].RequestId == "" || resumed[0].GetResumeSessionId() != "retained-executor" {
		t.Fatalf("retry ID/session not retained: reads=%+v resumed=%+v", reads, resumed)
	}
	if err := e.close(); err != nil {
		t.Fatal(err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestExecutorRetriesUnknownCursorReadAtMostOnce(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &fakeExecutionClient{}
	var reads []*executorpb.ReadProcessRequest
	client.read = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads = append(reads, proto.Clone(request).(*executorpb.ReadProcessRequest))
		return &executorpb.ReadProcessResponse{
			Status: &executorpb.ExecutionStatus{
				Outcome: executorpb.ExecutionStatus_OUTCOME_UNKNOWN,
				Message: "adapter could not recover the read outcome",
			},
		}, nil
	}
	opens := 0
	client.open = func(ctx context.Context, _ *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		opens++
		return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{Status: completedExecutionStatus(), ExecutionSessionId: "retained-executor"}}, nil
	}
	e := newFakeExecutor(t, store, owner, client)
	cursor := uint64(5)
	_, err := executorRPC[executorReadResult](t.Context(), e, executorMethodProcessRead, executorRead{ProcessID: "existing-process", AfterSeq: &cursor, MaxBytes: 65536})
	if !errors.As(err, new(*executorTransportError)) {
		t.Fatalf("read error=%v, want an explicit unknown result", err)
	}
	if len(reads) != 2 || opens != 1 || !proto.Equal(reads[0], reads[1]) {
		t.Fatalf("read retry count=%d opens=%d requests=%+v", len(reads), opens, reads)
	}
	if err := e.close(); err != nil {
		t.Fatal(err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestExecutorDoesNotReplayMutationAfterLostReply(t *testing.T) {
	for _, method := range []executionMethod{executorMethodProcessStart, executorMethodProcessWrite} {
		t.Run(string(method), func(t *testing.T) {
			store, owner := claimedExecutorStore(t)
			client := &fakeExecutionClient{}
			starts, writes := 0, 0
			client.start = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
				starts++
				return nil, status.Error(codes.Unavailable, "lost mutation reply")
			}
			client.write = func(context.Context, *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error) {
				writes++
				return nil, status.Error(codes.Unavailable, "lost mutation reply")
			}
			e := newFakeExecutor(t, store, owner, client)
			var params any
			if method == executorMethodProcessStart {
				params = executorStart{ProcessID: "lost-start", Argv: []string{"touch", "once"}, Cwd: "file:///tmp"}
			} else {
				params = executorWrite{ProcessID: "existing", Chunk: []byte("x"), WriteID: "write-once"}
			}
			_, err := e.rpc(t.Context(), method, params, nil)
			var transport *executorTransportError
			if !errors.As(err, &transport) || !strings.Contains(transport.Error(), "remote effect outcome is unknown") {
				t.Fatalf("mutation result=%v", err)
			}
			if starts != boolInt(method == executorMethodProcessStart) || writes != boolInt(method == executorMethodProcessWrite) {
				t.Fatalf("mutation replayed: starts=%d writes=%d", starts, writes)
			}
			if transport.intentID == "" {
				t.Fatal("lost call was not linked to its durable intent")
			}
			assertNoUnresolvedNestedIntents(t, store, owner)
		})
	}
}

func TestExecutorNeverSubstitutesMissingSessionOnRecovery(t *testing.T) {
	for _, tc := range []struct {
		name     string
		response *executorpb.OpenSessionResponse
		openErr  error
	}{
		{name: "unknown outcome is not definitive", response: &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_UNKNOWN, Message: "unknown session id retained-executor"}}},
		{name: "rejection is not session loss", response: &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, Message: "executor access denied", UpstreamCode: ptrInt32(-32600)}}},
		{name: "different session ID", response: &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, Message: "unknown session id another-executor", UpstreamCode: ptrInt32(-32600)}}},
		{name: "different session", response: &executorpb.OpenSessionResponse{Status: completedExecutionStatus(), ExecutionSessionId: "new-executor"}},
		{name: "unavailable adapter", openErr: status.Error(codes.Unavailable, "adapter unavailable")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store, owner := claimedExecutorStore(t)
			client := &fakeExecutionClient{}
			reads, opens := 0, 0
			client.open = func(ctx context.Context, _ *executorpb.OpenSessionRequest) (executorSessionStream, error) {
				opens++
				if tc.openErr != nil {
					return nil, tc.openErr
				}
				return &fakeExecutionStream{ctx: ctx, initial: tc.response}, nil
			}
			client.read = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
				reads++
				return &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}}, nil
			}
			e := newFakeExecutor(t, store, owner, client)
			e.reconnectRequired = true
			if tc.openErr != nil {
				e.reconnectTimeout = 150 * time.Millisecond
			}
			_, err := e.rpc(t.Context(), executorMethodProcessRead, executorRead{ProcessID: "existing", MaxBytes: 65536}, nil)
			if err == nil || reads != 0 || tc.openErr == nil && opens != 1 || tc.openErr != nil && opens < 1 {
				t.Fatalf("session substitution: err=%v reads=%d opens=%d", err, reads, opens)
			}
			conversation, readErr := store.Read(t.Context(), owner)
			if readErr != nil {
				t.Fatal(readErr)
			}
			if conversation.ExecutorSession == nil || *conversation.ExecutorSession != "retained-executor" {
				t.Fatalf("retained session changed: %+v", conversation.ExecutorSession)
			}
		})
	}
}

func TestExecutorOpenRejectionNeedsDurableNativeEvidenceForRecovery(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	intent, err := store.RecordNested(t.Context(), owner, IntentExecutor, map[string]any{"method": "initialize"})
	if err != nil {
		t.Fatal(err)
	}
	executor := newFakeExecutor(t, store, owner, &fakeExecutionClient{})
	response := &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{
		Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, UpstreamCode: ptrInt32(-32600), Message: "unknown session id retained-executor",
	}}
	frameErr := errors.New("native response frame persistence failed")
	err = executor.finishOpenFailure(t.Context(), intent, response, frameErr)
	if isExecutorConnectionFailure(err) {
		t.Fatalf("native evidence persistence failure became an ordinary connection failure: %v", err)
	}
	if !errors.Is(err, frameErr) {
		t.Fatalf("native frame persistence error was discarded: %v", err)
	}
	result, err := intentResultForDB(t, store, intent)
	if err != nil {
		t.Fatal(err)
	}
	if result["outcome"] != "rejected" {
		t.Fatalf("typed rejection was not durably settled: %+v", result)
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

	client := &fakeExecutionClient{}
	var resumes []*executorpb.OpenSessionRequest
	starts := 0
	var startedWithSession string
	client.open = func(ctx context.Context, request *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		resumes = append(resumes, proto.Clone(request).(*executorpb.OpenSessionRequest))
		if len(resumes) == 1 {
			return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{
				Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, UpstreamCode: ptrInt32(-32600), Message: "unknown session id retained-executor",
			}}}, nil
		}
		if request.ResumeSessionId != nil {
			t.Fatalf("fresh session request resumed %q", *request.ResumeSessionId)
		}
		return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{
			Status: completedExecutionStatus(), ExecutionSessionId: "fresh-executor",
			Shell: &executorpb.ExecutionShell{Name: "bash", Path: "/bin/bash"},
		}}, nil
	}
	client.start = func(_ context.Context, request *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		startedWithSession = request.ExecutionSessionId
		return &executorpb.StartProcessResponse{Status: completedExecutionStatus(), ProcessId: request.ProcessId}, nil
	}
	reads := 0
	client.read = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads++
		if request.ProcessId == "old-process" || request.ProcessId == "drained-process" {
			t.Fatalf("retired process %q reached the fresh executor", request.ProcessId)
		}
		return &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}, Exited: true, Closed: true, ExitCode: ptrInt32(0)}, nil
	}
	e := newFakeExecutor(t, store, owner, client)
	e.shell = &execShell{Name: "bash", Path: "/bin/bash"}
	e.environment = map[string]string{}
	e.reconnectRequired = true
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

func TestExecutorReconnectsBeforeNextOperationWithoutReplayingMutation(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &fakeExecutionClient{}
	starts, reads, opens := 0, 0, 0
	client.start = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		return nil, status.Error(codes.Unavailable, "start reply lost")
	}
	client.read = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads++
		return &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}}, nil
	}
	client.open = func(ctx context.Context, request *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		opens++
		if request.GetResumeSessionId() != "retained-executor" {
			t.Fatalf("resume=%q", request.GetResumeSessionId())
		}
		return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{Status: completedExecutionStatus(), ExecutionSessionId: "retained-executor"}}, nil
	}
	e := newFakeExecutor(t, store, owner, client)
	_, err := e.rpc(t.Context(), executorMethodProcessStart, executorStart{ProcessID: "lost-start", Argv: []string{"touch", "once"}, Cwd: "file:///tmp"}, nil)
	if !errors.As(err, new(*executorTransportError)) {
		t.Fatalf("start error=%v, want explicit unknown result", err)
	}
	_, err = e.rpc(t.Context(), executorMethodProcessRead, executorRead{ProcessID: "existing", MaxBytes: 65536}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if starts != 1 || reads != 1 || opens != 1 {
		t.Fatalf("starts=%d reads=%d opens=%d", starts, reads, opens)
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
	client := &fakeExecutionClient{}
	var reads []*executorpb.ReadProcessRequest
	client.read = func(_ context.Context, r *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads = append(reads, proto.Clone(r).(*executorpb.ReadProcessRequest))
		if len(reads) == 1 {
			return nil, status.Error(codes.Unavailable, "Gateway went away after read dispatch")
		}
		return &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Chunks: []*executorpb.ExecutionOutputChunk{{Seq: 6, Stream: "stdout", Data: []byte("after")}}, NextSeq: 7}, nil
	}
	offline := make(chan struct{})
	restored := make(chan struct{})
	var resumes []*executorpb.OpenSessionRequest
	client.open = func(ctx context.Context, r *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		resumes = append(resumes, proto.Clone(r).(*executorpb.OpenSessionRequest))
		if len(resumes) == 1 {
			close(offline)
			return nil, status.Error(codes.Unavailable, "Gateway unavailable")
		}
		select {
		case <-restored:
			return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{Status: completedExecutionStatus(), ExecutionSessionId: "retained-executor"}}, nil
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	e := newFakeExecutor(t, store, owner, client)
	e.scope = &executorpb.ExecutionScope{Destination: request.Executor, ExpectedIncarnationUid: "original", SessionId: request.SessionId}
	e.rpcTimeout = 30 * time.Millisecond
	e.reconnectTimeout = 2 * time.Second
	result := make(chan error, 1)
	go func() {
		_, err := e.ReconcileProcesses(t.Context(), 30*time.Millisecond)
		result <- err
	}()
	<-offline
	process, err := store.Process(t.Context(), owner, "running-process")
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != protocol.ProcessRunning || process.AfterSequence == nil || *process.AfterSequence != 5 || string(process.BufferedOutput) != "before " {
		t.Fatalf("transient outage abandoned or advanced the process: %+v", process)
	}
	close(restored)
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	if len(reads) != 2 || !proto.Equal(reads[0], reads[1]) || reads[0].RequestId == "" {
		t.Fatalf("read intent or cursor changed across reconnect: %+v", reads)
	}
	if len(resumes) < 2 {
		t.Fatalf("Gateway outage was not retried: %+v", resumes)
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
	if process.Status != protocol.ProcessRunning || process.AfterSequence == nil || *process.AfterSequence != 6 || string(process.BufferedOutput) != "before after" {
		t.Fatalf("reconnected read lost output or handle: %+v", process)
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
			client := &fakeExecutionClient{read: func(ctx context.Context, _ *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
				reads++
				deadline, ok := ctx.Deadline()
				if !ok || time.Until(deadline) > tc.attemptTimeout+time.Second {
					t.Fatalf("read RPC lost its short page deadline: deadline=%v, expected within %s", deadline, tc.attemptTimeout)
				}
				return &executorpb.ReadProcessResponse{Status: completedExecutionStatus()}, nil
			}}
			e := newFakeExecutor(t, store, owner, client)
			e.rpcTimeout = 30 * time.Second
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
	client := &fakeExecutionClient{}
	reads, opens := 0, 0
	client.read = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads++
		return nil, status.Error(codes.Unavailable, "Gateway lost read reply")
	}
	client.open = func(ctx context.Context, r *executorpb.OpenSessionRequest) (executorSessionStream, error) {
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
			return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{
				Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, UpstreamCode: ptrInt32(-32600), Message: "unknown session id retained-executor",
			}}}, nil
		default:
			if r.ResumeSessionId != nil {
				t.Fatalf("fresh open tried the lost native ID: %+v", r)
			}
			return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{Status: completedExecutionStatus(), ExecutionSessionId: "fresh-executor"}}, nil
		}
	}
	e := newFakeExecutor(t, store, owner, client)
	e.rpcTimeout = 30 * time.Millisecond
	e.reconnectTimeout = 2 * time.Second
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
	if process.Status != protocol.ProcessAbandoned || process.AfterSequence == nil || *process.AfterSequence != 5 || string(process.BufferedOutput) != "already observed" {
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

func TestReconnectBudgetExhaustionKeepsNativeBinding(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	client := &fakeExecutionClient{}
	resumes, mutations := 0, 0
	client.open = func(_ context.Context, r *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		resumes++
		if r.GetResumeSessionId() != "retained-executor" {
			t.Fatalf("outage caused a fresh session open: %+v", r)
		}
		return nil, status.Error(codes.Unavailable, "Gateway still unavailable")
	}
	client.start = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		mutations++
		return nil, errors.New("mutation dispatched")
	}
	e := newFakeExecutor(t, store, owner, client)
	e.reconnectRequired = true
	e.reconnectTimeout = 150 * time.Millisecond
	_, err := e.rpc(t.Context(), executorMethodProcessStart, executorStart{ProcessID: "new-process", Argv: []string{"touch", "new"}}, nil)
	if !errors.As(err, new(*executorNotDispatchedError)) || resumes < 1 || mutations != 0 {
		t.Fatalf("retry exhaustion dispatched mutation or lost its refusal: resumes=%d mutations=%d error=%v", resumes, mutations, err)
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if conversation.ExecutorSession == nil || *conversation.ExecutorSession != "retained-executor" || e.sessionID != "retained-executor" {
		t.Fatalf("transport outage replaced native binding: %+v, local=%q", conversation.ExecutorSession, e.sessionID)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestReconnectStopsOnCallerCancellation(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	resumes := 0
	client := &fakeExecutionClient{open: func(_ context.Context, r *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		resumes++
		if r.GetResumeSessionId() != "retained-executor" {
			t.Fatalf("caller cancellation caused a fresh open: %+v", r)
		}
		cancel()
		return nil, status.Error(codes.Unavailable, "Gateway unavailable")
	}}
	e := newFakeExecutor(t, store, owner, client)
	e.reconnectRequired = true
	if err := e.reconnectLocked(ctx, ""); !errors.Is(err, context.Canceled) || resumes != 1 {
		t.Fatalf("reconnect ignored cancellation: attempts=%d error=%v", resumes, err)
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if conversation.ExecutorSession == nil || *conversation.ExecutorSession != "retained-executor" {
		t.Fatalf("cancellation replaced native binding: %+v", conversation.ExecutorSession)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestReconnectDiscardsLateFirstFrameBeforeAnotherResume(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	firstDone := make(chan struct{})
	opens := 0
	client := &fakeExecutionClient{open: func(ctx context.Context, r *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		opens++
		if r.GetResumeSessionId() != "retained-executor" {
			t.Fatalf("resume changed native session: %+v", r)
		}
		if opens == 1 {
			return &lateSuccessExecutorStream{ctx: ctx, done: firstDone}, nil
		}
		select {
		case <-firstDone:
		default:
			t.Fatal("new resume overlapped the canceled stream")
		}
		return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{Status: completedExecutionStatus(), ExecutionSessionId: "retained-executor"}}, nil
	}}
	e := newFakeExecutor(t, store, owner, client)
	e.reconnectRequired = true
	e.reconnectTimeout = 2 * time.Second
	if err := e.reconnectLocked(t.Context(), ""); err != nil {
		t.Fatal(err)
	}
	if opens != 2 || e.sessionGeneration != 1 {
		t.Fatalf("late first frame became a bound session: opens=%d generation=%d", opens, e.sessionGeneration)
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
	client := &fakeExecutionClient{}
	client.read = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		return nil, status.Error(codes.Unavailable, "executor transport failed")
	}
	client.open = func(context.Context, *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		return nil, status.Error(codes.Unavailable, "session resume failed")
	}
	e := newFakeExecutor(t, store, owner, client)
	e.reconnectTimeout = 150 * time.Millisecond
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

type fakeExecutionClient struct {
	open      func(context.Context, *executorpb.OpenSessionRequest) (executorSessionStream, error)
	start     func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error)
	read      func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error)
	write     func(context.Context, *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error)
	signal    func(context.Context, *executorpb.SignalProcessRequest) (*executorpb.SignalProcessResponse, error)
	terminate func(context.Context, *executorpb.TerminateProcessRequest) (*executorpb.TerminateProcessResponse, error)
	metadata  func(context.Context, *executorpb.GetFileMetadataRequest) (*executorpb.GetFileMetadataResponse, error)
	openFile  func(context.Context, *executorpb.OpenFileRequest) (*executorpb.OpenFileResponse, error)
	readFile  func(context.Context, *executorpb.ReadFileRequest) (*executorpb.ReadFileResponse, error)
	closeFile func(context.Context, *executorpb.CloseFileRequest) (*executorpb.CloseFileResponse, error)
}

func (c *fakeExecutionClient) OpenSession(ctx context.Context, r *executorpb.OpenSessionRequest, _ ...grpc.CallOption) (executorSessionStream, error) {
	if c.open != nil {
		return c.open(ctx, r)
	}
	sid := r.GetResumeSessionId()
	return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{Status: completedExecutionStatus(), ExecutionSessionId: sid}}, nil
}

func (c *fakeExecutionClient) StartProcess(ctx context.Context, r *executorpb.StartProcessRequest, _ ...grpc.CallOption) (*executorpb.StartProcessResponse, error) {
	if c.start != nil {
		return c.start(ctx, r)
	}
	return &executorpb.StartProcessResponse{Status: completedExecutionStatus(), ProcessId: r.ProcessId}, nil
}

func (c *fakeExecutionClient) ReadProcess(ctx context.Context, r *executorpb.ReadProcessRequest, _ ...grpc.CallOption) (*executorpb.ReadProcessResponse, error) {
	if c.read != nil {
		return c.read(ctx, r)
	}
	return &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}}, nil
}

func (c *fakeExecutionClient) WriteProcess(ctx context.Context, r *executorpb.WriteProcessRequest, _ ...grpc.CallOption) (*executorpb.WriteProcessResponse, error) {
	if c.write != nil {
		return c.write(ctx, r)
	}
	return &executorpb.WriteProcessResponse{Status: completedExecutionStatus(), WriteStatus: "accepted"}, nil
}

func (c *fakeExecutionClient) SignalProcess(ctx context.Context, r *executorpb.SignalProcessRequest, _ ...grpc.CallOption) (*executorpb.SignalProcessResponse, error) {
	if c.signal != nil {
		return c.signal(ctx, r)
	}
	return &executorpb.SignalProcessResponse{Status: completedExecutionStatus()}, nil
}

func (c *fakeExecutionClient) TerminateProcess(ctx context.Context, r *executorpb.TerminateProcessRequest, _ ...grpc.CallOption) (*executorpb.TerminateProcessResponse, error) {
	if c.terminate != nil {
		return c.terminate(ctx, r)
	}
	return &executorpb.TerminateProcessResponse{Status: completedExecutionStatus()}, nil
}

func (c *fakeExecutionClient) GetFileMetadata(ctx context.Context, r *executorpb.GetFileMetadataRequest, _ ...grpc.CallOption) (*executorpb.GetFileMetadataResponse, error) {
	if c.metadata != nil {
		return c.metadata(ctx, r)
	}
	return &executorpb.GetFileMetadataResponse{Status: completedExecutionStatus()}, nil
}

func (c *fakeExecutionClient) OpenFile(ctx context.Context, r *executorpb.OpenFileRequest, _ ...grpc.CallOption) (*executorpb.OpenFileResponse, error) {
	if c.openFile != nil {
		return c.openFile(ctx, r)
	}
	return &executorpb.OpenFileResponse{Status: completedExecutionStatus(), HandleId: r.HandleId}, nil
}

func (c *fakeExecutionClient) ReadFile(ctx context.Context, r *executorpb.ReadFileRequest, _ ...grpc.CallOption) (*executorpb.ReadFileResponse, error) {
	if c.readFile != nil {
		return c.readFile(ctx, r)
	}
	return &executorpb.ReadFileResponse{Status: completedExecutionStatus(), Eof: true}, nil
}

func (c *fakeExecutionClient) CloseFile(ctx context.Context, r *executorpb.CloseFileRequest, _ ...grpc.CallOption) (*executorpb.CloseFileResponse, error) {
	if c.closeFile != nil {
		return c.closeFile(ctx, r)
	}
	return &executorpb.CloseFileResponse{Status: completedExecutionStatus()}, nil
}

type fakeExecutionStream struct {
	ctx     context.Context
	initial *executorpb.OpenSessionResponse
	once    sync.Once
}

type failedExecutionStream struct{ err error }

func (s failedExecutionStream) Recv() (*executorpb.OpenSessionResponse, error) { return nil, s.err }

type notificationExecutorStream struct {
	ctx       context.Context
	responses chan *executorpb.OpenSessionResponse
}

func (s *notificationExecutorStream) Recv() (*executorpb.OpenSessionResponse, error) {
	select {
	case response := <-s.responses:
		return response, nil
	case <-s.ctx.Done():
		return nil, s.ctx.Err()
	}
}

type lateSuccessExecutorStream struct {
	ctx  context.Context
	done chan struct{}
}

func (s *lateSuccessExecutorStream) Recv() (*executorpb.OpenSessionResponse, error) {
	<-s.ctx.Done()
	close(s.done)
	return &executorpb.OpenSessionResponse{Status: completedExecutionStatus(), ExecutionSessionId: "retained-executor"}, nil
}

type gatedExecutorStream struct {
	ctx      context.Context
	started  chan struct{}
	release  chan struct{}
	finished chan struct{}
	response *executorpb.OpenSessionResponse
	mu       sync.Mutex
	first    bool
}

func (s *gatedExecutorStream) Recv() (*executorpb.OpenSessionResponse, error) {
	s.mu.Lock()
	if s.first {
		s.mu.Unlock()
		<-s.ctx.Done()
		return nil, s.ctx.Err()
	}
	s.first = true
	s.mu.Unlock()
	close(s.started)
	select {
	case <-s.release:
	case <-s.ctx.Done():
		close(s.finished)
		return nil, s.ctx.Err()
	}
	close(s.finished)
	return s.response, nil
}

func (s *fakeExecutionStream) Recv() (*executorpb.OpenSessionResponse, error) {
	var response *executorpb.OpenSessionResponse
	s.once.Do(func() { response = s.initial })
	if response != nil {
		return response, nil
	}
	<-s.ctx.Done()
	return nil, io.EOF
}

func claimedExecutorStore(t *testing.T) (*Store, Owner) {
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

func newFakeExecutor(t *testing.T, store *Store, owner Owner, client executorClient) *Executor {
	t.Helper()
	return &Executor{store: store, owner: owner, client: client, scope: &executorpb.ExecutionScope{SessionId: owner.ConversationID}, sessionID: "retained-executor", lifetime: t.Context(), cwd: "/tmp", processLocks: map[string]*sync.Mutex{}, initializeTimeout: time.Minute, reconnectTimeout: 2 * time.Minute, workspacePrepared: true}
}

func completedExecutionStatus() *executorpb.ExecutionStatus {
	return &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_COMPLETED}
}
func ptrInt32(value int32) *int32 { return &value }
func boolInt(value bool) int {
	if value {
		return 1
	}
	return 0
}

func assertNoUnresolvedNestedIntents(t *testing.T, store *Store, owner Owner) {
	t.Helper()
	var count int
	if err := store.DB().QueryRowContext(t.Context(), "SELECT count(*) FROM sampler_intent WHERE conversation_id=$1 AND result IS NULL", owner.ConversationID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("unresolved nested intents=%d", count)
	}
}
