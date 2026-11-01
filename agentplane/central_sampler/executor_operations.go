package centralsampler

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/execution/executorpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// The sampler works with semantic operation values. Native JSON frames are evidence only and
// are deliberately never decoded here.
type (
	executorEmpty   struct{}
	executorStarted struct {
		ProcessID string `json:"processId"`
	}
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
	ProcessID      string          `json:"processId"`
	AfterSeq       *uint64         `json:"afterSeq"`
	MaxBytes       int             `json:"maxBytes"`
	WaitMS         int             `json:"waitMs"`
	ObservedOutput *headTailBuffer `json:"-"`
}
type executorChunk struct {
	Seq    uint64 `json:"seq"`
	Stream string `json:"stream"`
	Chunk  []byte `json:"chunk"`
}
type executorReadResult struct {
	Chunks   []executorChunk `json:"chunks"`
	NextSeq  uint64          `json:"nextSeq"`
	Exited   bool            `json:"exited"`
	ExitCode *int            `json:"exitCode"`
	Closed   bool            `json:"closed"`
	Failure  *string         `json:"failure"`
}

func (r executorReadResult) output() []byte {
	var out []byte
	for _, chunk := range r.Chunks {
		out = append(out, chunk.Chunk...)
	}
	return out
}
func (r executorReadResult) uncertain() bool { return r.Failure != nil || r.Closed && !r.Exited }
func (r executorReadResult) drained() bool   { return r.Exited && r.Closed && len(r.Chunks) == 0 }

func describeProcessReadFailure(result *executorReadResult) string {
	if result.Failure != nil {
		return *result.Failure
	}
	return "output stream closed before an exit was observed"
}

type executorWrite struct {
	ProcessID string `json:"processId"`
	Chunk     []byte `json:"chunk"`
	WriteID   string `json:"writeId"`
}
type executorWriteResult struct {
	Status string `json:"status"`
}
type executorSignal struct {
	ProcessID string `json:"processId"`
	Signal    string `json:"signal"`
}
type executorTerminate struct {
	ProcessID string `json:"processId"`
}
type executorTerminateResult struct {
	Running bool `json:"running"`
}
type executorFSGetMetadata struct {
	Path string `json:"path"`
}
type executorFSMetadata struct {
	IsDirectory bool   `json:"isDirectory"`
	IsFile      bool   `json:"isFile"`
	IsSymlink   bool   `json:"isSymlink"`
	Size        uint64 `json:"size"`
}
type executorFSOpen struct {
	HandleID string `json:"handleId"`
	Path     string `json:"path"`
	Mode     string `json:"mode"`
}
type executorFSOpened struct {
	HandleID string `json:"handleId"`
}
type executorFSReadBlock struct {
	HandleID string `json:"handleId"`
	Offset   uint64 `json:"offset"`
	Len      int    `json:"len"`
}
type executorFSBlock struct {
	Chunk []byte `json:"chunk"`
	EOF   bool   `json:"eof"`
}
type executorFSClose struct {
	HandleID string `json:"handleId"`
}

type executorSessionStream interface {
	Recv() (*executorpb.OpenSessionResponse, error)
}
type executorClient interface {
	OpenSession(context.Context, *executorpb.OpenSessionRequest, ...grpc.CallOption) (executorSessionStream, error)
	StartProcess(context.Context, *executorpb.StartProcessRequest, ...grpc.CallOption) (*executorpb.StartProcessResponse, error)
	ReadProcess(context.Context, *executorpb.ReadProcessRequest, ...grpc.CallOption) (*executorpb.ReadProcessResponse, error)
	WriteProcess(context.Context, *executorpb.WriteProcessRequest, ...grpc.CallOption) (*executorpb.WriteProcessResponse, error)
	SignalProcess(context.Context, *executorpb.SignalProcessRequest, ...grpc.CallOption) (*executorpb.SignalProcessResponse, error)
	TerminateProcess(context.Context, *executorpb.TerminateProcessRequest, ...grpc.CallOption) (*executorpb.TerminateProcessResponse, error)
	GetFileMetadata(context.Context, *executorpb.GetFileMetadataRequest, ...grpc.CallOption) (*executorpb.GetFileMetadataResponse, error)
	OpenFile(context.Context, *executorpb.OpenFileRequest, ...grpc.CallOption) (*executorpb.OpenFileResponse, error)
	ReadFile(context.Context, *executorpb.ReadFileRequest, ...grpc.CallOption) (*executorpb.ReadFileResponse, error)
	CloseFile(context.Context, *executorpb.CloseFileRequest, ...grpc.CallOption) (*executorpb.CloseFileResponse, error)
}

// typedExecutorClient keeps the sampler's narrow stream interface injectable in tests while
// using the same generated Executor client for direct Runner and Gateway targets.
type typedExecutorClient struct{ executorpb.ExecutorClient }

func (c typedExecutorClient) OpenSession(ctx context.Context, req *executorpb.OpenSessionRequest, opts ...grpc.CallOption) (executorSessionStream, error) {
	return c.ExecutorClient.OpenSession(ctx, req, opts...)
}

// openSession follows a verified incarnation change only at the session-open boundary. Process
// mutations are never replayed; old handles are retired before opening a fresh native session.
func (e *Executor) openSession(ctx context.Context, resume *string) error {
	err := e.openSessionAtScope(ctx, resume)
	if err == nil {
		return nil
	}
	var fence *executorIncarnationFenceError
	if errors.As(err, &fence) {
		if !fence.recorded {
			return fence
		}
		if _, explicitlyFenced := expectedIncarnationUIDFromContext(ctx); explicitlyFenced {
			return fence
		}
		scope := e.scopeSnapshot()
		if err := e.store.RebindExecutorIncarnation(ctx, e.owner, scope.GetDestination().GetSandboxUid(), scope.GetExpectedIncarnationUid(), fence.currentIncarnationUID, ""); err != nil {
			return fmt.Errorf("record execution Sandbox incarnation change: %w", err)
		}
		scope.ExpectedIncarnationUid = fence.currentIncarnationUID
		e.setScope(scope)
		e.sessionID, e.shell = "", nil
		e.workspacePrepared = false
		e.lostSessionConfirmed = false
		// One fresh open, with no retained process or file handles and no recursive retries.
		if err := e.openSessionAtScope(ctx, nil); err != nil {
			return err
		}
		return &executorIncarnationReplacedError{}
	}
	return err
}

func (e *Executor) openSessionAtScope(ctx context.Context, resume *string) error {
	scope := executionScopeFromContext(ctx, e.scopeSnapshot())
	handshakeCtx, cancelHandshake := context.WithTimeout(ctx, e.initializeTimeout)
	defer cancelHandshake()
	intentRequest := map[string]any{"method": executorMethodInitialize, "resume_session_id": resume}
	if scope.GetDestination().GetSandboxUid() != "" {
		intentRequest["execution_scope"] = map[string]any{
			"sandbox_uid": scope.GetDestination().GetSandboxUid(), "expected_incarnation_uid": scope.GetExpectedIncarnationUid(),
		}
	}
	if rebind, ok := incarnationRebindFromContext(ctx); ok {
		intentRequest["incarnation_rebind_from"] = rebind.previousUID
	}
	intent, err := e.store.RecordNested(ctx, e.owner, IntentExecutor, intentRequest)
	if err != nil {
		return err
	}
	streamCtx, cancel := context.WithCancel(e.lifetime)
	stream, err := e.client.OpenSession(streamCtx, &executorpb.OpenSessionRequest{Scope: scope, RequestId: intent, ResumeSessionId: resume})
	if err != nil {
		cancel()
		if environmentFailure := e.newEnvironmentFailure(executorMethodInitialize, intent, "", "", nil, nil, err); environmentFailure != nil {
			if finishErr := e.store.FinishNested(ctx, e.owner, intent, environmentFailure.intentEvidence()); finishErr != nil {
				return fmt.Errorf("record known OpenSession refusal after %v: %w", environmentFailure, finishErr)
			}
			return environmentFailure
		}
		if fence := e.incarnationFenceError(err, scope, executorMethodInitialize, intent, "", "", nil, nil, NestedNotDispatched); fence != nil {
			if finishErr := e.store.FinishNested(ctx, e.owner, intent, fence.intentEvidence()); finishErr != nil {
				return errors.Join(fence, fmt.Errorf("record stale OpenSession fence: %w", finishErr))
			}
			fence.recorded = true
			return fence
		}
		transport := &executorTransportError{method: executorMethodInitialize, phase: "open", intentID: intent, retainedSession: e.retainedSessionID(ctx), cause: err}
		if finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(transport.retainedSession)); finishErr != nil {
			return fmt.Errorf("record OpenSession transport outcome after %v: %w", transport, finishErr)
		}
		return transport
	}
	first := make(chan struct {
		response *executorpb.OpenSessionResponse
		err      error
	}, 1)
	go func() {
		response, recvErr := stream.Recv()
		first <- struct {
			response *executorpb.OpenSessionResponse
			err      error
		}{response, recvErr}
	}()
	var opened *executorpb.OpenSessionResponse
	select {
	case result := <-first:
		opened, err = result.response, result.err
	case <-handshakeCtx.Done():
		cancel()
		select {
		case result := <-first:
			opened, err = result.response, result.err
		default:
			err = handshakeCtx.Err()
		}
	}
	if err != nil {
		cancel()
		if environmentFailure := e.newEnvironmentFailure(executorMethodInitialize, intent, "", "", nil, nil, err); environmentFailure != nil {
			if finishErr := e.store.FinishNested(ctx, e.owner, intent, environmentFailure.intentEvidence()); finishErr != nil {
				return fmt.Errorf("record known OpenSession refusal after %v: %w", environmentFailure, finishErr)
			}
			return environmentFailure
		}
		if fence := e.incarnationFenceError(err, scope, executorMethodInitialize, intent, "", "", nil, nil, NestedNotDispatched); fence != nil {
			if finishErr := e.store.FinishNested(ctx, e.owner, intent, fence.intentEvidence()); finishErr != nil {
				return errors.Join(fence, fmt.Errorf("record stale OpenSession fence: %w", finishErr))
			}
			fence.recorded = true
			return fence
		}
		transport := &executorTransportError{method: executorMethodInitialize, phase: "receive", intentID: intent, retainedSession: e.retainedSessionID(ctx), cause: err}
		if finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(transport.retainedSession)); finishErr != nil {
			return fmt.Errorf("record OpenSession receive outcome after %v: %w", transport, finishErr)
		}
		return transport
	}
	if opened == nil {
		cancel()
		transport := &executorTransportError{method: executorMethodInitialize, phase: "response", intentID: intent, retainedSession: e.retainedSessionID(ctx), cause: errors.New("executor OpenSession returned an empty response")}
		if err := e.finishUnknownIntent(ctx, intent, transport.evidence(transport.retainedSession)); err != nil {
			return fmt.Errorf("record empty OpenSession response: %w", err)
		}
		return transport
	}
	frameErr := e.recordNativeFrames(ctx, intent, opened.ExecutionSessionId, executorMethodInitialize, opened.NativeFrames)
	if opened.Status == nil || opened.Status.Outcome != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		cancel()
		return e.finishOpenFailure(ctx, intent, opened, frameErr)
	}
	if opened.ExecutionSessionId == "" || resume != nil && opened.ExecutionSessionId != *resume {
		cancel()
		transport := &executorTransportError{method: executorMethodInitialize, phase: "response", intentID: intent, retainedSession: e.retainedSessionID(ctx), cause: errors.New("executor resumed a different or empty execution session")}
		finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(transport.retainedSession))
		if frameErr != nil || finishErr != nil {
			return fmt.Errorf("record mismatched OpenSession response: %w", errors.Join(frameErr, finishErr))
		}
		return transport
	}
	var shellName *string
	var shell *execShell
	if opened.Shell != nil {
		shell = &execShell{Name: opened.Shell.Name, Path: opened.Shell.Path}
		shellName = &opened.Shell.Name
	}
	var environmentInfo any
	if opened.Shell != nil {
		environmentInfo = map[string]any{"shell": map[string]any{"name": opened.Shell.Name, "path": opened.Shell.Path}}
	}
	if err := e.store.FinishNested(ctx, e.owner, intent, map[string]any{
		"outcome": NestedCompleted, "request_id": intent, "execution_session_id": opened.ExecutionSessionId,
		"sessionId": opened.ExecutionSessionId, "environmentInfo": environmentInfo,
	}); err != nil {
		cancel()
		return errors.Join(frameErr, err)
	}
	if frameErr != nil {
		cancel()
		return fmt.Errorf("record OpenSession native evidence: %w", frameErr)
	}
	if rebind, ok := incarnationRebindFromContext(ctx); ok {
		e.setReconnectRequired(true)
		if err := e.stopCurrentSessionStream(ctx); err != nil {
			cancel()
			return fmt.Errorf("stop old execution session after verified incarnation change: %w", err)
		}
		if err := e.store.RebindExecutorIncarnationWithSession(ctx, e.owner,
			scope.GetDestination().GetSandboxUid(), rebind.previousUID, scope.GetExpectedIncarnationUid(),
			opened.ExecutionSessionId, shellName); err != nil {
			cancel()
			return fmt.Errorf("record verified execution Sandbox incarnation change: %w", err)
		}
		e.setScope(scope)
		e.workspacePrepared = false
		e.lostSessionConfirmed = false
	} else if err := e.store.SetExecutorSession(ctx, e.owner, opened.ExecutionSessionId, shellName); err != nil {
		cancel()
		return err
	}
	done := make(chan struct{})
	e.sessionID, e.sessionStream, e.sessionCancel, e.sessionDone, e.shell = opened.ExecutionSessionId, stream, cancel, done, shell
	e.sessionStateMu.Lock()
	e.sessionGeneration++
	generation := e.sessionGeneration
	e.reconnectRequired = false
	if resume == nil && e.lostSessionConfirmed {
		e.lostSessionConfirmed = false
	}
	e.sessionStateMu.Unlock()
	go e.watchSession(streamCtx, stream, intent, opened.ExecutionSessionId, generation, cancel, done)
	return nil
}

func (e *Executor) finishOpenFailure(ctx context.Context, intent string, response *executorpb.OpenSessionResponse, nativeEvidenceErr error) error {
	if response.Status != nil && response.Status.Outcome == executorpb.ExecutionStatus_OUTCOME_REJECTED {
		message := response.Status.Message
		if message == "" {
			message = "session open rejected"
		}
		rejectedError := &executorRejectedError{method: executorMethodInitialize, code: int(response.Status.GetUpstreamCode()), message: message}
		rejected := map[string]any{"code": response.Status.UpstreamCode, "message": message}
		if err := e.store.FinishNested(ctx, e.owner, intent, map[string]any{"outcome": NestedRejected, "request_id": intent, "rejected": rejected}); err != nil {
			return fmt.Errorf("record rejected OpenSession result: %w", errors.Join(nativeEvidenceErr, err))
		}
		// A typed rejection alone is insufficient if accompanying native frames failed to
		// persist. Session-loss recovery requires the complete response evidence boundary.
		if nativeEvidenceErr != nil {
			return fmt.Errorf("record rejected OpenSession native evidence: %w", nativeEvidenceErr)
		}
		rejectedError.recorded = true
		return rejectedError
	}
	message := "execution session open outcome is unknown"
	if response.Status != nil && response.Status.Message != "" {
		message = response.Status.Message
	}
	if err := e.store.FinishNested(ctx, e.owner, intent, map[string]any{"outcome": NestedUnknown, "request_id": intent, "message": message}); err != nil {
		return errors.Join(nativeEvidenceErr, errors.New(message), fmt.Errorf("record unknown OpenSession outcome: %w", err))
	}
	if nativeEvidenceErr != nil {
		return fmt.Errorf("record unknown OpenSession native evidence: %w", nativeEvidenceErr)
	}
	return &executorTransportError{method: executorMethodInitialize, phase: "response", intentID: intent, retainedSession: e.retainedSessionID(ctx), cause: errors.New(message)}
}

func (e *Executor) watchSession(streamCtx context.Context, stream executorSessionStream, operationID, sessionID string, generation uint64, cancel context.CancelFunc, done chan struct{}) {
	defer close(done)
	for {
		response, err := stream.Recv()
		if err != nil {
			e.sessionStateMu.Lock()
			if !e.closing && e.sessionGeneration == generation {
				e.reconnectRequired = true
			}
			e.sessionStateMu.Unlock()
			return
		}
		// Reconnect cancels streamCtx while this watcher may already be recording a
		// received notification. Keep that evidence write alive for a bounded time.
		journalCtx, cancelJournal := context.WithTimeout(context.WithoutCancel(streamCtx), 5*time.Second)
		err = e.recordNativeFrames(journalCtx, operationID, sessionID, executorMethodSessionNotice, response.NativeFrames)
		cancelJournal()
		if err != nil {
			e.sessionStateMu.Lock()
			if e.sessionGeneration == generation {
				e.sessionError = fmt.Errorf("record execution session notification evidence: %w", err)
				e.reconnectRequired = true
			}
			e.sessionStateMu.Unlock()
			cancel()
			return
		}
		if response.Status != nil {
			e.sessionStateMu.Lock()
			if !e.closing && e.sessionGeneration == generation {
				e.reconnectRequired = true
			}
			e.sessionStateMu.Unlock()
			cancel()
			return
		}
		if response.ExecutionSessionId != "" && response.ExecutionSessionId != sessionID {
			e.sessionStateMu.Lock()
			if e.sessionGeneration == generation {
				e.reconnectRequired = true
			}
			e.sessionStateMu.Unlock()
			cancel()
			return
		}
	}
}

func (e *Executor) recordNativeFrames(ctx context.Context, operationID, sessionID string, method executionMethod, frames []*executorpb.ExecutionNativeFrame) error {
	if ctx.Err() != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
	}
	for _, frame := range frames {
		if frame == nil {
			continue
		}
		payload := map[string]any{"request_id": operationID, "execution_session_id": sessionID, "payload": frame.Payload}
		if method == executorMethodFSReadBlock {
			digest := sha256.Sum256(frame.Payload)
			payload["payload_digest_sha256"] = hex.EncodeToString(digest[:])
			payload["payload_bytes"] = len(frame.Payload)
			delete(payload, "payload")
		}
		if err := e.store.ProtocolEvent(ctx, e.owner, operationID, "executor", frame.Direction, payload); err != nil {
			return err
		}
	}
	return nil
}

// ensureSessionLocked is called with rpcMu held before a typed operation is admitted. A
// connection failure rejects that operation without poisoning the JavaScript session. Evidence
// and ownership failures remain operational errors and must never masquerade as remote failures.
func (e *Executor) ensureSessionLocked(ctx context.Context, method executionMethod, processID string) error {
	e.sessionStateMu.Lock()
	sessionError, reconnectRequired := e.sessionError, e.reconnectRequired
	e.sessionStateMu.Unlock()
	if sessionError != nil {
		return fmt.Errorf("execution session evidence journal failed; refusing further dispatch: %w", sessionError)
	}
	if expectedUID, explicit := expectedIncarnationUIDFromContext(ctx); explicit {
		currentScope := e.scopeSnapshot()
		if expectedUID != currentScope.GetExpectedIncarnationUid() {
			if currentScope.GetExpectedIncarnationUid() == "" || currentScope.GetDestination().GetSandboxUid() == "" {
				return &toolError{"cannot fence execution because the retained scope has no incarnation or Sandbox UID"}
			}
			candidateScope := proto.Clone(currentScope).(*executorpb.ExecutionScope)
			candidateScope.ExpectedIncarnationUid = expectedUID
			candidateCtx := withExecutionScope(ctx, candidateScope)
			candidateCtx = withIncarnationRebind(candidateCtx, currentScope.GetExpectedIncarnationUid())
			if err := e.openSessionAtScope(candidateCtx, nil); err != nil {
				return err
			}
			return nil
		}
	}
	var err error
	if e.sessionID == "" {
		err = e.openSession(ctx, nil)
	} else if reconnectRequired {
		err = e.reconnectLocked(ctx, processID)
	}
	// Workspace preparation has no native handle or previously dispatched mutation. It can
	// continue immediately after the durable incarnation notice and fresh session open.
	var changed *executorIncarnationReplacedError
	if errors.As(err, &changed) && method == executorMethodProcessStart && processID == "" {
		return nil
	}
	if err == nil || !isExecutorConnectionFailure(err) {
		return err
	}
	return &executorNotDispatchedError{method: method, processID: processID, retainedSession: e.retainedSessionID(ctx), cause: err}
}

func isExecutorConnectionFailure(err error) bool {
	var transport *executorTransportError
	var environment *executorEnvironmentFailureError
	var rejected *executorRejectedError
	var replaced *executorSessionReplacedError
	var changed *executorIncarnationReplacedError
	return errors.As(err, &changed) || errors.As(err, &transport) || errors.As(err, &environment) || errors.As(err, &rejected) || errors.As(err, &replaced)
}

func (e *Executor) rpc(ctx context.Context, method executionMethod, params any, resultEvidence func(map[string]any) map[string]any) (json.RawMessage, error) {
	e.rpcMu.Lock()
	defer e.rpcMu.Unlock()
	if err := e.ensureSessionLocked(ctx, method, executorProcessID(params)); err != nil {
		return nil, err
	}
	if e.rpcTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, e.rpcTimeout)
		defer cancel()
	}
	requestJSON, err := protocolObject(map[string]any{"method": method, "params": params})
	if err != nil {
		return nil, err
	}
	intent, err := e.store.RecordNested(ctx, e.owner, IntentExecutor, requestJSON)
	if err != nil {
		return nil, err
	}
	request, err := e.typedRequest(ctx, method, params, intent)
	if err != nil {
		finishErr := e.store.FinishNested(ctx, e.owner, intent, map[string]any{"outcome": NestedNotDispatched, "request_id": intent, "method": method, "reason": err.Error()})
		return nil, errors.Join(err, finishErr)
	}
	response, err := e.call(ctx, method, request)
	readRetried := false
	if err != nil {
		remoteOutcome := NestedNotDispatched
		if method == executorMethodProcessRead {
			// The read did not reach the current execution incarnation, but the process
			// may still be running from an earlier successful StartProcess.
			remoteOutcome = NestedUnknown
		}
		scope := executionScopeFromContext(ctx, e.scopeSnapshot())
		if fence := e.incarnationFenceError(err, scope, method, intent, executorProcessID(params), e.sessionID, nil, nil, remoteOutcome); fence != nil {
			if read, ok := params.(executorRead); ok && method == executorMethodProcessRead {
				fence.afterSequence, fence.output = read.AfterSeq, read.ObservedOutput
			}
			e.setReconnectRequired(true)
			if finishErr := e.finishUnknownIntent(ctx, intent, fence.intentEvidence()); finishErr != nil {
				return nil, fmt.Errorf("record stale execution incarnation for %s after %v: %w", method, fence, finishErr)
			}
			fence.recorded = true
			return nil, fence
		}
	}
	var transport *executorTransportError
	if err != nil && errors.As(err, &transport) {
		e.setReconnectRequired(true)
		if method == executorMethodProcessRead && ctx.Err() == nil {
			if reconnectErr := e.reconnectLocked(ctx, executorProcessID(params)); reconnectErr == nil {
				readRetried = true
				response, err = e.call(ctx, method, request)
			} else if !isExecutorConnectionFailure(reconnectErr) {
				finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(e.sessionID))
				return nil, fmt.Errorf("resume executor state after %v: %w", transport, errors.Join(reconnectErr, finishErr))
			} else {
				transport.cause = errors.Join(transport.cause, fmt.Errorf("resume executor session: %w", reconnectErr))
			}
		}
	}
	if err != nil {
		var environment *executorEnvironmentFailureError
		if errors.As(err, &environment) {
			environment.intentID = intent
			environment.processID = executorProcessID(params)
			environment.executionSessionID = e.sessionID
			if read, ok := params.(executorRead); ok {
				environment.afterSequence = read.AfterSeq
				environment.output = read.ObservedOutput
				// A failed read cannot establish whether the already-started process exited.
				environment.remoteOutcome = NestedUnknown
			}
			e.setReconnectRequired(true)
			if finishErr := e.store.FinishNested(ctx, e.owner, intent, environment.intentEvidence()); finishErr != nil {
				return nil, fmt.Errorf("record known typed execution %s refusal after %v: %w", method, environment, finishErr)
			}
			return nil, environment
		}
		if errors.As(err, &transport) {
			transport.intentID, transport.processID = intent, executorProcessID(params)
			transport.retainedSession = e.retainedSessionID(ctx)
			e.setReconnectRequired(true)
			if finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(transport.retainedSession)); finishErr != nil {
				return nil, fmt.Errorf("record unknown typed execution %s outcome after %v: %w", method, transport, finishErr)
			}
		}
		return nil, err
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
		if method == executorMethodProcessRead && !readRetried && ctx.Err() == nil && frameErr == nil {
			e.setReconnectRequired(true)
			if reconnectErr := e.reconnectLocked(ctx, executorProcessID(params)); reconnectErr == nil {
				readRetried = true
				response, err = e.call(ctx, method, request)
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
				transport := &executorTransportError{method: method, phase: "native", intentID: intent, processID: executorProcessID(params), retainedSession: e.sessionID, cause: errors.New(statusValue.Message)}
				finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(e.sessionID))
				return nil, fmt.Errorf("resume executor state after %v: %w", transport, errors.Join(reconnectErr, finishErr))
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
		finishErr := e.store.FinishNested(ctx, e.owner, intent, map[string]any{"outcome": NestedRejected, "request_id": intent, "execution_session_id": e.sessionID, "rejected": rejected})
		if frameJournalErr != nil {
			return nil, errors.Join(frameJournalErr, finishErr)
		}
		if finishErr != nil {
			return nil, fmt.Errorf("record rejected typed execution %s result: %w", method, finishErr)
		}
		var afterSequence *uint64
		if read, ok := params.(executorRead); ok {
			afterSequence = read.AfterSeq
		}
		return nil, &executorRejectedError{
			method: method, code: int(statusValue.GetUpstreamCode()), message: message,
			intentID: intent, processID: executorProcessID(params), executionSessionID: e.sessionID,
			afterSequence: afterSequence, recorded: true,
		}
	}
	if statusValue.Outcome != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		message := statusValue.Message
		if message == "" {
			message = "remote execution outcome is unknown; operation was not replayed"
		}
		transport = &executorTransportError{method: method, phase: "native", intentID: intent, processID: executorProcessID(params), retainedSession: e.sessionID, cause: errors.New(message)}
		if finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(e.sessionID)); finishErr != nil {
			return nil, fmt.Errorf("record unknown typed execution outcome after %v: %w", transport, errors.Join(frameJournalErr, finishErr))
		}
		e.setReconnectRequired(true)
		if frameJournalErr != nil {
			return nil, frameJournalErr
		}
		return nil, transport
	}
	result, err := responseObject(method, response)
	if err != nil {
		finishErr := e.store.FinishNested(ctx, e.owner, intent, map[string]any{"outcome": NestedCompleted, "request_id": intent, "execution_session_id": e.sessionID, "result_decode_error": err.Error()})
		return nil, errors.Join(frameJournalErr, err, finishErr)
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		finishErr := e.store.FinishNested(ctx, e.owner, intent, map[string]any{"outcome": NestedCompleted, "request_id": intent, "execution_session_id": e.sessionID, "result_encoding_error": err.Error()})
		return nil, errors.Join(frameJournalErr, err, finishErr)
	}
	evidenceResult := make(map[string]any, len(result))
	for key, value := range result {
		evidenceResult[key] = value
	}
	if resultEvidence != nil {
		evidenceResult = resultEvidence(result)
	}
	evidence := evidenceResult
	evidence["outcome"] = NestedCompleted
	evidence["request_id"] = intent
	evidence["execution_session_id"] = e.sessionID
	if err := e.store.FinishNested(ctx, e.owner, intent, evidence); err != nil {
		return nil, errors.Join(frameJournalErr, fmt.Errorf("record completed typed execution %s result: %w", method, err))
	}
	if frameJournalErr != nil {
		return nil, frameJournalErr
	}
	return encoded, nil
}

func (e *Executor) reconnectLocked(ctx context.Context, notDispatchedProcessID string) error {
	e.setReconnectRequired(true)
	conversation, err := e.store.Read(ctx, e.owner)
	if err != nil {
		return err
	}
	previousSession := e.sessionID
	if e.sessionCancel != nil {
		e.sessionCancel()
		e.sessionCancel = nil
		if e.sessionDone != nil {
			select {
			case <-e.sessionDone:
			case <-ctx.Done():
				return fmt.Errorf("wait for previous execution session stream to end: %w", ctx.Err())
			}
			e.sessionDone = nil
		}
	}
	if conversation.ExecutorSession == nil {
		if !e.lostSessionConfirmed {
			return errors.New("cannot open a fresh executor session without definitive loss of the retained session")
		}
		if err := e.openSession(ctx, nil); err != nil {
			return err
		}
		return &executorSessionReplacedError{previousSession: previousSession}
	}
	retainedSession := *conversation.ExecutorSession
	if err := e.openSession(ctx, &retainedSession); err != nil {
		var rejected *executorRejectedError
		if !errors.As(err, &rejected) || !isDefinitiveLostExecutorSession(rejected, retainedSession) {
			return err
		}
		if retireErr := e.store.RetireLostExecutorSession(ctx, e.owner, retainedSession, notDispatchedProcessID); retireErr != nil {
			return fmt.Errorf("retire lost executor session after %v: %w", err, retireErr)
		}
		e.lostSessionConfirmed = true
		e.shell = nil
		if freshErr := e.openSession(ctx, nil); freshErr != nil {
			return fmt.Errorf("open fresh executor session after definitive session loss (%v): %w", err, freshErr)
		}
		return &executorSessionReplacedError{previousSession: retainedSession}
	}
	return nil
}

func (e *Executor) close() error {
	e.rpcMu.Lock()
	defer e.rpcMu.Unlock()
	e.sessionStateMu.Lock()
	e.closing = true
	e.sessionStateMu.Unlock()
	var streamErr error
	if e.sessionCancel != nil {
		e.sessionCancel()
		e.sessionCancel = nil
		if e.sessionDone != nil {
			select {
			case <-e.sessionDone:
			case <-time.After(5 * time.Second):
				streamErr = errors.New("timed out waiting for execution session stream to close")
			}
			e.sessionDone = nil
		}
	}
	if e.connection != nil {
		return errors.Join(streamErr, e.connection.Close())
	}
	return streamErr
}

func (e *Executor) setReconnectRequired(value bool) {
	e.sessionStateMu.Lock()
	e.reconnectRequired = value
	e.sessionStateMu.Unlock()
}

func (e *Executor) setSessionError(err error) {
	e.sessionStateMu.Lock()
	if e.sessionError == nil {
		e.sessionError = err
	}
	e.sessionStateMu.Unlock()
}

type executorTransportError struct {
	method                                      executionMethod
	phase, intentID, processID, retainedSession string
	afterSequence                               *uint64
	output                                      *headTailBuffer
	cause                                       error
}

type executorEnvironmentFailureError struct {
	method             executionMethod
	intentID           string
	processID          string
	executionSessionID string
	state              executorpb.ExecutionEnvironmentFailure_State
	remoteOutcome      NestedOutcome
	afterSequence      *uint64
	output             *headTailBuffer
	cause              error
}

func (e *executorEnvironmentFailureError) Error() string {
	message := fmt.Sprintf("execution environment is %s; typed execution %s was not admitted", e.state.String(), e.method)
	if e.remoteOutcome == NestedUnknown {
		return message + "; the existing process outcome remains unknown and was not replayed"
	}
	return message + "; the request was not dispatched and was not retried"
}

func (e *executorEnvironmentFailureError) Unwrap() error { return e.cause }

func (e *executorEnvironmentFailureError) executionFailureRecord() executionFailureRecord {
	requestOutcome := NestedNotDispatched
	return executionFailureRecord{
		message: e.Error(), method: e.method, intentID: e.intentID, processID: e.processID,
		executionSessionID: e.executionSessionID, afterSequence: e.afterSequence,
		remoteOutcome: e.remoteOutcome, requestOutcome: &requestOutcome, automaticReplay: false,
		environmentState: &e.state, transportError: e.cause.Error(), output: e.output,
	}
}

func (e *executorEnvironmentFailureError) intentEvidence() executionFailureEvidence {
	evidence := e.executionFailureRecord().intentEvidence()
	// This RPC was refused before dispatch even when the existing process remains unknown.
	evidence.RemoteOutcome = NestedNotDispatched
	return evidence
}

func (e *Executor) newEnvironmentFailure(method executionMethod, intentID, processID, executionSessionID string, afterSequence *uint64, output *headTailBuffer, cause error) *executorEnvironmentFailureError {
	detail, ok := executionEnvironmentFailureDetail(cause)
	if !ok {
		return nil
	}
	return &executorEnvironmentFailureError{
		method: method, intentID: intentID, processID: processID, executionSessionID: executionSessionID,
		state: detail.GetState(), remoteOutcome: NestedNotDispatched, afterSequence: afterSequence, output: output, cause: cause,
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

func (e *executorTransportError) Error() string {
	operation := processDescription(e.processID)
	if e.intentID != "" {
		operation = " intent " + e.intentID + operation
	}
	return fmt.Sprintf("typed execution %s %s failed%s: remote effect outcome is unknown; mutation was not replayed: %v", e.method, e.phase, operation, e.cause)
}
func (e *executorTransportError) Unwrap() error { return e.cause }
func (e *executorTransportError) evidence(retainedSession string) executionFailureEvidence {
	return (executionFailureRecord{
		message: e.Error(), method: e.method, phase: e.phase, intentID: e.intentID,
		processID: e.processID, executionSessionID: retainedSession, afterSequence: e.afterSequence,
		remoteOutcome: NestedUnknown, automaticReplay: false, transportError: e.cause.Error(), output: e.output,
	}).intentEvidence()
}

type executorNotDispatchedError struct {
	method                     executionMethod
	processID, retainedSession string
	afterSequence              *uint64
	output                     *headTailBuffer
	cause                      error
}

func (e *executorNotDispatchedError) Error() string {
	return fmt.Sprintf("typed execution %s was not dispatched%s because the executor connection could not be established: %v; previous remote outcomes remain unknown and no mutation was replayed", e.method, processDescription(e.processID), e.cause)
}
func (e *executorNotDispatchedError) Unwrap() error { return e.cause }

type executorRejectedError struct {
	method             executionMethod
	code               int
	message            string
	intentID           string
	processID          string
	executionSessionID string
	afterSequence      *uint64
	output             *headTailBuffer
	recorded           bool // The semantic result and all native response frames are durably recorded.
}

func (e *executorRejectedError) Error() string {
	return fmt.Sprintf("exec-server rejected %s (%d): %s", e.method, e.code, e.message)
}

type executorIncarnationReplacedError struct{}

func (*executorIncarnationReplacedError) Error() string {
	return "execution Sandbox changed incarnation; old execution handles were abandoned and a fresh session was opened. The requested operation was not dispatched"
}

type executorSessionReplacedError struct {
	previousSession string
}

func (e *executorSessionReplacedError) Error() string {
	return fmt.Sprintf("retained exec-server session %q was definitively lost; its process handles were abandoned and a fresh session was opened. The requested operation was not dispatched and must not be retried automatically", e.previousSession)
}

func isDefinitiveLostExecutorSession(rejected *executorRejectedError, expectedSession string) bool {
	if rejected == nil || !rejected.recorded || rejected.method != executorMethodInitialize || rejected.code != -32600 || expectedSession == "" {
		return false
	}
	// Codex exec-server's session registry reports an absent retained session as exactly
	// "unknown session id <id>" with JSON-RPC invalid_request (-32600). Keep this classifier
	// exact so unrelated rejections and transport outages never retire a live binding.
	return rejected.message == "unknown session id "+expectedSession
}

func processDescription(id string) string {
	if id == "" {
		return ""
	}
	return " for process " + id
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

func (e *Executor) retainedSessionID(ctx context.Context) string {
	if ctx.Err() != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
	}
	c, err := e.store.Read(ctx, e.owner)
	if err != nil || c.ExecutorSession == nil {
		return ""
	}
	return *c.ExecutorSession
}

func (e *Executor) finishUnknownIntent(ctx context.Context, intent string, evidence any) error {
	if ctx.Err() != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
	}
	return e.store.FinishNested(ctx, e.owner, intent, evidence)
}

func (e *Executor) typedRequest(ctx context.Context, method executionMethod, params any, id string) (proto.Message, error) {
	base := func() (*executorpb.ExecutionScope, string, string) {
		return executionScopeFromContext(ctx, e.scopeSnapshot()), e.sessionID, id
	}
	scope, sid, rid := base()
	switch method {
	case executorMethodProcessStart:
		p := params.(executorStart)
		policy := p.EnvPolicy
		var env *executorpb.ExecutionEnvironmentPolicy
		if policy != nil {
			env = &executorpb.ExecutionEnvironmentPolicy{Inherit: policy.Inherit, IgnoreDefaultExcludes: policy.IgnoreDefaultExcludes, Exclude: policy.Exclude, Set: policy.Set, IncludeOnly: policy.IncludeOnly}
		}
		return &executorpb.StartProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID, Argv: p.Argv, Cwd: p.Cwd, EnvironmentPolicy: env, Environment: p.Env, Tty: p.TTY, PipeStdin: p.PipeStdin, Arg0: p.Arg0}, nil
	case executorMethodProcessRead:
		p := params.(executorRead)
		return &executorpb.ReadProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID, AfterSeq: p.AfterSeq, MaxBytes: uint32(p.MaxBytes), WaitMs: uint32(p.WaitMS)}, nil
	case executorMethodProcessWrite:
		p := params.(executorWrite)
		return &executorpb.WriteProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID, Data: p.Chunk, WriteId: p.WriteID}, nil
	case executorMethodProcessSignal:
		p := params.(executorSignal)
		return &executorpb.SignalProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID, Signal: p.Signal}, nil
	case executorMethodProcessTerminate:
		p := params.(executorTerminate)
		return &executorpb.TerminateProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID}, nil
	case executorMethodFSGetMetadata:
		p := params.(executorFSGetMetadata)
		return &executorpb.GetFileMetadataRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, Path: p.Path}, nil
	case executorMethodFSOpen:
		p := params.(executorFSOpen)
		return &executorpb.OpenFileRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, HandleId: p.HandleID, Path: p.Path}, nil
	case executorMethodFSReadBlock:
		p := params.(executorFSReadBlock)
		return &executorpb.ReadFileRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, HandleId: p.HandleID, Offset: p.Offset, Length: uint32(p.Len)}, nil
	case executorMethodFSClose:
		p := params.(executorFSClose)
		return &executorpb.CloseFileRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, HandleId: p.HandleID}, nil
	default:
		return nil, fmt.Errorf("unsupported typed execution method %q", method)
	}
}

func (e *Executor) call(ctx context.Context, method executionMethod, request proto.Message) (proto.Message, error) {
	var response proto.Message
	var err error
	switch method {
	case executorMethodProcessStart:
		response, err = e.client.StartProcess(ctx, request.(*executorpb.StartProcessRequest))
	case executorMethodProcessRead:
		response, err = e.client.ReadProcess(ctx, request.(*executorpb.ReadProcessRequest))
	case executorMethodProcessWrite:
		response, err = e.client.WriteProcess(ctx, request.(*executorpb.WriteProcessRequest))
	case executorMethodProcessSignal:
		response, err = e.client.SignalProcess(ctx, request.(*executorpb.SignalProcessRequest))
	case executorMethodProcessTerminate:
		response, err = e.client.TerminateProcess(ctx, request.(*executorpb.TerminateProcessRequest))
	case executorMethodFSGetMetadata:
		response, err = e.client.GetFileMetadata(ctx, request.(*executorpb.GetFileMetadataRequest))
	case executorMethodFSOpen:
		response, err = e.client.OpenFile(ctx, request.(*executorpb.OpenFileRequest))
	case executorMethodFSReadBlock:
		response, err = e.client.ReadFile(ctx, request.(*executorpb.ReadFileRequest))
	case executorMethodFSClose:
		response, err = e.client.CloseFile(ctx, request.(*executorpb.CloseFileRequest))
	default:
		return nil, fmt.Errorf("unsupported typed execution method %q", method)
	}
	if err != nil {
		if environment := e.newEnvironmentFailure(method, "", "", "", nil, nil, err); environment != nil {
			return nil, environment
		}
		return nil, &executorTransportError{method: method, phase: "grpc", cause: err}
	}
	if response == nil {
		return nil, &executorTransportError{method: method, phase: "response", cause: errors.New("typed execution response is empty")}
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

func responseObject(method executionMethod, response proto.Message) (map[string]any, error) {
	switch r := response.(type) {
	case *executorpb.StartProcessResponse:
		return map[string]any{"processId": r.ProcessId}, nil
	case *executorpb.ReadProcessResponse:
		chunks := make([]executorChunk, 0, len(r.Chunks))
		for _, c := range r.Chunks {
			chunks = append(chunks, executorChunk{Seq: c.Seq, Stream: c.Stream, Chunk: append([]byte{}, c.Data...)})
		}
		var code *int
		if r.ExitCode != nil {
			x := int(*r.ExitCode)
			code = &x
		}
		return protocolObject(executorReadResult{Chunks: chunks, NextSeq: r.NextSeq, Exited: r.Exited, ExitCode: code, Closed: r.Closed, Failure: r.Failure})
	case *executorpb.WriteProcessResponse:
		return map[string]any{"status": r.WriteStatus}, nil
	case *executorpb.SignalProcessResponse:
		return map[string]any{}, nil
	case *executorpb.TerminateProcessResponse:
		return map[string]any{"running": r.Running}, nil
	case *executorpb.GetFileMetadataResponse:
		return protocolObject(executorFSMetadata{IsDirectory: r.IsDirectory, IsFile: r.IsFile, IsSymlink: r.IsSymlink, Size: r.Size})
	case *executorpb.OpenFileResponse:
		return map[string]any{"handleId": r.HandleId}, nil
	case *executorpb.ReadFileResponse:
		return protocolObject(executorFSBlock{Chunk: append([]byte{}, r.Data...), EOF: r.Eof})
	case *executorpb.CloseFileResponse:
		return map[string]any{}, nil
	default:
		return nil, fmt.Errorf("unsupported typed execution response for %s", method)
	}
}

func decodeProtocolObject(data []byte, value any) error {
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return errors.New("protocol payload must be a JSON object")
	}
	decoder := json.NewDecoder(bytes.NewReader(trimmed))
	decoder.UseNumber()
	if err := decoder.Decode(value); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return errors.New("protocol payload has trailing JSON")
	}
	return nil
}

func protocolObject(value any) (map[string]any, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var object map[string]any
	if err := decodeProtocolObject(data, &object); err != nil {
		return nil, err
	}
	return object, nil
}

func executorRPC[R any, P any](ctx context.Context, e *Executor, method executionMethod, params P) (*R, error) {
	return executorRPCWithEvidence[R](ctx, e, method, params, nil)
}

func executorRPCWithEvidence[R any, P any](ctx context.Context, e *Executor, method executionMethod, params P, resultEvidence func(map[string]any) map[string]any) (*R, error) {
	raw, err := e.rpc(ctx, method, params, resultEvidence)
	if err != nil {
		return nil, err
	}
	var result R
	if err := decodeProtocolObject(raw, &result); err != nil {
		return nil, fmt.Errorf("decode typed execution %s result: %w", method, err)
	}
	return &result, nil
}
