package centralsampler

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

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
	return e.openSessionWithHandshake(ctx, resume, e.initializeTimeout)
}

func (e *Executor) openSessionWithHandshake(ctx context.Context, resume *string, handshakeTimeout time.Duration) error {
	err := e.openSessionAtScopeWithHandshake(ctx, resume, handshakeTimeout)
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
		if err := e.store.RebindExecutorIncarnation(ctx, e.owner, scope.GetDestination().GetTarget().GetUid(), scope.GetExpectedIncarnationUid(), fence.currentIncarnationUID, ""); err != nil {
			return fmt.Errorf("record execution Sandbox incarnation change: %w", err)
		}
		slog.Info("central sampler executor incarnation replaced", "session_id", scope.GetSessionId(), "sandbox_uid", scope.GetDestination().GetTarget().GetUid(),
			"previous_incarnation_uid", scope.GetExpectedIncarnationUid(), "incarnation_uid", fence.currentIncarnationUID, "retired_execution_session_id", e.sessionID)
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
	return e.openSessionAtScopeWithHandshake(ctx, resume, e.initializeTimeout)
}

func (e *Executor) openSessionAtScopeWithHandshake(ctx context.Context, resume *string, handshakeTimeout time.Duration) error {
	scope := executionScopeFromContext(ctx, e.scopeSnapshot())
	handshakeCtx, cancelHandshake := context.WithTimeout(ctx, handshakeTimeout)
	defer cancelHandshake()
	intentRequest := map[string]any{"method": executorMethodInitialize, "resume_session_id": resume}
	if scope.GetDestination().GetTarget().GetUid() != "" {
		intentRequest["execution_scope"] = map[string]any{
			"sandbox_uid": scope.GetDestination().GetTarget().GetUid(), "expected_incarnation_uid": scope.GetExpectedIncarnationUid(),
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
	// The live stream outlives the handshake, but its creation must still obey
	// the caller's deadline. Detach the cancellation only after a valid first
	// response has been durably bound below.
	stopHandshakeCancel := context.AfterFunc(handshakeCtx, cancel)
	defer stopHandshakeCancel()
	stream, err := e.client.OpenSession(streamCtx, &executorpb.OpenSessionRequest{Scope: scope, RequestId: intent, ResumeSessionId: resume})
	if err != nil {
		cancel()
		return e.finishOpenSessionFailure(ctx, handshakeCtx, intent, scope, "open", err)
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
		// Never accept a response from a stream after its handshake deadline.
		// The receive must end before another resume may be attempted, so two
		// OpenSession streams cannot overlap on the retained native session.
		select {
		case <-first:
			err = handshakeCtx.Err()
		case <-time.After(time.Second):
			err = errors.New("executor OpenSession receive did not stop after handshake cancellation")
		}
	}
	if err != nil {
		cancel()
		return e.finishOpenSessionFailure(ctx, handshakeCtx, intent, scope, "receive", err)
	}
	if opened == nil {
		cancel()
		transport := &executorTransportError{method: executorMethodInitialize, phase: "response", intentID: intent, retainedSession: e.retainedSessionID(ctx), cause: errors.New("executor OpenSession returned an empty response")}
		if err := e.finishNested(ctx, intent, transport.evidence(transport.retainedSession)); err != nil {
			return fmt.Errorf("record empty OpenSession response: %w", err)
		}
		return transport
	}
	stoppedHandshakeCancel := stopHandshakeCancel()
	if !stoppedHandshakeCancel || handshakeCtx.Err() != nil {
		// The deadline callback may already be canceling this stream even when
		// Recv won the select above. A first frame from that stream cannot be
		// installed as a healthy long-lived binding.
		cancel()
		cause := handshakeCtx.Err()
		if cause == nil {
			cause = errors.New("executor OpenSession handshake cancellation raced its first response")
		}
		transport := &executorTransportError{method: executorMethodInitialize, phase: "receive", intentID: intent, retainedSession: e.retainedSessionID(ctx), cause: cause}
		if finishErr := e.finishNested(ctx, intent, transport.evidence(transport.retainedSession)); finishErr != nil {
			return fmt.Errorf("record OpenSession canceled first response after %v: %w", transport, finishErr)
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
		finishErr := e.finishNested(ctx, intent, transport.evidence(transport.retainedSession))
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
	if err := e.finishNested(ctx, intent, map[string]any{
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
	rebind, rebound := incarnationRebindFromContext(ctx)
	if rebound {
		e.setReconnectRequired(true)
		if err := e.stopCurrentSessionStream(ctx); err != nil {
			cancel()
			return fmt.Errorf("stop old execution session after verified incarnation change: %w", err)
		}
		if err := e.store.RebindExecutorIncarnationWithSession(ctx, e.owner,
			scope.GetDestination().GetTarget().GetUid(), rebind.previousUID, scope.GetExpectedIncarnationUid(),
			opened.ExecutionSessionId, shellName); err != nil {
			cancel()
			return fmt.Errorf("record verified execution Sandbox incarnation change: %w", err)
		}
		slog.Info("central sampler executor incarnation replaced", "session_id", scope.GetSessionId(), "sandbox_uid", scope.GetDestination().GetTarget().GetUid(),
			"previous_incarnation_uid", rebind.previousUID, "incarnation_uid", scope.GetExpectedIncarnationUid(), "retired_execution_session_id", e.sessionID)
		e.setScope(scope)
		e.workspacePrepared = false
		e.lostSessionConfirmed = false
	} else if err := e.store.SetExecutorSession(ctx, e.owner, opened.ExecutionSessionId, shellName); err != nil {
		cancel()
		return err
	}
	slog.Info("central sampler executor session opened", "session_id", scope.GetSessionId(), "sandbox_uid", scope.GetDestination().GetTarget().GetUid(),
		"incarnation_uid", scope.GetExpectedIncarnationUid(), "execution_session_id", opened.ExecutionSessionId, "resumed", resume != nil, "rebound", rebound)
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

// finishOpenSessionFailure records the shared refusal, incarnation-fence, or unknown-outcome
// classification for failures to create the stream or receive its first frame. phase stays in
// transport evidence so callers can distinguish those two failure boundaries.
func (e *Executor) finishOpenSessionFailure(ctx, handshakeCtx context.Context, intent string, scope *executorpb.ExecutionScope, phase string, err error) error {
	if handshakeCtx.Err() != nil && (errors.Is(err, context.Canceled) || status.Code(err) == codes.Canceled) {
		err = handshakeCtx.Err()
	}
	if environmentFailure := e.newEnvironmentFailure(executorMethodInitialize, intent, "", "", nil, nil, err); environmentFailure != nil {
		if finishErr := e.finishNested(ctx, intent, environmentFailure.intentEvidence()); finishErr != nil {
			return fmt.Errorf("record known OpenSession refusal after %v: %w", environmentFailure, finishErr)
		}
		return environmentFailure
	}
	if fence := e.incarnationFenceError(err, scope, executorMethodInitialize, intent, "", "", nil, nil, NestedNotDispatched); fence != nil {
		if finishErr := e.finishNested(ctx, intent, fence.intentEvidence()); finishErr != nil {
			return errors.Join(fence, fmt.Errorf("record stale OpenSession fence: %w", finishErr))
		}
		fence.recorded = true
		return fence
	}
	transport := &executorTransportError{method: executorMethodInitialize, phase: phase, intentID: intent, retainedSession: e.retainedSessionID(ctx), cause: err}
	if finishErr := e.finishNested(ctx, intent, transport.evidence(transport.retainedSession)); finishErr != nil {
		outcome := "transport"
		if phase == "receive" {
			outcome = "receive"
		}
		return fmt.Errorf("record OpenSession %s outcome after %v: %w", outcome, transport, finishErr)
	}
	return transport
}

func (e *Executor) finishOpenFailure(ctx context.Context, intent string, response *executorpb.OpenSessionResponse, nativeEvidenceErr error) error {
	if response.Status != nil && response.Status.Outcome == executorpb.ExecutionStatus_OUTCOME_REJECTED {
		message := response.Status.Message
		if message == "" {
			message = "session open rejected"
		}
		rejectedError := &executorRejectedError{method: executorMethodInitialize, code: int(response.Status.GetUpstreamCode()), message: message}
		rejected := map[string]any{"code": response.Status.UpstreamCode, "message": message}
		if err := e.finishNested(ctx, intent, map[string]any{"outcome": NestedRejected, "request_id": intent, "rejected": rejected}); err != nil {
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
	if err := e.finishNested(ctx, intent, map[string]any{"outcome": NestedUnknown, "request_id": intent, "message": message}); err != nil {
		return errors.Join(nativeEvidenceErr, errors.New(message), fmt.Errorf("record unknown OpenSession outcome: %w", err))
	}
	if nativeEvidenceErr != nil {
		return fmt.Errorf("record unknown OpenSession native evidence: %w", nativeEvidenceErr)
	}
	return &executorTransportError{method: executorMethodInitialize, phase: "response", intentID: intent, retainedSession: e.retainedSessionID(ctx), cause: errors.New(message)}
}

func (e *Executor) watchSession(streamCtx context.Context, stream executorSessionStream, operationID, sessionID string, generation uint64, cancel context.CancelFunc, done chan struct{}) {
	defer close(done)
	defer e.signalProcessChange()
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
		if len(response.NativeFrames) != 0 {
			// Notifications are evidence and wakeups. Cursor reads remain authoritative for
			// output, so duplicate or coalesced wakeups cannot duplicate process bytes.
			e.signalProcessChange()
		}
	}
}

func (e *Executor) recordNativeFrames(ctx context.Context, operationID, sessionID string, method executionMethod, frames []*executorpb.ExecutionNativeFrame) error {
	// The RPC deadline bounds receiving the response, not committing its evidence.
	// Store writes still verify the Session lease after caller or stream cancellation.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
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
		phase, err := nativeFramePhase(frame.Direction)
		if err != nil {
			return err
		}
		if err := e.store.ProtocolEvent(ctx, e.owner, operationID, eventpb.ExecutionProgress_KIND_EXECUTOR, phase, payload); err != nil {
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
			if currentScope.GetExpectedIncarnationUid() == "" || currentScope.GetDestination().GetTarget().GetUid() == "" {
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
	// A resume has no tool effect. Keep trying the same native session across a short
	// Gateway outage, but bound both the whole reconnect and each handshake. In
	// particular, an unavailable Gateway is not evidence that the exec-server or
	// its session has disappeared.
	deadline := time.Now().Add(e.reconnectTimeout)
	backoff := 100 * time.Millisecond
	var lastTransport error
	for {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return fmt.Errorf("executor session resume retry budget exhausted: %w", lastTransport)
		}
		attemptTimeout := min(remaining, min(5*time.Second, max(100*time.Millisecond, e.reconnectTimeout/4)))
		err := e.openSessionWithHandshake(ctx, &retainedSession, attemptTimeout)
		if err == nil {
			return nil
		}
		var rejected *executorRejectedError
		if errors.As(err, &rejected) && isDefinitiveLostExecutorSession(rejected, retainedSession) {
			if retireErr := e.store.RetireLostExecutorSession(ctx, e.owner, retainedSession, notDispatchedProcessID); retireErr != nil {
				return fmt.Errorf("retire lost executor session after %v: %w", err, retireErr)
			}
			scope := e.scopeSnapshot()
			slog.Warn("central sampler executor session lost", "session_id", scope.GetSessionId(), "sandbox_uid", scope.GetDestination().GetTarget().GetUid(),
				"incarnation_uid", scope.GetExpectedIncarnationUid(), "execution_session_id", retainedSession, "not_dispatched_process_id", notDispatchedProcessID)
			e.lostSessionConfirmed = true
			e.shell = nil
			if freshErr := e.openSession(ctx, nil); freshErr != nil {
				return fmt.Errorf("open fresh executor session after definitive session loss (%v): %w", err, freshErr)
			}
			return &executorSessionReplacedError{previousSession: retainedSession}
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if !retryableExecutorResumeFailure(err) {
			return err
		}
		lastTransport = err
		wait := min(backoff, time.Until(deadline))
		if wait <= 0 {
			continue
		}
		timer := time.NewTimer(wait)
		select {
		case <-timer.C:
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		}
		backoff = min(backoff*2, time.Second)
	}
}

func retryableExecutorResumeFailure(err error) bool {
	var transport *executorTransportError
	if !errors.As(err, &transport) || transport.phase != "open" && transport.phase != "receive" {
		return false
	}
	code := status.Code(transport.cause)
	return code == codes.Unavailable || code == codes.DeadlineExceeded || errors.Is(transport.cause, context.DeadlineExceeded)
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
