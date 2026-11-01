package centralsampler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestExecutorContinuesAfterSameSandboxResumes(t *testing.T) {
	for _, api := range []protocol.APIStyle{protocol.APIStyleResponses, protocol.APIStyleAnthropicMessages} {
		for _, retained := range []bool{false, true} {
			name := string(api) + "/first-open"
			if retained {
				name = string(api) + "/retained-session"
			}
			t.Run(name, func(t *testing.T) {
				store, owner, request := resumedExecutorStore(t, api)
				if retained {
					if err := store.SetExecutorSession(t.Context(), owner, "old-executor", nil); err != nil {
						t.Fatal(err)
					}
					if err := store.RegisterModelProcess(t.Context(), owner, "old-process", 1234, false); err != nil {
						t.Fatal(err)
					}
					if err := store.RetainProcessOutput(t.Context(), owner, "old-process", 5, []byte("previous output"), false, nil); err != nil {
						t.Fatal(err)
					}
				}
				if err := store.CellResult(t.Context(), owner, "central-js-cell", "live"); err != nil {
					t.Fatal(err)
				}
				client := &fakeExecutionClient{}
				opens, commands := 0, 0
				client.open = func(ctx context.Context, r *executorpb.OpenSessionRequest) (executorSessionStream, error) {
					opens++
					if opens == 1 {
						return nil, changedIncarnationStatus(t, "sandbox-uid", "replacement").Err()
					}
					if r.Scope.ExpectedIncarnationUid != "replacement" || r.ResumeSessionId != nil {
						t.Fatalf("fresh open used stale identity: %+v", r)
					}
					stored, err := store.GetSession(t.Context(), request.SessionId, "fixture-account")
					if err != nil {
						t.Fatal(err)
					}
					bound := &samplerpb.OpenSessionRequest{}
					if err := proto.Unmarshal(stored.Configuration, bound); err != nil {
						t.Fatal(err)
					}
					if bound.ExpectedIncarnationUid != "replacement" {
						t.Fatal("fresh open preceded durable rebind")
					}
					return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{Status: completedExecutionStatus(), ExecutionSessionId: "replacement-executor", Shell: &executorpb.ExecutionShell{Name: "bash", Path: "/bin/bash"}}}, nil
				}
				client.start = func(_ context.Context, r *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
					if r.Scope.ExpectedIncarnationUid != "replacement" || r.ExecutionSessionId != "replacement-executor" {
						t.Fatalf("command used stale identity: %+v", r)
					}
					for _, arg := range r.Argv {
						if arg == "echo resumed" {
							commands++
						}
					}
					return &executorpb.StartProcessResponse{Status: completedExecutionStatus(), ProcessId: r.ProcessId}, nil
				}
				client.read = func(_ context.Context, r *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
					if r.ProcessId == "old-process" {
						t.Fatal("old process reached replacement executor")
					}
					return &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Exited: true, Closed: true, ExitCode: ptrInt32(0)}, nil
				}
				executor := newFakeExecutor(t, store, owner, client)
				executor.scope = &executorpb.ExecutionScope{Destination: request.Executor, ExpectedIncarnationUid: "original", SessionId: request.SessionId}
				executor.environment = map[string]string{}
				executor.sessionID = ""
				if retained {
					executor.sessionID = "old-executor"
					executor.reconnectRequired = true
				}
				if _, err := executor.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"echo resumed"}`)); err != nil {
					t.Fatal(err)
				}
				if commands != 1 || opens != 2 {
					t.Fatalf("command starts=%d opens=%d", commands, opens)
				}
				conversation, err := store.Read(t.Context(), owner)
				if err != nil {
					t.Fatal(err)
				}
				wire, err := json.Marshal(conversation.Archive)
				if err != nil {
					t.Fatal(err)
				}
				if strings.Count(string(wire), "execution Sandbox changed incarnation") != 1 {
					t.Fatalf("expected one model notice: %s", wire)
				}
				if retained {
					process, err := store.Process(t.Context(), owner, "old-process")
					if err != nil {
						t.Fatal(err)
					}
					if process.Status != protocol.ProcessAbandoned || string(process.BufferedOutput) != "previous output" || *process.AfterSequence != 5 {
						t.Fatalf("lost old evidence: %+v", process)
					}
					before := opens
					if _, err := executor.Invoke(t.Context(), "write_stdin", json.RawMessage(`{"session_id":1234,"chars":"x"}`)); err == nil {
						t.Fatal("old stdin handle was accepted")
					}
					if opens != before {
						t.Fatal("old handle opened another execution session")
					}
				}
				state, err := store.ExecutionState(t.Context(), owner)
				if err != nil {
					t.Fatal(err)
				}
				if len(state.Cells) != 1 || state.Cells[0].ID != "central-js-cell" {
					t.Fatalf("incarnation change retired central JavaScript: %+v", state)
				}
				if err := executor.close(); err != nil {
					t.Fatal(err)
				}
				assertNoUnresolvedNestedIntents(t, store, owner)
			})
		}
	}
}

func TestExecutorDoesNotRebindWithoutMatchingSandboxEvidence(t *testing.T) {
	for _, tc := range []struct {
		name string
		err  error
	}{
		{"unstructured error", status.Error(codes.FailedPrecondition, "Sandbox incarnation changed")},
		{"different sandbox", changedIncarnationStatus(t, "other-sandbox", "replacement").Err()},
		{"empty incarnation", changedIncarnationStatus(t, "sandbox-uid", "").Err()},
		{"same incarnation", changedIncarnationStatus(t, "sandbox-uid", "original").Err()},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store, owner, request := resumedExecutorStore(t, protocol.APIStyleResponses)
			client := &fakeExecutionClient{open: func(context.Context, *executorpb.OpenSessionRequest) (executorSessionStream, error) {
				return nil, tc.err
			}}
			executor := newFakeExecutor(t, store, owner, client)
			executor.scope = &executorpb.ExecutionScope{Destination: request.Executor, ExpectedIncarnationUid: "original", SessionId: request.SessionId}
			if err := executor.openSession(t.Context(), nil); err == nil {
				t.Fatal("invalid rebind evidence accepted")
			}
			stored, err := store.GetSession(t.Context(), request.SessionId, "fixture-account")
			if err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(request, decodeResumeRequest(t, stored.Configuration)) {
				t.Fatal("invalid evidence changed durable binding")
			}
			conversation, err := store.Read(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			wire, err := json.Marshal(conversation.Archive)
			if err != nil {
				t.Fatal(err)
			}
			if strings.Contains(string(wire), "changed incarnation") {
				t.Fatal("invalid evidence emitted a change notice")
			}
			assertNoUnresolvedNestedIntents(t, store, owner)
		})
	}
}

func TestExecCommandExpectedIncarnationRejectsBeforeWorkspaceSetup(t *testing.T) {
	store, owner, request := resumedExecutorStore(t, protocol.APIStyleResponses)
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	client := &fakeExecutionClient{}
	var opened *executorpb.OpenSessionRequest
	starts := 0
	client.open = func(ctx context.Context, r *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		opened = proto.Clone(r).(*executorpb.OpenSessionRequest)
		return nil, changedIncarnationStatus(t, "sandbox-uid", "current-incarnation").Err()
	}
	client.start = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		return &executorpb.StartProcessResponse{Status: completedExecutionStatus()}, nil
	}
	executor := newFakeExecutor(t, store, owner, client)
	executor.scope = &executorpb.ExecutionScope{Destination: request.Executor, ExpectedIncarnationUid: "original", SessionId: request.SessionId}
	executor.workspacePrepared = false
	_, err := executor.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"touch should-not-run","expected_incarnation_uid":"stale-incarnation"}`))
	var outcome *toolOutcomeError
	if !errors.As(err, &outcome) {
		t.Fatalf("exec error=%v, want structured stale-incarnation tool rejection", err)
	}
	var message map[string]any
	if err := json.Unmarshal([]byte(outcome.toolError.message), &message); err != nil {
		t.Fatalf("decode model-facing execution fence facts: %v; message=%q", err, outcome.toolError.message)
	}
	if message["current_incarnation_uid"] != "current-incarnation" || message["remote_outcome"] != "not_dispatched" || message["request_outcome"] != "not_dispatched" || message["method"] != "initialize" {
		t.Fatalf("unexpected stale fence message facts: %+v", message)
	}
	if opened == nil || opened.Scope.GetExpectedIncarnationUid() != "stale-incarnation" || opened.ResumeSessionId != nil {
		t.Fatalf("explicit fence was not checked on a fresh authenticated OpenSession: %+v", opened)
	}
	if starts != 0 || executor.workspacePrepared {
		t.Fatalf("stale fence caused workspace or command effects: starts=%d prepared=%v", starts, executor.workspacePrepared)
	}
	stored, err := store.GetSession(t.Context(), request.SessionId, "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(request, decodeResumeRequest(t, stored.Configuration)) {
		t.Fatal("stale explicit fence changed the durable scope")
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if conversation.ExecutorSession == nil || *conversation.ExecutorSession != "retained-executor" {
		t.Fatalf("stale fence replaced retained native session: %+v", conversation.ExecutorSession)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestExecCommandExpectedIncarnationFencesWorkspaceStartAtDispatch(t *testing.T) {
	store, owner, request := resumedExecutorStore(t, protocol.APIStyleResponses)
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	client := &fakeExecutionClient{}
	starts, reads := 0, 0
	var workspaceProcessID string
	client.start = func(_ context.Context, r *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		workspaceProcessID = r.ProcessId
		return nil, changedIncarnationStatus(t, "sandbox-uid", "current-incarnation").Err()
	}
	client.read = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads++
		return &executorpb.ReadProcessResponse{Status: completedExecutionStatus()}, nil
	}
	executor := newFakeExecutor(t, store, owner, client)
	executor.scope = &executorpb.ExecutionScope{Destination: request.Executor, ExpectedIncarnationUid: "original", SessionId: request.SessionId}
	executor.workspacePrepared = false
	_, err := executor.execCommand(t.Context(), json.RawMessage(`{"cmd":"touch should-not-run","expected_incarnation_uid":"original"}`))
	var fence *executorIncarnationFenceError
	if !errors.As(err, &fence) || !fence.recorded {
		t.Fatalf("exec error=%v, want recorded stale process/start rejection", err)
	}
	if fence.currentIncarnationUID != "current-incarnation" || fence.remoteOutcome != NestedNotDispatched || fence.method != executorMethodProcessStart {
		t.Fatalf("unexpected stale fence facts: %+v", fence)
	}
	if starts != 1 || reads != 0 || executor.workspacePrepared {
		t.Fatalf("fenced workspace start caused later effects: starts=%d reads=%d prepared=%v", starts, reads, executor.workspacePrepared)
	}
	process, err := store.Process(t.Context(), owner, workspaceProcessID)
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != protocol.ProcessExited {
		t.Fatalf("unstarted workspace process was not settled locally: %+v", process)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestExecCommandExpectedIncarnationKeepsMatchingRetainedSession(t *testing.T) {
	store, owner, request := resumedExecutorStore(t, protocol.APIStyleResponses)
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	client := &fakeExecutionClient{}
	opens, starts := 0, 0
	client.open = func(context.Context, *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		opens++
		return nil, errors.New("matching fence opened an unnecessary session")
	}
	client.start = func(_ context.Context, r *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		if r.Scope.GetExpectedIncarnationUid() != "original" || r.ExecutionSessionId != "retained-executor" {
			t.Fatalf("matching fence left the retained execution session: %+v", r)
		}
		return &executorpb.StartProcessResponse{Status: completedExecutionStatus(), ProcessId: r.ProcessId}, nil
	}
	client.read = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		return &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Exited: true, Closed: true, ExitCode: ptrInt32(0)}, nil
	}
	executor := newFakeExecutor(t, store, owner, client)
	executor.scope = &executorpb.ExecutionScope{Destination: request.Executor, ExpectedIncarnationUid: "original", SessionId: request.SessionId}
	executor.environment = map[string]string{}
	if _, err := executor.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"echo retained","expected_incarnation_uid":"original"}`)); err != nil {
		t.Fatal(err)
	}
	if opens != 0 || starts != 1 {
		t.Fatalf("matching fence opened %d fresh sessions and started %d processes, want 0 and 1", opens, starts)
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if conversation.ExecutorSession == nil || *conversation.ExecutorSession != "retained-executor" {
		t.Fatalf("matching fence changed the retained binding: %+v", conversation.ExecutorSession)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestExecCommandExpectedIncarnationReadFenceAbandonsWithoutReplay(t *testing.T) {
	store, owner, request := resumedExecutorStore(t, protocol.APIStyleResponses)
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	client := &fakeExecutionClient{}
	starts, reads := 0, 0
	client.start = func(_ context.Context, r *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		return &executorpb.StartProcessResponse{Status: completedExecutionStatus(), ProcessId: r.ProcessId}, nil
	}
	client.read = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads++
		if reads == 1 {
			return &executorpb.ReadProcessResponse{
				Status:  completedExecutionStatus(),
				Chunks:  []*executorpb.ExecutionOutputChunk{{Seq: 1, Stream: "stdout", Data: []byte("partial output")}},
				NextSeq: 2,
			}, nil
		}
		return nil, changedIncarnationStatus(t, "sandbox-uid", "current-incarnation").Err()
	}
	executor := newFakeExecutor(t, store, owner, client)
	executor.scope = &executorpb.ExecutionScope{Destination: request.Executor, ExpectedIncarnationUid: "original", SessionId: request.SessionId}
	executor.environment = map[string]string{}
	_, err := executor.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"echo started once","yield_time_ms":250,"expected_incarnation_uid":"original"}`))
	var outcome *toolOutcomeError
	if !errors.As(err, &outcome) {
		t.Fatalf("exec error=%v, want structured process/read fence", err)
	}
	toolEvidence, ok := outcome.evidence.(executionFailureEvidence)
	if !ok {
		t.Fatalf("process/read fence evidence has type %T, want executionFailureEvidence", outcome.evidence)
	}
	if toolEvidence.CurrentIncarnationUID != "current-incarnation" || toolEvidence.RemoteOutcome != NestedUnknown ||
		toolEvidence.RequestOutcome == nil || *toolEvidence.RequestOutcome != NestedNotDispatched ||
		toolEvidence.ProcessID == "" || toolEvidence.ExecutionSessionID != "retained-executor" ||
		toolEvidence.AfterSeq == nil || *toolEvidence.AfterSeq != 1 || toolEvidence.BufferedOutput == nil ||
		*toolEvidence.BufferedOutput != "partial output" || toolEvidence.BufferedOutputBase64 == nil ||
		!bytes.Equal(*toolEvidence.BufferedOutputBase64, []byte("partial output")) {
		t.Fatalf("process/read fence tool evidence lost typed facts: %+v", toolEvidence)
	}
	var message map[string]any
	if err := json.Unmarshal([]byte(outcome.toolError.message), &message); err != nil {
		t.Fatalf("decode model-facing process/read facts: %v; message=%q", err, outcome.toolError.message)
	}
	if starts != 1 || reads != 2 {
		t.Fatalf("fenced call started %d processes and made %d reads, want one start and two reads with no replay", starts, reads)
	}
	if message["method"] != "process/read" || message["remote_outcome"] != "unknown" || message["request_outcome"] != "not_dispatched" || message["automatic_replay"] != false || message["current_incarnation_uid"] != "current-incarnation" || message["execution_session_id"] != "retained-executor" || message["after_seq"] != float64(1) {
		t.Fatalf("unexpected process/read fence facts: %+v", message)
	}
	observedOutput, ok := message["observed_output"].(map[string]any)
	if !ok || observedOutput["text"] != "partial output" {
		t.Fatalf("fenced read did not retain observed process output: %+v", message["observed_output"])
	}
	processID, ok := message["process_id"].(string)
	if !ok || processID == "" {
		t.Fatalf("process/read fence omitted process ID: %+v", message)
	}
	process, err := store.Process(t.Context(), owner, processID)
	if err != nil {
		t.Fatal(err)
	}
	if process.Status != protocol.ProcessAbandoned {
		t.Fatalf("uncertain process was not abandoned: %+v", process)
	}
	if string(process.BufferedOutput) != "partial output" || process.AfterSequence == nil || *process.AfterSequence != 1 {
		t.Fatalf("fenced read lost durable output evidence: %+v", process)
	}
	var intentResultWire []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT result FROM sampler_intent WHERE conversation_id=$1 AND result->>'current_incarnation_uid'='current-incarnation'`, owner.ConversationID).Scan(&intentResultWire); err != nil {
		t.Fatal(err)
	}
	var intentResult map[string]any
	if err := json.Unmarshal(intentResultWire, &intentResult); err != nil {
		t.Fatal(err)
	}
	if intentResult["request_outcome"] != "not_dispatched" || intentResult["remote_outcome"] != "unknown" || intentResult["after_seq"] != float64(1) || intentResult["buffered_output"] != "partial output" {
		t.Fatalf("nested process/read evidence lost outcome or output facts: %+v", intentResult)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestExecCommandExpectedIncarnationRebindsOnlyAfterAuthenticatedOpen(t *testing.T) {
	store, owner, request := resumedExecutorStore(t, protocol.APIStyleResponses)
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterModelProcess(t.Context(), owner, "old-process", 1234, false); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, "old-process", nil, protocol.ProcessRunning, nil, false); err != nil {
		t.Fatal(err)
	}
	client := &fakeExecutionClient{}
	starts, reads := 0, 0
	client.open = func(ctx context.Context, r *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		if r.Scope.GetExpectedIncarnationUid() != "current-incarnation" || r.ResumeSessionId != nil {
			t.Fatalf("candidate OpenSession scope=%+v resume=%v", r.Scope, r.ResumeSessionId)
		}
		return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{
			Status: completedExecutionStatus(), ExecutionSessionId: "new-executor",
			Shell: &executorpb.ExecutionShell{Name: "bash", Path: "/bin/bash"},
		}}, nil
	}
	client.start = func(_ context.Context, r *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		if r.Scope.GetExpectedIncarnationUid() != "current-incarnation" || r.ExecutionSessionId != "new-executor" {
			t.Fatalf("workspace/command used wrong execution scope: %+v", r)
		}
		conversation, err := store.Read(t.Context(), owner)
		if err != nil {
			t.Fatal(err)
		}
		if conversation.ExecutorSession == nil || *conversation.ExecutorSession != "new-executor" {
			t.Fatalf("workspace began before new session binding persisted: %+v", conversation.ExecutorSession)
		}
		oldProcess, err := store.Process(t.Context(), owner, "old-process")
		if err != nil {
			t.Fatal(err)
		}
		if oldProcess.Status != protocol.ProcessAbandoned {
			t.Fatalf("workspace began before old handles were retired: %+v", oldProcess)
		}
		return &executorpb.StartProcessResponse{Status: completedExecutionStatus(), ProcessId: r.ProcessId}, nil
	}
	client.read = func(_ context.Context, r *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads++
		return &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Exited: true, Closed: true, ExitCode: ptrInt32(0)}, nil
	}
	executor := newFakeExecutor(t, store, owner, client)
	executor.scope = &executorpb.ExecutionScope{Destination: request.Executor, ExpectedIncarnationUid: "original", SessionId: request.SessionId}
	executor.environment = map[string]string{}
	executor.shell = nil
	executor.workspacePrepared = false
	if _, err := executor.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"echo exactly-once","expected_incarnation_uid":"current-incarnation"}`)); err != nil {
		t.Fatal(err)
	}
	if starts != 2 || reads != 2 {
		t.Fatalf("workspace+command executions=%d reads=%d, want exactly one each", starts, reads)
	}
	stored, err := store.GetSession(t.Context(), request.SessionId, "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	bound := decodeResumeRequest(t, stored.Configuration)
	if bound.GetExpectedIncarnationUid() != "current-incarnation" {
		t.Fatalf("stored expected UID=%q", bound.GetExpectedIncarnationUid())
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if conversation.ExecutorSession == nil || *conversation.ExecutorSession != "new-executor" {
		t.Fatalf("stored executor session=%v", conversation.ExecutorSession)
	}
	oldProcess, err := store.Process(t.Context(), owner, "old-process")
	if err != nil {
		t.Fatal(err)
	}
	if oldProcess.Status != protocol.ProcessAbandoned {
		t.Fatalf("old execution handle survived rebind: %+v", oldProcess)
	}
	if err := executor.close(); err != nil {
		t.Fatal(err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestExecCommandExpectedIncarnationCrashBeforeRebindKeepsOldBinding(t *testing.T) {
	store, owner, request := resumedExecutorStore(t, protocol.APIStyleResponses)
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	if err := store.RegisterModelProcess(t.Context(), owner, "old-process", 1234, false); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, "old-process", nil, protocol.ProcessRunning, nil, false); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `CREATE FUNCTION reject_candidate_executor_session() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN
			IF NEW.executor_session = 'candidate-session' THEN
				RAISE EXCEPTION 'injected binding persistence failure';
			END IF;
			RETURN NEW;
		END
	$$`); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `CREATE TRIGGER reject_candidate_executor_session BEFORE UPDATE OF executor_session ON sampler_conversation FOR EACH ROW EXECUTE FUNCTION reject_candidate_executor_session()`); err != nil {
		t.Fatal(err)
	}
	triggerInstalled := true
	dropBindingFailure := func(ctx context.Context) error {
		if !triggerInstalled {
			return nil
		}
		if _, err := store.DB().ExecContext(ctx, `DROP TRIGGER reject_candidate_executor_session ON sampler_conversation`); err != nil {
			return err
		}
		_, err := store.DB().ExecContext(ctx, `DROP FUNCTION reject_candidate_executor_session()`)
		if err == nil {
			triggerInstalled = false
		}
		return err
	}
	t.Cleanup(func() {
		if err := dropBindingFailure(context.Background()); err != nil {
			t.Errorf("drop injected binding failure: %v", err)
		}
	})

	firstClient := &fakeExecutionClient{}
	firstStarts, firstOpens := 0, 0
	firstClient.open = func(ctx context.Context, r *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		firstOpens++
		if r.Scope.GetExpectedIncarnationUid() != "candidate" || r.ResumeSessionId != nil {
			t.Fatalf("candidate open used wrong scope: %+v", r)
		}
		return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{
			Status: completedExecutionStatus(), ExecutionSessionId: "candidate-session",
		}}, nil
	}
	firstClient.start = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		firstStarts++
		return &executorpb.StartProcessResponse{Status: completedExecutionStatus()}, nil
	}
	firstExecutor := newFakeExecutor(t, store, owner, firstClient)
	firstExecutor.scope = &executorpb.ExecutionScope{Destination: request.Executor, ExpectedIncarnationUid: "original", SessionId: request.SessionId}
	firstExecutor.workspacePrepared = false
	_, err := firstExecutor.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"echo exactly once","expected_incarnation_uid":"candidate"}`))
	if err == nil || !strings.Contains(err.Error(), "record verified execution Sandbox incarnation change") {
		t.Fatalf("first fenced call error=%v, want failure between acknowledged empty open and durable rebind", err)
	}
	if firstOpens != 1 || firstStarts != 0 {
		t.Fatalf("pre-crash opens=%d starts=%d, want 1 empty open and no process effects", firstOpens, firstStarts)
	}
	if err := dropBindingFailure(t.Context()); err != nil {
		t.Fatal(err)
	}
	stored, err := store.GetSession(t.Context(), request.SessionId, "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(request, decodeResumeRequest(t, stored.Configuration)) {
		t.Fatal("failed rebind changed the durable expected incarnation")
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if conversation.ExecutorSession == nil || *conversation.ExecutorSession != "retained-executor" {
		t.Fatalf("failed rebind changed the durable executor session: %+v", conversation.ExecutorSession)
	}
	var requestWire, resultWire []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT request,result FROM sampler_intent WHERE conversation_id=$1 AND request->>'incarnation_rebind_from'='original' ORDER BY id DESC LIMIT 1`, owner.ConversationID).Scan(&requestWire, &resultWire); err != nil {
		t.Fatal(err)
	}
	var initRequest struct {
		Scope struct {
			SandboxUID             string `json:"sandbox_uid"`
			ExpectedIncarnationUID string `json:"expected_incarnation_uid"`
		} `json:"execution_scope"`
		RebindFrom string `json:"incarnation_rebind_from"`
	}
	if err := json.Unmarshal(requestWire, &initRequest); err != nil {
		t.Fatal(err)
	}
	var initResult map[string]any
	if err := json.Unmarshal(resultWire, &initResult); err != nil {
		t.Fatal(err)
	}
	if initRequest.Scope.SandboxUID != "sandbox-uid" || initRequest.Scope.ExpectedIncarnationUID != "candidate" || initRequest.RebindFrom != "original" || initResult["execution_session_id"] != "candidate-session" {
		t.Fatalf("completed empty OpenSession intent lacks candidate identity/outcome: request=%+v result=%+v", initRequest, initResult)
	}

	// Reconstruct only durable state, as a sampler restart would. The completed empty session
	// from before the crash is evidence, never a binding; the fresh explicit call opens again.
	secondClient := &fakeExecutionClient{}
	secondOpens, secondStarts, secondReads := 0, 0, 0
	secondClient.open = func(ctx context.Context, r *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		secondOpens++
		if r.Scope.GetExpectedIncarnationUid() != "candidate" || r.ResumeSessionId != nil {
			t.Fatalf("restart reused the orphaned session or old identity: %+v", r)
		}
		return &fakeExecutionStream{ctx: ctx, initial: &executorpb.OpenSessionResponse{
			Status: completedExecutionStatus(), ExecutionSessionId: "restarted-session",
		}}, nil
	}
	secondClient.start = func(_ context.Context, r *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		secondStarts++
		if r.Scope.GetExpectedIncarnationUid() != "candidate" || r.ExecutionSessionId != "restarted-session" {
			t.Fatalf("post-restart workspace/command used wrong scope: %+v", r)
		}
		conversation, err := store.Read(t.Context(), owner)
		if err != nil {
			t.Fatal(err)
		}
		if conversation.ExecutorSession == nil || *conversation.ExecutorSession != "restarted-session" {
			t.Fatalf("post-restart command began before durable rebind: %+v", conversation.ExecutorSession)
		}
		oldProcess, err := store.Process(t.Context(), owner, "old-process")
		if err != nil {
			t.Fatal(err)
		}
		if oldProcess.Status != protocol.ProcessAbandoned {
			t.Fatalf("old process handle survived post-restart rebind: %+v", oldProcess)
		}
		return &executorpb.StartProcessResponse{Status: completedExecutionStatus(), ProcessId: r.ProcessId}, nil
	}
	secondClient.read = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		secondReads++
		return &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Exited: true, Closed: true, ExitCode: ptrInt32(0)}, nil
	}
	secondExecutor := newFakeExecutor(t, store, owner, secondClient)
	secondExecutor.scope = &executorpb.ExecutionScope{Destination: request.Executor, ExpectedIncarnationUid: "original", SessionId: request.SessionId}
	secondExecutor.environment = map[string]string{}
	secondExecutor.workspacePrepared = false
	if _, err := secondExecutor.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"echo exactly once","expected_incarnation_uid":"candidate"}`)); err != nil {
		t.Fatal(err)
	}
	if secondOpens != 1 || secondStarts != 2 || secondReads != 2 {
		t.Fatalf("post-restart opens=%d starts=%d reads=%d, want one fresh open and one workspace plus one command", secondOpens, secondStarts, secondReads)
	}
	if err := secondExecutor.close(); err != nil {
		t.Fatal(err)
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func TestExecCommandExpectedIncarnationDoesNotRevealUIDOnAuthorizationFailure(t *testing.T) {
	store, owner, request := resumedExecutorStore(t, protocol.APIStyleResponses)
	if err := store.SetExecutorSession(t.Context(), owner, "retained-executor", nil); err != nil {
		t.Fatal(err)
	}
	client := &fakeExecutionClient{open: func(context.Context, *executorpb.OpenSessionRequest) (executorSessionStream, error) {
		return nil, status.Error(codes.PermissionDenied, "target authorization denied")
	}}
	executor := newFakeExecutor(t, store, owner, client)
	executor.scope = &executorpb.ExecutionScope{Destination: request.Executor, ExpectedIncarnationUid: "original", SessionId: request.SessionId}
	_, err := executor.Invoke(t.Context(), "exec_command", json.RawMessage(`{"cmd":"touch should-not-run","expected_incarnation_uid":"candidate"}`))
	var outcome *toolOutcomeError
	if !errors.As(err, &outcome) || strings.Contains(outcome.toolError.message, "current_incarnation_uid") || strings.Contains(outcome.toolError.message, "candidate") {
		t.Fatalf("authorization failure leaked or accepted incarnation UID: %v", err)
	}
	evidence, ok := outcome.evidence.(executionFailureEvidence)
	if !ok {
		t.Fatalf("authorization failure evidence has type %T, want executionFailureEvidence", outcome.evidence)
	}
	if evidence.CurrentIncarnationUID != "" {
		t.Fatalf("authorization failure evidence leaked current UID: %+v", evidence)
	}
	wire, err := json.Marshal(evidence)
	if err != nil {
		t.Fatalf("marshal authorization failure evidence: %v", err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(wire, &fields); err != nil {
		t.Fatalf("decode authorization failure evidence: %v", err)
	}
	if _, ok := fields["current_incarnation_uid"]; ok {
		t.Fatalf("authorization failure projection leaked current UID field: %s", wire)
	}
	stored, err := store.GetSession(t.Context(), request.SessionId, "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(request, decodeResumeRequest(t, stored.Configuration)) {
		t.Fatal("authorization failure changed the durable scope")
	}
	assertNoUnresolvedNestedIntents(t, store, owner)
}

func resumedExecutorStore(t *testing.T, api protocol.APIStyle) (*Store, Owner, *samplerpb.OpenSessionRequest) {
	t.Helper()
	store := newTestStore(t)
	request := &samplerpb.OpenSessionRequest{SessionId: uuid.NewString(), Spec: &sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CENTRAL_SAMPLER, Cwd: "/fixture", Model: "fixture-model"}, Executor: &sandboxpb.SandboxDestination{Sandbox: "sandbox", SandboxUid: "sandbox-uid", Owner: &sandboxpb.ServiceAccount{Namespace: "agents", Name: "sandbox"}}, ExpectedIncarnationUid: "original"}
	session, err := store.OpenSession(t.Context(), request, "fixture-account", api)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	return store, owner, request
}

func changedIncarnationStatus(t *testing.T, sandboxUID, incarnationUID string) *status.Status {
	t.Helper()
	st, err := status.New(codes.FailedPrecondition, "Sandbox incarnation changed").WithDetails(&executorpb.SandboxIncarnationChanged{SandboxUid: sandboxUID, IncarnationUid: incarnationUID})
	if err != nil {
		t.Fatal(err)
	}
	return st
}

func decodeResumeRequest(t *testing.T, wire []byte) *samplerpb.OpenSessionRequest {
	t.Helper()
	request := &samplerpb.OpenSessionRequest{}
	if err := proto.Unmarshal(wire, request); err != nil {
		t.Fatal(err)
	}
	return request
}
