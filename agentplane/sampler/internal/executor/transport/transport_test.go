package transport

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"strconv"
	"sync"
	"testing"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	executorfake "github.com/agentydragon/ducktape/agentplane/testing/executor"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestInitializationHandshakeUsesInitializeTimeout(t *testing.T) {
	client := &executorfake.Client{}
	stream := &executorfake.GatedStream{
		Started: make(chan struct{}), Release: make(chan struct{}), Finished: make(chan struct{}),
		Response: &executorpb.OpenSessionResponse{Status: executorfake.CompletedStatus(), ExecutionSessionId: "fake-execution-session"},
	}
	client.OnOpenSession = func(ctx context.Context, _ *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		stream.Ctx = ctx
		return stream, nil
	}
	transport, _ := newTestTransport(client, nil, "", transportOptions{
		rpcTimeout: 5 * time.Millisecond, initializeTimeout: time.Minute,
	})
	t.Cleanup(func() { _ = transport.Close() })

	result := make(chan error, 1)
	go func() { result <- transport.openSession(t.Context(), nil) }()
	<-stream.Started
	select {
	case err := <-result:
		t.Fatalf("OpenSession ended at the general RPC timeout: %v", err)
	case <-stream.Ctx.Done():
		t.Fatalf("OpenSession stream ended at the general RPC timeout: %v", stream.Ctx.Err())
	case <-time.After(50 * time.Millisecond):
	}
	close(stream.Release)
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	<-stream.Finished
}

func TestInitializationTimeoutKeepsUnknownReceiveOutcome(t *testing.T) {
	client := &executorfake.Client{}
	stream := &executorfake.GatedStream{Started: make(chan struct{}), Release: make(chan struct{}), Finished: make(chan struct{})}
	client.OnOpenSession = func(ctx context.Context, _ *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		stream.Ctx = ctx
		return stream, nil
	}
	transport, journal := newTestTransport(client, nil, "", transportOptions{initializeTimeout: 5 * time.Millisecond})
	t.Cleanup(func() { _ = transport.Close() })

	err := transport.openSession(t.Context(), nil)
	var failure *Failure
	if !errors.As(err, &failure) || failure.Kind != FailureTransport || failure.Phase != "receive" {
		t.Fatalf("initialization timeout=%v, want unknown receive failure", err)
	}
	<-stream.Finished
	result := journal.resultForMethod(MethodInitialize)
	if result["remote_outcome"] != string(Unknown) || result["automatic_replay"] != false || result["phase"] != "receive" {
		t.Fatalf("initialization timeout evidence=%+v", result)
	}
}

func TestOpenSessionTransportFailurePreservesFailurePhase(t *testing.T) {
	for _, tc := range []struct {
		name  string
		phase string
	}{
		{name: "open", phase: "open"},
		{name: "receive", phase: "receive"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client := &executorfake.Client{}
			cause := status.Error(codes.Unavailable, "fake executor route unavailable")
			client.OnOpenSession = func(context.Context, *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
				if tc.phase == "open" {
					return nil, cause
				}
				return executorfake.FailedStream{Err: cause}, nil
			}
			transport, journal := newTestTransport(client, nil, "", transportOptions{})
			t.Cleanup(func() { _ = transport.Close() })

			err := transport.openSession(t.Context(), nil)
			var failure *Failure
			if !errors.As(err, &failure) || failure.Kind != FailureTransport || failure.Phase != tc.phase {
				t.Fatalf("OpenSession error=%v, want transport phase %q", err, tc.phase)
			}
			result := journal.resultForMethod(MethodInitialize)
			if result["phase"] != tc.phase || result["remote_outcome"] != string(Unknown) || result["automatic_replay"] != false {
				t.Fatalf("OpenSession evidence=%+v", result)
			}
		})
	}
}

func TestEnsureSessionPreservesIncarnationFenceAndJournalFailure(t *testing.T) {
	journalFailure := errors.New("fake incarnation-fence journal failure")
	journal := &memoryJournal{finishErr: journalFailure}
	changed, err := status.New(codes.FailedPrecondition, "fake Sandbox incarnation changed").WithDetails(&executorpb.SandboxIncarnationChanged{
		SandboxUid: "fake-sandbox", IncarnationUid: "fake-replacement",
	})
	if err != nil {
		t.Fatal(err)
	}
	client := &executorfake.Client{OnOpenSession: func(context.Context, *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		return nil, changed.Err()
	}}
	transport, _ := newTestTransport(client, journal, "", transportOptions{})
	t.Cleanup(func() { _ = transport.Close() })
	transport.scope = &executorpb.ExecutionScope{
		Destination:            &identitypb.SandboxDestination{Target: &identitypb.EnvironmentTarget{Uid: "fake-sandbox"}},
		ExpectedIncarnationUid: "fake-original", SessionId: "fake-sampler-session",
	}

	candidateUID := "fake-candidate"
	operation, callErr := transport.EnsureSession(WithExpectedIncarnationUID(t.Context(), &candidateUID))
	if callErr == nil || !errors.Is(callErr, journalFailure) {
		t.Fatalf("typed session ensure error=%v, want joined journal failure", callErr)
	}
	var failure *Failure
	if !errors.As(callErr, &failure) || failure.Kind != FailureIncarnationFence {
		t.Fatalf("typed session ensure error=%v, want original incarnation fence retained", callErr)
	}
	if operation.Failure != nil || operation.Outcome != "" {
		t.Fatalf("operational journal failure was converted to a transport result: %+v", operation)
	}
}

func TestReconnectBudgetExhaustionIsNotDispatchedAndKeepsRetainedSession(t *testing.T) {
	client := &executorfake.Client{}
	opens, starts := 0, 0
	client.OnOpenSession = func(_ context.Context, request *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		opens++
		if request.GetResumeSessionId() != "fake-retained-session" {
			t.Fatalf("outage caused a fresh OpenSession: %+v", request)
		}
		return nil, status.Error(codes.Unavailable, "fake retained route unavailable")
	}
	client.OnStartProcess = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		return &executorpb.StartProcessResponse{Status: executorfake.CompletedStatus()}, nil
	}
	transport, journal := newTestTransport(client, nil, "fake-retained-session", transportOptions{reconnectTimeout: 30 * time.Millisecond})
	t.Cleanup(func() { _ = transport.Close() })

	operation, err := transport.Start(t.Context(), StartRequest{ProcessID: "fake-new-process", Argv: []string{"touch", "fake-output"}})
	if err != nil || operation.Outcome != NotDispatched || operation.Failure == nil || operation.Failure.Kind != FailureNotDispatched {
		t.Fatalf("start after reconnect exhaustion=%+v error=%v, want typed not-dispatched", operation, err)
	}
	if opens == 0 || starts != 0 {
		t.Fatalf("reconnect opens=%d process starts=%d, want resume attempts and no mutation", opens, starts)
	}
	if journal.bindings.sessionID != "fake-retained-session" || !journal.bindings.hasSession || len(journal.bindings.retired) != 0 {
		t.Fatalf("reconnect exhaustion changed retained session binding: %+v", journal.bindings)
	}
	for _, request := range journal.requests {
		if request["method"] == string(MethodProcessStart) {
			t.Fatalf("not-dispatched start created a process intent: %+v", request)
		}
	}
}

func TestTypedResponseSerializationPreservesEmptyCollectionsAndBytes(t *testing.T) {
	read, err := responseObject(MethodProcessRead, &executorpb.ReadProcessResponse{
		Status: executorfake.CompletedStatus(), Chunks: []*executorpb.ExecutionOutputChunk{},
	})
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(read)
	if err != nil {
		t.Fatal(err)
	}
	var result ReadResult
	if err := json.Unmarshal(encoded, &result); err != nil {
		t.Fatal(err)
	}
	if result.Chunks == nil {
		t.Fatalf("empty chunks decoded as nil: %s", encoded)
	}
	read, err = responseObject(MethodProcessRead, &executorpb.ReadProcessResponse{
		Status: executorfake.CompletedStatus(),
		Chunks: []*executorpb.ExecutionOutputChunk{{Seq: 1, Stream: "stdout"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if chunks, ok := read["chunks"].([]any); !ok || len(chunks) != 1 || chunks[0].(map[string]any)["chunk"] != "" {
		t.Fatalf("empty process output chunk=%+v, want base64 empty string", read["chunks"])
	}
	file, err := responseObject(MethodFSReadBlock, &executorpb.ReadFileResponse{Status: executorfake.CompletedStatus(), Eof: true})
	if err != nil {
		t.Fatal(err)
	}
	if file["chunk"] != "" {
		t.Fatalf("empty file block=%+v, want base64 empty string", file)
	}
}

func TestReadBlockReturnsBytesAndJournalsOnlyBase64Digest(t *testing.T) {
	chunk := []byte("opaque image block")
	client := &executorfake.Client{OnReadFile: func(context.Context, *executorpb.ReadFileRequest) (*executorpb.ReadFileResponse, error) {
		return &executorpb.ReadFileResponse{Status: executorfake.CompletedStatus(), Data: chunk, Eof: true}, nil
	}}
	transport, journal := newTestTransport(client, nil, "", transportOptions{})
	t.Cleanup(func() { _ = transport.Close() })

	operation, err := transport.ReadBlock(t.Context(), ReadBlockRequest{HandleID: "fake-image", Len: len(chunk)})
	if err != nil || operation.Outcome != Completed || string(operation.Value.Chunk) != string(chunk) || !operation.Value.EOF {
		t.Fatalf("read block=%+v error=%v, want original bytes and EOF", operation, err)
	}
	evidence := journal.resultForMethod(MethodFSReadBlock)
	encoded := base64.StdEncoding.EncodeToString(chunk)
	chunkEvidence, ok := evidence["chunk"].(map[string]any)
	if !ok || chunkEvidence["base64_bytes"] != float64(len(encoded)) {
		t.Fatalf("read block intent evidence=%+v", evidence)
	}
	digest := sha256.Sum256([]byte(encoded))
	if chunkEvidence["base64_sha256"] != hex.EncodeToString(digest[:]) || evidence["outcome"] != string(Completed) || evidence["response"] != nil {
		t.Fatalf("read block intent result evidence=%+v", evidence)
	}
}

// recordOpens records every OpenSession request that client receives and otherwise keeps the
// fake's default stream.
func recordOpens(client *executorfake.Client) func() []*executorpb.OpenSessionRequest {
	var opens []*executorpb.OpenSessionRequest
	defaultOpen := &executorfake.Client{}
	client.OnOpenSession = func(ctx context.Context, request *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		opens = append(opens, proto.Clone(request).(*executorpb.OpenSessionRequest))
		return defaultOpen.OpenSession(ctx, request)
	}
	return func() []*executorpb.OpenSessionRequest { return opens }
}

func TestReadRetryReusesRequestIdentity(t *testing.T) {
	client := &executorfake.Client{}
	var reads []*executorpb.ReadProcessRequest
	client.OnReadProcess = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads = append(reads, proto.Clone(request).(*executorpb.ReadProcessRequest))
		if len(reads) == 1 {
			return nil, status.Error(codes.Unavailable, "fake reply lost after dispatch")
		}
		return &executorpb.ReadProcessResponse{
			Status: executorfake.CompletedStatus(), Chunks: []*executorpb.ExecutionOutputChunk{},
			NextSeq: 8, Exited: true, Closed: true,
		}, nil
	}
	opens := recordOpens(client)
	transport, _ := newTestTransport(client, nil, "", transportOptions{})
	t.Cleanup(func() { _ = transport.Close() })

	cursor := uint64(7)
	operation, err := transport.Read(t.Context(), ReadRequest{ProcessID: "fake-existing-process", AfterSeq: &cursor, MaxBytes: 65536})
	if err != nil || operation.Outcome != Completed {
		t.Fatalf("read operation=%+v error=%v", operation, err)
	}
	if len(reads) != 2 || !proto.Equal(reads[0], reads[1]) {
		t.Fatalf("cursor read changed on retry: %+v", reads)
	}
	if reads[0].GetRequestId() == "" || len(opens()) != 2 || opens()[1].GetResumeSessionId() != "fake-execution-session" {
		t.Fatalf("retry identity/session not retained: reads=%+v opens=%+v", reads, opens())
	}
}

func TestUnknownCursorReadRetriesAtMostOnce(t *testing.T) {
	client := &executorfake.Client{}
	var reads []*executorpb.ReadProcessRequest
	client.OnReadProcess = func(_ context.Context, request *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads = append(reads, proto.Clone(request).(*executorpb.ReadProcessRequest))
		return &executorpb.ReadProcessResponse{Status: &executorpb.ExecutionStatus{
			Outcome: executorpb.ExecutionStatus_OUTCOME_UNKNOWN, Message: "fake adapter cannot recover read outcome",
		}}, nil
	}
	opens := recordOpens(client)
	transport, _ := newTestTransport(client, nil, "", transportOptions{})
	t.Cleanup(func() { _ = transport.Close() })

	cursor := uint64(5)
	operation, err := transport.Read(t.Context(), ReadRequest{ProcessID: "fake-existing-process", AfterSeq: &cursor, MaxBytes: 65536})
	if err != nil || operation.Outcome != Unknown || operation.Failure == nil || operation.Failure.Kind != FailureTransport {
		t.Fatalf("read operation=%+v error=%v, want explicit unknown outcome", operation, err)
	}
	if len(reads) != 2 || !proto.Equal(reads[0], reads[1]) || len(opens()) != 2 || opens()[1].GetResumeSessionId() != "fake-execution-session" {
		t.Fatalf("read retry count=%d open count=%d requests=%+v opens=%+v", len(reads), len(opens()), reads, opens())
	}
}

func TestMutationsAreNeverReplayedAfterLostReply(t *testing.T) {
	for _, tc := range []struct {
		name string
		call func(*Transport, context.Context) (Outcome, *Failure, error)
	}{
		{
			name: "start",
			call: func(transport *Transport, ctx context.Context) (Outcome, *Failure, error) {
				operation, err := transport.Start(ctx, StartRequest{ProcessID: "fake-new-process", Argv: []string{"touch", "fake-once"}, Cwd: "/fake"})
				return operation.Outcome, operation.Failure, err
			},
		},
		{
			name: "write",
			call: func(transport *Transport, ctx context.Context) (Outcome, *Failure, error) {
				operation, err := transport.Write(ctx, WriteRequest{ProcessID: "fake-existing-process", Chunk: []byte("fake input"), WriteID: "fake-write-id"})
				return operation.Outcome, operation.Failure, err
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client := &executorfake.Client{}
			starts, writes := 0, 0
			client.OnStartProcess = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
				starts++
				return nil, status.Error(codes.Unavailable, "fake mutation reply lost")
			}
			client.OnWriteProcess = func(context.Context, *executorpb.WriteProcessRequest) (*executorpb.WriteProcessResponse, error) {
				writes++
				return nil, status.Error(codes.Unavailable, "fake mutation reply lost")
			}
			transport, _ := newTestTransport(client, nil, "", transportOptions{})
			t.Cleanup(func() { _ = transport.Close() })

			outcome, failure, err := tc.call(transport, t.Context())
			if err != nil || outcome != Unknown || failure == nil || failure.Kind != FailureTransport || failure.IntentID == "" {
				t.Fatalf("mutation outcome=%q failure=%+v error=%v", outcome, failure, err)
			}
			if starts != boolInt(tc.name == "start") || writes != boolInt(tc.name == "write") {
				t.Fatalf("mutation replayed: starts=%d writes=%d", starts, writes)
			}
		})
	}
}

func TestRecoveryNeverSubstitutesMissingRetainedSession(t *testing.T) {
	for _, tc := range []struct {
		name     string
		response *executorpb.OpenSessionResponse
		openErr  error
	}{
		{name: "unknown outcome", response: &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_UNKNOWN, Message: "fake session outcome unknown"}}},
		{name: "rejection", response: &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, Message: "fake access denied", UpstreamCode: int32Pointer(-32600)}}},
		{name: "different rejected session", response: &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, Message: "unknown session id fake-other-session", UpstreamCode: int32Pointer(-32600)}}},
		{name: "different completed session", response: &executorpb.OpenSessionResponse{Status: executorfake.CompletedStatus(), ExecutionSessionId: "fake-other-session"}},
		{name: "unavailable adapter", openErr: status.Error(codes.Unavailable, "fake adapter unavailable")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client := &executorfake.Client{}
			opens, reads := 0, 0
			client.OnOpenSession = func(ctx context.Context, _ *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
				opens++
				if tc.openErr != nil {
					return nil, tc.openErr
				}
				return &executorfake.Stream{Ctx: ctx, Initial: tc.response}, nil
			}
			client.OnReadProcess = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
				reads++
				return &executorpb.ReadProcessResponse{Status: executorfake.CompletedStatus()}, nil
			}
			transport, journal := newTestTransport(client, nil, "fake-retained-session", transportOptions{reconnectTimeout: 120 * time.Millisecond})
			t.Cleanup(func() { _ = transport.Close() })

			operation, err := transport.Read(t.Context(), ReadRequest{ProcessID: "fake-existing-process", MaxBytes: 65536})
			if err != nil || operation.Outcome != NotDispatched {
				t.Fatalf("recovery outcome=%q error=%v, want request refused before dispatch", operation.Outcome, err)
			}
			if reads != 0 || opens < 1 {
				t.Fatalf("session recovery dispatched a read or skipped resume: opens=%d reads=%d", opens, reads)
			}
			if journal.bindings.sessionID != "fake-retained-session" || !journal.bindings.hasSession || len(journal.bindings.retired) != 0 {
				t.Fatalf("recovery substituted or retired the retained binding: %+v", journal.bindings)
			}
		})
	}
}

func TestOpenRejectionRequiresNativeEvidenceBeforeRecovery(t *testing.T) {
	frameErr := errors.New("fake native response persistence failed")
	journal := &memoryJournal{frameErr: frameErr}
	transport, _ := newTestTransport(&executorfake.Client{}, journal, "", transportOptions{})
	response := &executorpb.OpenSessionResponse{Status: &executorpb.ExecutionStatus{
		Outcome: executorpb.ExecutionStatus_OUTCOME_REJECTED, UpstreamCode: int32Pointer(-32600), Message: "unknown session id fake-retained-session",
	}}
	frameErrObserved := transport.recordNativeFrames(t.Context(), "fake-intent", "fake-retained-session", MethodInitialize, []*executorpb.ExecutionNativeFrame{{Direction: "response", Payload: []byte("fake frame")}})
	err := transport.finishOpenFailure(t.Context(), "fake-intent", response, frameErrObserved)
	if isExecutorConnectionFailure(err) || !errors.Is(err, frameErr) {
		t.Fatalf("rejected open with missing native evidence error=%v", err)
	}
	result := journal.result("fake-intent")
	if result["outcome"] != string(Rejected) {
		t.Fatalf("typed rejection was not durably settled: %+v", result)
	}
}

func TestNextOperationResumesAfterMutationFailureWithoutReplay(t *testing.T) {
	client := &executorfake.Client{}
	starts, reads := 0, 0
	client.OnStartProcess = func(context.Context, *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		starts++
		return nil, status.Error(codes.Unavailable, "fake start reply lost")
	}
	client.OnReadProcess = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		reads++
		return &executorpb.ReadProcessResponse{Status: executorfake.CompletedStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}}, nil
	}
	opens := recordOpens(client)
	transport, _ := newTestTransport(client, nil, "", transportOptions{})
	t.Cleanup(func() { _ = transport.Close() })

	start, err := transport.Start(t.Context(), StartRequest{ProcessID: "fake-lost-start", Argv: []string{"touch", "fake-once"}})
	if err != nil || start.Outcome != Unknown || start.Failure == nil {
		t.Fatalf("lost start operation=%+v error=%v", start, err)
	}
	read, err := transport.Read(t.Context(), ReadRequest{ProcessID: "fake-existing-process", MaxBytes: 65536})
	if err != nil || read.Outcome != Completed {
		t.Fatalf("next read operation=%+v error=%v", read, err)
	}
	if starts != 1 || reads != 1 || len(opens()) != 2 || opens()[1].GetResumeSessionId() != "fake-execution-session" {
		t.Fatalf("starts=%d reads=%d resumes=%+v", starts, reads, opens())
	}
}

func TestRetryableResumeFailureClassification(t *testing.T) {
	for _, tc := range []struct {
		name    string
		failure *Failure
		want    bool
	}{
		{name: "unavailable while opening", failure: &Failure{Kind: FailureTransport, Phase: "open", Cause: status.Error(codes.Unavailable, "fake gateway unavailable")}, want: true},
		{name: "deadline while receiving", failure: &Failure{Kind: FailureTransport, Phase: "receive", Cause: status.Error(codes.DeadlineExceeded, "fake resume deadline")}, want: true},
		{name: "refusal after response", failure: &Failure{Kind: FailureTransport, Phase: "response", Cause: status.Error(codes.Unavailable, "fake resume refused")}},
		{name: "permission denied", failure: &Failure{Kind: FailureTransport, Phase: "open", Cause: status.Error(codes.PermissionDenied, "fake resume denied")}},
		{name: "control route unavailable during initialize", failure: &Failure{Kind: FailureEnvironment, Method: MethodInitialize, EnvironmentState: executorpb.ExecutionEnvironmentFailure_STATE_CONTROL_ROUTE_UNAVAILABLE.String(), RemoteOutcome: NotDispatched}, want: true},
		{name: "control route unavailable for read", failure: &Failure{Kind: FailureEnvironment, Method: MethodProcessRead, EnvironmentState: executorpb.ExecutionEnvironmentFailure_STATE_CONTROL_ROUTE_UNAVAILABLE.String(), RemoteOutcome: NotDispatched}},
		{name: "other environment state", failure: &Failure{Kind: FailureEnvironment, Method: MethodInitialize, EnvironmentState: executorpb.ExecutionEnvironmentFailure_STATE_NOT_READY.String(), RemoteOutcome: NotDispatched}},
		{name: "unknown initialize outcome", failure: &Failure{Kind: FailureEnvironment, Method: MethodInitialize, EnvironmentState: executorpb.ExecutionEnvironmentFailure_STATE_CONTROL_ROUTE_UNAVAILABLE.String(), RemoteOutcome: Unknown}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := retryableExecutorResumeFailure(tc.failure); got != tc.want {
				t.Fatalf("retryableExecutorResumeFailure(%+v)=%t, want %t", tc.failure, got, tc.want)
			}
		})
	}
}

func TestReconnectStopsOnCallerCancellation(t *testing.T) {
	client := &executorfake.Client{}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	resumes := 0
	client.OnOpenSession = func(_ context.Context, request *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		resumes++
		if request.GetResumeSessionId() != "fake-retained-session" {
			t.Fatalf("caller cancellation caused a fresh OpenSession: %+v", request)
		}
		cancel()
		return nil, status.Error(codes.Unavailable, "fake gateway unavailable")
	}
	transport, journal := newTestTransport(client, nil, "fake-retained-session", transportOptions{reconnectTimeout: 2 * time.Second})
	t.Cleanup(func() { _ = transport.Close() })

	operation, err := transport.EnsureSession(ctx)
	if !errors.Is(err, context.Canceled) || resumes != 1 || operation.Outcome != "" {
		t.Fatalf("reconnect ignored cancellation: attempts=%d operation=%+v error=%v", resumes, operation, err)
	}
	if journal.bindings.sessionID != "fake-retained-session" || !journal.bindings.hasSession {
		t.Fatalf("cancellation replaced native binding: %+v", journal.bindings)
	}
}

func TestReconnectDiscardsLateFirstFrameBeforeNextResume(t *testing.T) {
	client := &executorfake.Client{}
	firstDone := make(chan struct{})
	opens := 0
	client.OnOpenSession = func(ctx context.Context, request *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		opens++
		if request.GetResumeSessionId() != "fake-retained-session" {
			t.Fatalf("resume changed native session: %+v", request)
		}
		if opens == 1 {
			return &executorfake.LateSuccessStream{Ctx: ctx, Done: firstDone}, nil
		}
		select {
		case <-firstDone:
		default:
			t.Fatal("new resume overlapped the canceled stream")
		}
		return &executorfake.Stream{Ctx: ctx, Initial: &executorpb.OpenSessionResponse{Status: executorfake.CompletedStatus(), ExecutionSessionId: "fake-retained-session"}}, nil
	}
	transport, _ := newTestTransport(client, nil, "fake-retained-session", transportOptions{reconnectTimeout: 2 * time.Second})
	t.Cleanup(func() { _ = transport.Close() })

	operation, err := transport.EnsureSession(t.Context())
	if err != nil || operation.Outcome != Completed {
		t.Fatalf("resume operation=%+v error=%v", operation, err)
	}
	if opens != 2 || transport.sessionGeneration != 1 || transport.sessionID != "fake-retained-session" {
		t.Fatalf("late first frame became a bound session: opens=%d generation=%d session=%q", opens, transport.sessionGeneration, transport.sessionID)
	}
}

func TestWatcherJournalsNotificationAfterStreamCancellation(t *testing.T) {
	streamCtx, cancel := context.WithCancel(context.Background())
	defer cancel()
	frame := &executorpb.ExecutionNativeFrame{Direction: "notification", Payload: []byte("fake native notification")}
	journal := &memoryJournal{}
	journal.onFrame = func(ctx context.Context, _ NativeFrame) error {
		cancel()
		if err := ctx.Err(); err != nil {
			return err
		}
		return nil
	}
	broadcaster := &testBroadcaster{}
	transport, _ := newTestTransport(&executorfake.Client{}, journal, "", transportOptions{processChanges: broadcaster})
	transport.sessionGeneration = 1
	stream := &executorfake.NotificationStream{Ctx: streamCtx, Responses: make(chan *executorpb.OpenSessionResponse, 1)}
	stream.Responses <- &executorpb.OpenSessionResponse{ExecutionSessionId: "fake-session", NativeFrames: []*executorpb.ExecutionNativeFrame{frame}}
	done := make(chan struct{})
	transport.watchSession(streamCtx, stream, "fake-notification-intent", "fake-session", 1, cancel, done)
	<-done

	frames := journal.framesSnapshot()
	if len(frames) != 1 || frames[0].OperationID != "fake-notification-intent" || frames[0].ExecutionSession != "fake-session" || string(frames[0].Payload) != string(frame.Payload) {
		t.Fatalf("notification evidence=%+v", frames)
	}
	if broadcaster.count != 2 {
		t.Fatalf("process wakeups=%d, want notification and stream-exit wakeups", broadcaster.count)
	}
}

func TestStaleWatcherCannotFenceCurrentSessionGeneration(t *testing.T) {
	transport, _ := newTestTransport(&executorfake.Client{}, nil, "", transportOptions{})
	transport.sessionGeneration = 2
	transport.reconnectRequired = false
	streamCtx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	transport.watchSession(streamCtx, executorfake.FailedStream{Err: io.EOF}, "fake-old-intent", "fake-old-session", 1, cancel, done)
	<-done
	if transport.reconnectRequired {
		t.Fatal("stale watcher marked the current session for reconnect")
	}
}

func TestFailureEvidencePreservesFullWidthCursorAtJSONBoundary(t *testing.T) {
	cursor := ^uint64(0)
	failure := &Failure{Method: MethodProcessRead, AfterSequence: &cursor, RemoteOutcome: Unknown}
	wire, err := json.Marshal(failure.IntentEvidence())
	if err != nil {
		t.Fatal(err)
	}
	var decoded IntentEvidence
	if err := json.Unmarshal(wire, &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded.AfterSeq == nil || *decoded.AfterSeq != cursor {
		t.Fatalf("full-width cursor did not survive JSON boundary: wire=%s decoded=%+v", wire, decoded)
	}
}

type transportOptions struct {
	rpcTimeout        time.Duration
	initializeTimeout time.Duration
	reconnectTimeout  time.Duration
	processChanges    ProcessChangeBroadcaster
}

func newTestTransport(client *executorfake.Client, journal *memoryJournal, initialSessionID string, options transportOptions) (*Transport, *memoryJournal) {
	if journal == nil {
		journal = &memoryJournal{}
	}
	bindings := newMemoryBindings(initialSessionID)
	journal.bindings = bindings
	if options.initializeTimeout == 0 {
		options.initializeTimeout = time.Minute
	}
	if options.reconnectTimeout == 0 {
		options.reconnectTimeout = 2 * time.Minute
	}
	if options.processChanges == nil {
		options.processChanges = &testBroadcaster{}
	}
	return New(Config{
		Client: client, Journal: journal, Bindings: bindings, ProcessChanges: options.processChanges,
		Scope: &executorpb.ExecutionScope{SessionId: "fake-sampler-session"}, Lifetime: context.Background(),
		InitialSessionID: initialSessionID, RPCTimeout: options.rpcTimeout,
		InitializeTimeout: options.initializeTimeout, ReconnectTimeout: options.reconnectTimeout,
	}), journal
}

type memoryJournal struct {
	mu        sync.Mutex
	bindings  *memoryBindings
	nextID    int
	requests  map[string]map[string]any
	results   map[string]json.RawMessage
	frames    []NativeFrame
	frameErr  error
	finishErr error
	onFrame   func(context.Context, NativeFrame) error
}

func (j *memoryJournal) BeginExecutorIntent(_ context.Context, request map[string]any) (string, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.requests == nil {
		j.requests = map[string]map[string]any{}
	}
	j.nextID++
	id := "fake-intent-" + strconv.Itoa(j.nextID)
	j.requests[id] = request
	return id, nil
}

func (j *memoryJournal) FinishExecutorIntent(_ context.Context, id string, evidence any) error {
	if j.finishErr != nil {
		return j.finishErr
	}
	wire, err := json.Marshal(evidence)
	if err != nil {
		return err
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.results == nil {
		j.results = map[string]json.RawMessage{}
	}
	j.results[id] = wire
	return nil
}

func (j *memoryJournal) RecordExecutorNativeFrame(ctx context.Context, frame NativeFrame) error {
	frame.Payload = append([]byte(nil), frame.Payload...)
	j.mu.Lock()
	j.frames = append(j.frames, frame)
	frameErr, onFrame := j.frameErr, j.onFrame
	j.mu.Unlock()
	if onFrame != nil {
		if err := onFrame(ctx, frame); err != nil {
			return err
		}
	}
	return frameErr
}

func (j *memoryJournal) resultForMethod(method Method) map[string]any {
	j.mu.Lock()
	defer j.mu.Unlock()
	for id, request := range j.requests {
		if request["method"] != string(method) && request["method"] != method {
			continue
		}
		var result map[string]any
		if err := json.Unmarshal(j.results[id], &result); err != nil {
			return nil
		}
		return result
	}
	return nil
}

func (j *memoryJournal) result(id string) map[string]any {
	j.mu.Lock()
	defer j.mu.Unlock()
	var result map[string]any
	if err := json.Unmarshal(j.results[id], &result); err != nil {
		return nil
	}
	return result
}

func (j *memoryJournal) framesSnapshot() []NativeFrame {
	j.mu.Lock()
	defer j.mu.Unlock()
	frames := make([]NativeFrame, len(j.frames))
	copy(frames, j.frames)
	return frames
}

type memoryBindings struct {
	mu         sync.Mutex
	sessionID  string
	hasSession bool
	retired    []string
}

func newMemoryBindings(sessionID string) *memoryBindings {
	return &memoryBindings{sessionID: sessionID, hasSession: sessionID != ""}
}

func (b *memoryBindings) CurrentSession(context.Context) (string, bool, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.sessionID, b.hasSession, nil
}

func (b *memoryBindings) BindSession(_ context.Context, sessionID string, _ *string) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.sessionID, b.hasSession = sessionID, sessionID != ""
	return nil
}

func (b *memoryBindings) BindInitialSession(ctx context.Context, _, _ string, sessionID string, shell *string) error {
	return b.BindSession(ctx, sessionID, shell)
}

func (b *memoryBindings) RetireLostSession(_ context.Context, sessionID, _ string) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.retired = append(b.retired, sessionID)
	if b.sessionID == sessionID {
		b.sessionID, b.hasSession = "", false
	}
	return nil
}

func (b *memoryBindings) RebindIncarnation(context.Context, string, string, string, string) error {
	return nil
}

func (b *memoryBindings) RebindIncarnationWithSession(context.Context, string, string, string, string, *string) error {
	return nil
}

type testBroadcaster struct {
	mu    sync.Mutex
	count int
}

func (b *testBroadcaster) SignalProcessChange() {
	b.mu.Lock()
	b.count++
	b.mu.Unlock()
}

func int32Pointer(value int32) *int32 { return &value }

func boolInt(value bool) int {
	if value {
		return 1
	}
	return 0
}

func TestFirstOpenBindsReturnedIncarnationAndFencesResume(t *testing.T) {
	client := &executorfake.Client{}
	calls := 0
	client.OnOpenSession = func(ctx context.Context, request *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
		calls++
		if calls == 1 {
			if request.Scope.GetExpectedIncarnationUid() != "" || request.ResumeSessionId != nil {
				t.Fatalf("first open carried a fence: %v", request)
			}
		} else if request.Scope.GetExpectedIncarnationUid() != "verified" || request.GetResumeSessionId() != "native-session" {
			t.Fatalf("resume lost binding: %v", request)
		}
		return &executorfake.Stream{Ctx: ctx, Initial: &executorpb.OpenSessionResponse{Status: executorfake.CompletedStatus(), ExecutionSessionId: "native-session", IncarnationUid: "verified"}}, nil
	}
	transport, journal := newTestTransport(client, nil, "", transportOptions{})
	t.Cleanup(func() { _ = transport.Close() })
	transport.setScope(&executorpb.ExecutionScope{Destination: &identitypb.SandboxDestination{Target: &identitypb.EnvironmentTarget{Uid: "sandbox-uid"}}, SessionId: "sampler-session"})
	if err := transport.openSession(t.Context(), nil); err != nil {
		t.Fatal(err)
	}
	if transport.ScopeSnapshot().GetExpectedIncarnationUid() != "verified" {
		t.Fatal("first response did not establish scope")
	}
	if retained, exists, err := journal.bindings.CurrentSession(t.Context()); err != nil || !exists || retained != "native-session" {
		t.Fatalf("binding=%q exists=%t err=%v", retained, exists, err)
	}
	if err := transport.stopCurrentSessionStream(t.Context()); err != nil {
		t.Fatal(err)
	}
	sessionID := "native-session"
	if err := transport.openSession(t.Context(), &sessionID); err != nil {
		t.Fatal(err)
	}
}

func TestOpenRejectsMissingOrMismatchedIncarnation(t *testing.T) {
	for _, tc := range []struct{ name, expected, returned string }{
		{name: "unbound without response incarnation"},
		{name: "bound with another response incarnation", expected: "established", returned: "other"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client := &executorfake.Client{OnOpenSession: func(ctx context.Context, _ *executorpb.OpenSessionRequest) (grpc.ServerStreamingClient[executorpb.OpenSessionResponse], error) {
				return &executorfake.Stream{Ctx: ctx, Initial: &executorpb.OpenSessionResponse{Status: executorfake.CompletedStatus(), ExecutionSessionId: "native-session", IncarnationUid: tc.returned}}, nil
			}}
			transport, journal := newTestTransport(client, nil, "", transportOptions{})
			t.Cleanup(func() { _ = transport.Close() })
			transport.setScope(&executorpb.ExecutionScope{Destination: &identitypb.SandboxDestination{Target: &identitypb.EnvironmentTarget{Uid: "sandbox-uid"}}, ExpectedIncarnationUid: tc.expected, SessionId: "sampler-session"})
			if err := transport.openSession(t.Context(), nil); err == nil {
				t.Fatal("accepted invalid incarnation response")
			}
			if _, exists, _ := journal.bindings.CurrentSession(t.Context()); exists {
				t.Fatal("invalid response installed native binding")
			}
		})
	}
}
