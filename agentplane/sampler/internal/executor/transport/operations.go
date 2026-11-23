package transport

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/jsonwire"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/outputbuffer"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type executorEnvPolicy struct {
	Inherit               string            `json:"inherit"`
	IgnoreDefaultExcludes bool              `json:"ignoreDefaultExcludes"`
	Exclude               []string          `json:"exclude"`
	Set                   map[string]string `json:"set"`
	IncludeOnly           []string          `json:"includeOnly"`
}
type executorStart struct {
	ProcessID string             `json:"processId"`
	Argv      []string           `json:"argv"`
	Cwd       string             `json:"cwd"`
	EnvPolicy *executorEnvPolicy `json:"envPolicy,omitempty"`
	Env       map[string]string  `json:"env"`
	TTY       bool               `json:"tty"`
	PipeStdin bool               `json:"pipeStdin,omitempty"`
	Arg0      *string            `json:"arg0,omitempty"`
}
type executorRead struct {
	ProcessID      string               `json:"processId"`
	AfterSeq       *uint64              `json:"afterSeq"`
	MaxBytes       int                  `json:"maxBytes"`
	WaitMS         int                  `json:"waitMs"`
	ObservedOutput *outputbuffer.Buffer `json:"-"`
}
type executorWrite struct {
	ProcessID string `json:"processId"`
	Chunk     []byte `json:"chunk"`
	WriteID   string `json:"writeId"`
}
type executorSignal struct {
	ProcessID string `json:"processId"`
	Signal    string `json:"signal"`
}
type executorTerminate struct {
	ProcessID string `json:"processId"`
}
type executorFSGetMetadata struct {
	Path string `json:"path"`
}
type executorFSOpen struct {
	HandleID string `json:"handleId"`
	Path     string `json:"path"`
	Mode     string `json:"mode"`
}
type executorFSReadBlock struct {
	HandleID string `json:"handleId"`
	Offset   uint64 `json:"offset"`
	Len      int    `json:"len"`
}
type executorFSClose struct {
	HandleID string `json:"handleId"`
}

func (e *Transport) rpc(ctx context.Context, method Method, params any) (Operation[json.RawMessage], error) {
	e.rpcMu.Lock()
	defer e.rpcMu.Unlock()
	var operation Operation[json.RawMessage]
	if err := e.ensureSessionLocked(ctx, method, executorProcessID(params)); err != nil {
		failure := failureFromError(err)
		if failure == nil {
			return operation, err
		}
		return failureOperation[json.RawMessage](failure), nil
	}
	if e.rpcTimeout > 0 {
		var cancel context.CancelFunc
		timeout := e.rpcTimeout
		if method == MethodProcessRead {
			timeout = e.ReadBudget(timeout)
		}
		ctx, cancel = context.WithTimeout(ctx, timeout)
		defer cancel()
	}
	requestJSON, err := jsonwire.Object(map[string]any{"method": method, "params": params})
	if err != nil {
		return operation, err
	}
	intent, err := e.journal.BeginExecutorIntent(ctx, requestJSON)
	if err != nil {
		return operation, err
	}
	request, err := e.typedRequest(ctx, method, params, intent)
	if err != nil {
		finishErr := e.finishNested(ctx, intent, map[string]any{"outcome": NotDispatched, "request_id": intent, "method": method, "reason": err.Error()})
		return operation, errors.Join(err, finishErr)
	}
	response, err := e.callWithReadDeadline(ctx, method, request)
	readRetried := false
	if err != nil {
		remoteOutcome := NotDispatched
		if method == MethodProcessRead {
			// The read did not reach the current execution incarnation, but the process
			// may still be running from an earlier successful StartProcess.
			remoteOutcome = Unknown
		}
		scope := executionScopeFromContext(ctx, e.ScopeSnapshot())
		if fence := e.incarnationFenceError(err, scope, method, intent, executorProcessID(params), e.sessionID, nil, nil, remoteOutcome); fence != nil {
			if read, ok := params.(executorRead); ok && method == MethodProcessRead {
				fence.AfterSequence, fence.Output = read.AfterSeq, read.ObservedOutput
			}
			e.setReconnectRequired(true)
			evidence, finishErr := e.finishOperation(ctx, intent, fence.IntentEvidence())
			if finishErr != nil {
				return operation, fmt.Errorf("record stale execution incarnation for %s after %v: %w", method, fence, finishErr)
			}
			fence.Recorded = true
			fence.Evidence = evidence
			return failureOperation[json.RawMessage](fence), nil
		}
	}
	var transport *Failure
	if err != nil {
		transport = failureFromError(err)
	}
	if transport != nil {
		if transport.Kind == FailureTransport {
			e.setReconnectRequired(true)
		}
		if transport.Kind == FailureTransport && method == MethodProcessRead && ctx.Err() == nil {
			if reconnectErr := e.reconnectLocked(ctx, executorProcessID(params)); reconnectErr == nil {
				readRetried = true
				response, err = e.callWithReadDeadline(ctx, method, request)
			} else if !isExecutorConnectionFailure(reconnectErr) {
				_, finishErr := e.finishOperation(ctx, intent, transport.IntentEvidence())
				return operation, fmt.Errorf("resume executor state after %v: %w", transport, errors.Join(reconnectErr, finishErr))
			} else {
				transport.Cause = errors.Join(transport.Cause, fmt.Errorf("resume executor session: %w", reconnectErr))
			}
		}
	}
	if err != nil {
		var failure *Failure
		if failure = failureFromError(err); failure != nil && failure.Kind == FailureEnvironment {
			failure.IntentID = intent
			failure.ProcessID = executorProcessID(params)
			failure.ExecutionSessionID = e.sessionID
			if read, ok := params.(executorRead); ok {
				failure.AfterSequence = read.AfterSeq
				failure.Output = read.ObservedOutput
				// A failed read cannot establish whether the already-started process exited.
				failure.RemoteOutcome = Unknown
			}
			e.setReconnectRequired(true)
			evidence, finishErr := e.finishOperation(ctx, intent, failure.IntentEvidence())
			if finishErr != nil {
				return operation, fmt.Errorf("record known typed execution %s refusal after %v: %w", method, failure, finishErr)
			}
			failure.Recorded = true
			failure.Evidence = evidence
			return failureOperation[json.RawMessage](failure), nil
		}
		if transport = failureFromError(err); transport != nil && transport.Kind == FailureTransport {
			transport.IntentID, transport.ProcessID = intent, executorProcessID(params)
			transport.ExecutionSessionID = e.retainedSessionID(ctx)
			e.setReconnectRequired(true)
			evidence, finishErr := e.finishOperation(ctx, intent, transport.IntentEvidence())
			if finishErr != nil {
				return operation, fmt.Errorf("record unknown typed execution %s outcome after %v: %w", method, transport, finishErr)
			}
			transport.Recorded = true
			transport.Evidence = evidence
			return failureOperation[json.RawMessage](transport), nil
		}
		if failure = failureFromError(err); failure != nil {
			return failureOperation[json.RawMessage](failure), nil
		}
		return operation, err
	}
	statusValue := executionStatus(response)
	frames := executionFrames(response)
	frameErr := e.recordNativeFrames(ctx, intent, e.sessionID, method, frames)
	var frameJournalErr error
	if frameErr != nil {
		frameJournalErr = fmt.Errorf("record typed %s native evidence: %w", method, frameErr)
		e.setSessionError(frameJournalErr)
		e.setReconnectRequired(true)
	}
	if statusValue == nil {
		statusValue = &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_UNKNOWN, Message: "typed execution response omitted its status"}
	}
	if statusValue.Outcome == executorpb.ExecutionStatus_OUTCOME_UNKNOWN {
		if method == MethodProcessRead && !readRetried && ctx.Err() == nil && frameErr == nil {
			e.setReconnectRequired(true)
			if reconnectErr := e.reconnectLocked(ctx, executorProcessID(params)); reconnectErr == nil {
				readRetried = true
				response, err = e.callWithReadDeadline(ctx, method, request)
				if err == nil {
					statusValue = executionStatus(response)
					frames = executionFrames(response)
					frameErr = errors.Join(frameErr, e.recordNativeFrames(ctx, intent, e.sessionID, method, frames))
					if frameErr != nil && frameJournalErr == nil {
						frameJournalErr = fmt.Errorf("record typed %s native evidence: %w", method, frameErr)
						e.setSessionError(frameJournalErr)
						e.setReconnectRequired(true)
					}
				} else {
					statusValue.Message = errors.Join(errors.New(statusValue.Message), err).Error()
				}
			} else if !isExecutorConnectionFailure(reconnectErr) {
				transport := &Failure{Kind: FailureTransport, Method: method, Phase: "native", IntentID: intent, ProcessID: executorProcessID(params), ExecutionSessionID: e.sessionID, Cause: errors.New(statusValue.Message)}
				_, finishErr := e.finishOperation(ctx, intent, transport.IntentEvidence())
				return operation, fmt.Errorf("resume executor state after %v: %w", transport, errors.Join(reconnectErr, finishErr))
			} else if statusValue.Message == "" {
				statusValue.Message = reconnectErr.Error()
			}
		}
	}
	if statusValue == nil {
		statusValue = &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_UNKNOWN, Message: "typed execution response omitted its status"}
	}
	if statusValue.Outcome == executorpb.ExecutionStatus_OUTCOME_REJECTED {
		message := statusValue.Message
		if message == "" {
			message = "request rejected"
		}
		rejected := map[string]any{"code": statusValue.UpstreamCode, "message": message}
		evidenceJSON, finishErr := e.finishOperation(ctx, intent, map[string]any{"outcome": Rejected, "request_id": intent, "execution_session_id": e.sessionID, "rejected": rejected})
		if frameJournalErr != nil {
			return operation, errors.Join(frameJournalErr, finishErr)
		}
		if finishErr != nil {
			return operation, fmt.Errorf("record rejected typed execution %s result: %w", method, finishErr)
		}
		var afterSequence *uint64
		if read, ok := params.(executorRead); ok {
			afterSequence = read.AfterSeq
		}
		failure := &Failure{
			Kind: FailureRejected, Method: method, RejectionCode: int(statusValue.GetUpstreamCode()), RejectionMessage: message,
			IntentID: intent, ProcessID: executorProcessID(params), ExecutionSessionID: e.sessionID,
			AfterSequence: afterSequence, Recorded: true, RemoteOutcome: Rejected,
			RequestOutcome: new(Rejected),
		}
		failure.Evidence = evidenceJSON
		return Operation[json.RawMessage]{Outcome: Rejected, IntentID: intent, ExecutionSessionID: e.sessionID, Evidence: evidenceJSON, Failure: failure}, nil
	}
	if statusValue.Outcome != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		message := statusValue.Message
		if message == "" {
			message = "remote execution outcome is unknown; operation was not replayed"
		}
		transport = &Failure{Kind: FailureTransport, Method: method, Phase: "native", IntentID: intent, ProcessID: executorProcessID(params), ExecutionSessionID: e.sessionID, Cause: errors.New(message)}
		evidence, finishErr := e.finishOperation(ctx, intent, transport.IntentEvidence())
		if finishErr != nil {
			return operation, fmt.Errorf("record unknown typed execution outcome after %v: %w", transport, errors.Join(frameJournalErr, finishErr))
		}
		transport.Evidence = evidence
		e.setReconnectRequired(true)
		if frameJournalErr != nil {
			return operation, frameJournalErr
		}
		return failureOperation[json.RawMessage](transport), nil
	}
	result, err := responseObject(method, response)
	if err != nil {
		finishErr := e.finishNested(ctx, intent, map[string]any{"outcome": Completed, "request_id": intent, "execution_session_id": e.sessionID, "result_decode_error": err.Error()})
		return operation, errors.Join(frameJournalErr, err, finishErr)
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		finishErr := e.finishNested(ctx, intent, map[string]any{"outcome": Completed, "request_id": intent, "execution_session_id": e.sessionID, "result_encoding_error": err.Error()})
		return operation, errors.Join(frameJournalErr, err, finishErr)
	}
	evidenceResult := make(map[string]any, len(result))
	for key, value := range result {
		evidenceResult[key] = value
	}
	if method == MethodFSReadBlock {
		if chunk, ok := evidenceResult["chunk"].(string); ok {
			digest := sha256.Sum256([]byte(chunk))
			evidenceResult["chunk"] = map[string]any{"base64_bytes": len(chunk), "base64_sha256": hex.EncodeToString(digest[:])}
		}
	}
	evidence := evidenceResult
	evidence["outcome"] = Completed
	evidence["request_id"] = intent
	evidence["execution_session_id"] = e.sessionID
	evidenceJSON, finishErr := e.finishOperation(ctx, intent, evidence)
	if finishErr != nil {
		return operation, errors.Join(frameJournalErr, fmt.Errorf("record completed typed execution %s result: %w", method, finishErr))
	}
	if frameJournalErr != nil {
		return operation, frameJournalErr
	}
	return Operation[json.RawMessage]{Outcome: Completed, IntentID: intent, ExecutionSessionID: e.sessionID, Evidence: evidenceJSON, Value: encoded}, nil
}

type executorReadAttemptTimeoutKey struct{}

func (e *Transport) callWithReadDeadline(ctx context.Context, method Method, request proto.Message) (proto.Message, error) {
	if method != MethodProcessRead {
		return e.call(ctx, method, request)
	}
	attemptTimeout := e.rpcTimeout
	if pageTimeout, ok := ctx.Value(executorReadAttemptTimeoutKey{}).(time.Duration); ok {
		if attemptTimeout <= 0 || pageTimeout < attemptTimeout {
			attemptTimeout = pageTimeout
		}
	}
	if attemptTimeout <= 0 {
		return e.call(ctx, method, request)
	}
	readCtx, cancel := context.WithTimeout(ctx, attemptTimeout)
	defer cancel()
	return e.call(readCtx, method, request)
}

func addTimeout(base, extra time.Duration) time.Duration {
	const maxDuration = time.Duration(1<<63 - 1)
	if extra > 0 && base > maxDuration-extra {
		return maxDuration
	}
	return base + extra
}

func (e *Transport) ReadBudget(normal time.Duration) time.Duration {
	if normal <= 0 {
		return normal
	}
	// Leave room for the original read, a retained-session reconnect, and
	// either a retried read or a fresh open after definitive native loss.
	return addTimeout(addTimeout(max(normal, e.rpcTimeout), e.reconnectTimeout), addTimeout(max(e.rpcTimeout, e.initializeTimeout), 5*time.Second))
}

func (e *Transport) newEnvironmentFailure(method Method, intentID, processID, executionSessionID string, afterSequence *uint64, output *outputbuffer.Buffer, cause error) *Failure {
	detail, ok := executionEnvironmentFailureDetail(cause)
	if !ok {
		return nil
	}
	return &Failure{
		Kind: FailureEnvironment, Method: method, IntentID: intentID, ProcessID: processID,
		ExecutionSessionID: executionSessionID, EnvironmentState: detail.GetState().String(),
		RemoteOutcome: NotDispatched, RequestOutcome: new(NotDispatched),
		AfterSequence: afterSequence, Output: output, TransportError: cause.Error(), Cause: cause,
	}
}

func executionEnvironmentFailureDetail(err error) (*executorpb.ExecutionEnvironmentFailure, bool) {
	st, ok := status.FromError(err)
	if !ok || st.Code() != codes.Unavailable && st.Code() != codes.FailedPrecondition {
		return nil, false
	}
	for _, detail := range st.Details() {
		failure, ok := detail.(*executorpb.ExecutionEnvironmentFailure)
		if !ok || failure.GetOutcome() != executorpb.ExecutionEnvironmentFailure_OUTCOME_NOT_DISPATCHED ||
			failure.GetState() == executorpb.ExecutionEnvironmentFailure_STATE_UNSPECIFIED {
			continue
		}
		switch failure.GetState() {
		case executorpb.ExecutionEnvironmentFailure_STATE_SUSPENDED,
			executorpb.ExecutionEnvironmentFailure_STATE_STARTING,
			executorpb.ExecutionEnvironmentFailure_STATE_NOT_READY,
			executorpb.ExecutionEnvironmentFailure_STATE_QUIESCING,
			executorpb.ExecutionEnvironmentFailure_STATE_CONTROL_ROUTE_UNAVAILABLE:
			return failure, true
		}
	}
	return nil, false
}

func isDefinitiveLostExecutorSession(rejected *Failure, expectedSession string) bool {
	if rejected == nil || !rejected.Recorded || rejected.Kind != FailureRejected || rejected.Method != MethodInitialize || rejected.RejectionCode != -32600 || expectedSession == "" {
		return false
	}
	// Codex exec-server's session registry reports an absent retained session as exactly
	// "unknown session id <id>" with JSON-RPC invalid_request (-32600). Keep this classifier
	// exact so unrelated rejections and transport outages never retire a live binding.
	return rejected.RejectionMessage == "unknown session id "+expectedSession
}

func executorProcessID(params any) string {
	switch v := params.(type) {
	case executorStart:
		return v.ProcessID
	case executorRead:
		return v.ProcessID
	case executorWrite:
		return v.ProcessID
	case executorSignal:
		return v.ProcessID
	case executorTerminate:
		return v.ProcessID
	}
	return ""
}

func (e *Transport) finishNested(ctx context.Context, intent string, evidence any) error {
	// Once an outcome is known, give its journal write a bounded window independent
	// of the RPC deadline. FinishNested still verifies the Session lease.
	journalCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	return e.journal.FinishExecutorIntent(journalCtx, intent, evidence)
}

func (e *Transport) finishOperation(ctx context.Context, intent string, evidence any) (json.RawMessage, error) {
	encoded, err := marshalEvidence(evidence)
	if err != nil {
		return nil, err
	}
	if err := e.finishNested(ctx, intent, evidence); err != nil {
		return nil, err
	}
	return encoded, nil
}

func (e *Transport) typedRequest(ctx context.Context, method Method, params any, id string) (proto.Message, error) {
	base := func() (*executorpb.ExecutionScope, string, string) {
		return executionScopeFromContext(ctx, e.ScopeSnapshot()), e.sessionID, id
	}
	scope, sid, rid := base()
	switch method {
	case MethodProcessStart:
		p := params.(executorStart)
		policy := p.EnvPolicy
		var env *executorpb.ExecutionEnvironmentPolicy
		if policy != nil {
			env = &executorpb.ExecutionEnvironmentPolicy{Inherit: policy.Inherit, IgnoreDefaultExcludes: policy.IgnoreDefaultExcludes, Exclude: policy.Exclude, Set: policy.Set, IncludeOnly: policy.IncludeOnly}
		}
		return &executorpb.StartProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID, Argv: p.Argv, Cwd: p.Cwd, EnvironmentPolicy: env, Environment: p.Env, Tty: p.TTY, PipeStdin: p.PipeStdin, Arg0: p.Arg0}, nil
	case MethodProcessRead:
		p := params.(executorRead)
		return &executorpb.ReadProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID, AfterSeq: p.AfterSeq, MaxBytes: uint32(p.MaxBytes), WaitMs: uint32(p.WaitMS)}, nil
	case MethodProcessWrite:
		p := params.(executorWrite)
		return &executorpb.WriteProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID, Data: p.Chunk, WriteId: p.WriteID}, nil
	case MethodProcessSignal:
		p := params.(executorSignal)
		return &executorpb.SignalProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID, Signal: p.Signal}, nil
	case MethodProcessTerminate:
		p := params.(executorTerminate)
		return &executorpb.TerminateProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID}, nil
	case MethodFSGetMetadata:
		p := params.(executorFSGetMetadata)
		return &executorpb.GetFileMetadataRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, Path: p.Path}, nil
	case MethodFSOpen:
		p := params.(executorFSOpen)
		return &executorpb.OpenFileRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, HandleId: p.HandleID, Path: p.Path}, nil
	case MethodFSReadBlock:
		p := params.(executorFSReadBlock)
		return &executorpb.ReadFileRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, HandleId: p.HandleID, Offset: p.Offset, Length: uint32(p.Len)}, nil
	case MethodFSClose:
		p := params.(executorFSClose)
		return &executorpb.CloseFileRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, HandleId: p.HandleID}, nil
	default:
		return nil, fmt.Errorf("unsupported typed execution method %q", method)
	}
}

func (e *Transport) call(ctx context.Context, method Method, request proto.Message) (proto.Message, error) {
	var response proto.Message
	var err error
	switch method {
	case MethodProcessStart:
		response, err = e.client.StartProcess(ctx, request.(*executorpb.StartProcessRequest))
	case MethodProcessRead:
		response, err = e.client.ReadProcess(ctx, request.(*executorpb.ReadProcessRequest))
	case MethodProcessWrite:
		response, err = e.client.WriteProcess(ctx, request.(*executorpb.WriteProcessRequest))
	case MethodProcessSignal:
		response, err = e.client.SignalProcess(ctx, request.(*executorpb.SignalProcessRequest))
	case MethodProcessTerminate:
		response, err = e.client.TerminateProcess(ctx, request.(*executorpb.TerminateProcessRequest))
	case MethodFSGetMetadata:
		response, err = e.client.GetFileMetadata(ctx, request.(*executorpb.GetFileMetadataRequest))
	case MethodFSOpen:
		response, err = e.client.OpenFile(ctx, request.(*executorpb.OpenFileRequest))
	case MethodFSReadBlock:
		response, err = e.client.ReadFile(ctx, request.(*executorpb.ReadFileRequest))
	case MethodFSClose:
		response, err = e.client.CloseFile(ctx, request.(*executorpb.CloseFileRequest))
	default:
		return nil, fmt.Errorf("unsupported typed execution method %q", method)
	}
	if err != nil {
		if environment := e.newEnvironmentFailure(method, "", "", "", nil, nil, err); environment != nil {
			return nil, environment
		}
		return nil, &Failure{Kind: FailureTransport, Method: method, Phase: "grpc", Cause: err}
	}
	if response == nil {
		return nil, &Failure{Kind: FailureTransport, Method: method, Phase: "response", Cause: errors.New("typed execution response is empty")}
	}
	return response, nil
}

func executionStatus(response proto.Message) *executorpb.ExecutionStatus {
	switch r := response.(type) {
	case *executorpb.StartProcessResponse:
		return r.Status
	case *executorpb.ReadProcessResponse:
		return r.Status
	case *executorpb.WriteProcessResponse:
		return r.Status
	case *executorpb.SignalProcessResponse:
		return r.Status
	case *executorpb.TerminateProcessResponse:
		return r.Status
	case *executorpb.GetFileMetadataResponse:
		return r.Status
	case *executorpb.OpenFileResponse:
		return r.Status
	case *executorpb.ReadFileResponse:
		return r.Status
	case *executorpb.CloseFileResponse:
		return r.Status
	}
	return nil
}

func executionFrames(response proto.Message) []*executorpb.ExecutionNativeFrame {
	switch r := response.(type) {
	case *executorpb.StartProcessResponse:
		return r.NativeFrames
	case *executorpb.ReadProcessResponse:
		return r.NativeFrames
	case *executorpb.WriteProcessResponse:
		return r.NativeFrames
	case *executorpb.SignalProcessResponse:
		return r.NativeFrames
	case *executorpb.TerminateProcessResponse:
		return r.NativeFrames
	case *executorpb.GetFileMetadataResponse:
		return r.NativeFrames
	case *executorpb.OpenFileResponse:
		return r.NativeFrames
	case *executorpb.ReadFileResponse:
		return r.NativeFrames
	case *executorpb.CloseFileResponse:
		return r.NativeFrames
	}
	return nil
}

func responseObject(method Method, response proto.Message) (map[string]any, error) {
	switch r := response.(type) {
	case *executorpb.StartProcessResponse:
		return jsonwire.Object(Started{ProcessID: r.ProcessId})
	case *executorpb.ReadProcessResponse:
		chunks := make([]Chunk, 0, len(r.Chunks))
		for _, c := range r.Chunks {
			chunks = append(chunks, Chunk{Seq: c.Seq, Stream: c.Stream, Chunk: append([]byte{}, c.Data...)})
		}
		var code *int
		if r.ExitCode != nil {
			x := int(*r.ExitCode)
			code = &x
		}
		return jsonwire.Object(ReadResult{Chunks: chunks, NextSeq: r.NextSeq, Exited: r.Exited, ExitCode: code, Closed: r.Closed, Failure: r.Failure})
	case *executorpb.WriteProcessResponse:
		return jsonwire.Object(WriteResult{Status: r.WriteStatus})
	case *executorpb.SignalProcessResponse:
		return map[string]any{}, nil
	case *executorpb.TerminateProcessResponse:
		return jsonwire.Object(Terminated{Running: r.Running})
	case *executorpb.GetFileMetadataResponse:
		return jsonwire.Object(Metadata{IsDirectory: r.IsDirectory, IsFile: r.IsFile, IsSymlink: r.IsSymlink, Size: r.Size})
	case *executorpb.OpenFileResponse:
		return jsonwire.Object(OpenedFile{HandleID: r.HandleId})
	case *executorpb.ReadFileResponse:
		return jsonwire.Object(Block{Chunk: append([]byte{}, r.Data...), EOF: r.Eof})
	case *executorpb.CloseFileResponse:
		return map[string]any{}, nil
	default:
		return nil, fmt.Errorf("unsupported typed execution response for %s", method)
	}
}
