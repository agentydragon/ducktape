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

	executorpb "github.com/agentydragon/ducktape/agentplane/executor/executorpb"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
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

func TestTransportFailureExposesObservedProcessOutputAndCursor(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	if err := store.RegisterModelProcess(t.Context(), owner, "observed-process", 78, false); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, "observed-process", nil, protocol.ProcessRunning, nil, false); err != nil {
		t.Fatal(err)
	}
	output := newHeadTailBuffer(unifiedExecOutputMaxBytes)
	output.push([]byte("stdout before failure\n"))
	cursor := uint64(8)
	transport := &executorTransportError{
		method: "process/read", phase: "grpc", intentID: "read-intent", processID: "observed-process",
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
	if message.RemoteOutcome != "unknown" || message.AutomaticReplay || message.Method != "process/read" ||
		message.IntentID != "read-intent" || message.ProcessID != "observed-process" || message.AfterSeq == nil || *message.AfterSeq != cursor ||
		message.ObservedOutput.Text != "stdout before failure\n" || message.ObservedOutput.Truncated ||
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
	if encoded, _ := rejected.evidence["buffered_output_base64"].(string); encoded != base64.StdEncoding.EncodeToString(output.retained()) {
		t.Fatalf("full output is missing from FollowEvents evidence: %+v", rejected.evidence)
	}
}

func TestSessionNotificationJournalSurvivesStreamCancellationAndFencesStaleOwner(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	frame := &executorpb.ExecutionNativeFrame{Direction: "receive", Payload: []byte("native notification")}
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
	output := newHeadTailBuffer(16 * 1024)
	output.push(bytes.Repeat([]byte("a"), 32*1024))
	cursor := uint64(12)
	message, err := observedProcessFailureMessage(&executorTransportError{
		method: "process/read", phase: "grpc", intentID: "read-intent", processID: "observed-process",
		afterSequence: &cursor, output: output, cause: errors.New("connection closed"),
	})
	if err != nil {
		t.Fatal(err)
	}
	var parsed observedProcessFailure
	if err := json.Unmarshal([]byte(message), &parsed); err != nil {
		t.Fatal(err)
	}
	view := parsed.ObservedOutput
	if !view.Truncated || view.RetainedBytes != 16*1024 || view.OmittedBytes != 16*1024 ||
		view.DisplayOmittedBytes != 8*1024 || view.ShownHeadBytes != 4*1024 || view.ShownTailBytes != 4*1024 ||
		!strings.Contains(view.Text, "8192 retained bytes not shown; 16384 earlier bytes omitted") {
		t.Fatalf("truncation metadata=%+v", view)
	}
	transport := &executorTransportError{
		method: "process/read", phase: "grpc", intentID: "read-intent", processID: "observed-process",
		afterSequence: &cursor, output: output, cause: errors.New("connection closed"),
	}
	evidence := transport.evidence("")
	full, err := base64.StdEncoding.DecodeString(evidence["buffered_output_base64"].(string))
	if err != nil || !bytes.Equal(full, output.retained()) || evidence["omitted_bytes"] != 16*1024 {
		t.Fatalf("truncated output cannot be retrieved exactly from evidence: bytes=%d omitted=%v err=%v",
			len(full), evidence["omitted_bytes"], err)
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
			_, err := e.rpc(t.Context(), "process/read", executorRead{ProcessID: "existing", MaxBytes: 65536}, nil)
			if err == nil || reads != 0 || opens != 1 {
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
	var rejected *executorRejectedError
	if !errors.As(err, &rejected) || rejected.recorded || isDefinitiveLostExecutorSession(rejected, "retained-executor") {
		t.Fatalf("partial response evidence qualified for session loss recovery: rejection=%+v error=%v", rejected, err)
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
	contextJSON, err := json.Marshal(conversation.Archive)
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
	return &Executor{store: store, owner: owner, client: client, scope: &executorpb.ExecutionScope{SessionId: owner.ConversationID}, sessionID: "retained-executor", lifetime: t.Context(), cwd: "/tmp", processLocks: map[string]*sync.Mutex{}, initializeTimeout: time.Minute}
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
