package centralsampler

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"sync"
	"testing"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/executor/executorpb"
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

func TestTypedReadResponseKeepsEmptyChunksAsAnArray(t *testing.T) {
	response := &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}}
	value, err := responseObject("process/read", response)
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
	readProcess, err := responseObject("process/read", &executorpb.ReadProcessResponse{
		Status: completedExecutionStatus(),
		Chunks: []*executorpb.ExecutionOutputChunk{{Seq: 1, Stream: "stdout"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if chunks, ok := readProcess["chunks"].([]any); !ok || len(chunks) != 1 || chunks[0].(map[string]any)["chunk"] != "" {
		t.Fatalf("empty process output chunk=%+v, want base64 empty string", readProcess["chunks"])
	}

	readFile, err := responseObject("fs/readBlock", &executorpb.ReadFileResponse{Status: completedExecutionStatus(), Eof: true})
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
	result, err := executorRPCWithEvidence[executorFSBlock](t.Context(), e, "fs/readBlock", executorFSReadBlock{HandleID: "image", Len: len(chunk)}, blockEvidence)
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
	if _, err := store.RecoverSession(t.Context(), session.ID, "fixture-account", blocked.Cursor, "remote process outcome reviewed"); err != nil {
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

func TestExecutorDrainsOutputThatArrivesAfterExit(t *testing.T) {
	for _, operation := range []string{"setup", "command", "reconcile"} {
		t.Run(operation, func(t *testing.T) {
			store, owner := claimedExecutorStore(t)
			reads := 0
			client := &fakeExecutionClient{}
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
			e := newFakeExecutor(t, store, owner, client)
			var output string
			var code int
			var err error
			if operation == "setup" {
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
				if operation == "reconcile" {
					for range 3 {
						if _, err := e.ReconcileProcesses(t.Context(), time.Second); err != nil {
							t.Fatal(err)
						}
					}
				}
				observed, readErr := e.readProcess(t.Context(), "late-output", 1000)
				err = readErr
				if err == nil {
					output, code = string(observed.output.retained()), *observed.exitCode
				}
			}
			if err != nil || code != 23 || output != "late stdout\n" || reads != 3 {
				t.Fatalf("exit=%d output=%q reads=%d error=%v", code, output, reads, err)
			}
		})
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
	_, err := executorRPC[executorReadResult](t.Context(), e, "process/read", executorRead{ProcessID: "existing-process", AfterSeq: &cursor, MaxBytes: 65536})
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
	_, err := executorRPC[executorReadResult](t.Context(), e, "process/read", executorRead{ProcessID: "existing-process", AfterSeq: &cursor, MaxBytes: 65536})
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
	for _, method := range []string{"process/start", "process/write"} {
		t.Run(method, func(t *testing.T) {
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
			if method == "process/start" {
				params = executorStart{ProcessID: "lost-start", Argv: []string{"touch", "once"}, Cwd: "file:///tmp"}
			} else {
				params = executorWrite{ProcessID: "existing", Chunk: []byte("x"), WriteID: "write-once"}
			}
			_, err := e.rpc(t.Context(), method, params, nil)
			var transport *executorTransportError
			if !errors.As(err, &transport) || !strings.Contains(transport.Error(), "remote effect outcome is unknown") {
				t.Fatalf("mutation result=%v", err)
			}
			if starts != boolInt(method == "process/start") || writes != boolInt(method == "process/write") {
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
		{name: "unknown retained session", response: &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_UNKNOWN, Message: "unknown retained session"}}},
		{name: "different session", response: &executorpb.OpenSessionResponse{Status: completedExecutionStatus(), ExecutionSessionId: "new-executor"}},
		{name: "unavailable adapter", openErr: status.Error(codes.Unavailable, "adapter unavailable")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store, owner := claimedExecutorStore(t)
			client := &fakeExecutionClient{}
			reads := 0
			client.open = func(ctx context.Context, _ *executorpb.OpenSessionRequest) (executorSessionStream, error) {
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
			_, err := e.rpc(t.Context(), "process/read", executorRead{ProcessID: "existing", MaxBytes: 65536}, nil)
			if err == nil || reads != 0 {
				t.Fatalf("session substitution: err=%v reads=%d", err, reads)
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
	_, err := e.rpc(t.Context(), "process/start", executorStart{ProcessID: "lost-start", Argv: []string{"touch", "once"}, Cwd: "file:///tmp"}, nil)
	if !errors.As(err, new(*executorTransportError)) {
		t.Fatalf("start error=%v, want explicit unknown result", err)
	}
	_, err = e.rpc(t.Context(), "process/read", executorRead{ProcessID: "existing", MaxBytes: 65536}, nil)
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
	return &Executor{store: store, owner: owner, client: client, scope: &executorpb.ExecutionScope{SessionId: owner.ConversationID}, sessionID: "retained-executor", lifetime: t.Context(), cwd: "/tmp", processLocks: map[string]*sync.Mutex{}}
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
