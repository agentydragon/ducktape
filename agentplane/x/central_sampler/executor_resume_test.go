package centralsampler

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/execution/executorpb"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
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

func resumedExecutorStore(t *testing.T, api protocol.APIStyle) (*Store, Owner, *samplerpb.OpenSessionRequest) {
	t.Helper()
	store := newTestStore(t)
	request := &samplerpb.OpenSessionRequest{SessionId: uuid.NewString(), Spec: &runnerpb.SessionSpec{Harness: runnerpb.Harness_HARNESS_CENTRAL_SAMPLER, Cwd: "/fixture", Model: "fixture-model"}, Executor: &sandboxpb.SandboxDestination{Sandbox: "sandbox", SandboxUid: "sandbox-uid", Owner: &sandboxpb.ServiceAccount{Namespace: "agents", Name: "sandbox"}}, ExpectedIncarnationUid: "original"}
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
