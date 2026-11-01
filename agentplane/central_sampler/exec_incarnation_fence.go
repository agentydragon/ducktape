package centralsampler

import (
	"context"
	"errors"
	"fmt"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/execution/executorpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type expectedIncarnationUIDContextKey struct{}

func withExpectedIncarnationUID(ctx context.Context, uid *string) context.Context {
	if uid == nil {
		return ctx
	}
	return context.WithValue(ctx, expectedIncarnationUIDContextKey{}, *uid)
}

func expectedIncarnationUIDFromContext(ctx context.Context) (string, bool) {
	uid, ok := ctx.Value(expectedIncarnationUIDContextKey{}).(string)
	return uid, ok
}

type executionScopeContextKey struct{}

func withExecutionScope(ctx context.Context, scope *executorpb.ExecutionScope) context.Context {
	return context.WithValue(ctx, executionScopeContextKey{}, scope)
}

func executionScopeFromContext(ctx context.Context, fallback *executorpb.ExecutionScope) *executorpb.ExecutionScope {
	scope := fallback
	if override, ok := ctx.Value(executionScopeContextKey{}).(*executorpb.ExecutionScope); ok && override != nil {
		scope = override
	}
	if scope == nil {
		scope = &executorpb.ExecutionScope{}
	}
	scope = proto.Clone(scope).(*executorpb.ExecutionScope)
	if uid, explicit := expectedIncarnationUIDFromContext(ctx); explicit {
		scope.ExpectedIncarnationUid = uid
	}
	return scope
}

type incarnationRebindContextKey struct{}

type incarnationRebind struct {
	previousUID string
}

func withIncarnationRebind(ctx context.Context, previousUID string) context.Context {
	return context.WithValue(ctx, incarnationRebindContextKey{}, incarnationRebind{previousUID: previousUID})
}

func incarnationRebindFromContext(ctx context.Context) (incarnationRebind, bool) {
	rebind, ok := ctx.Value(incarnationRebindContextKey{}).(incarnationRebind)
	return rebind, ok
}

func (e *Executor) scopeSnapshot() *executorpb.ExecutionScope {
	e.scopeMu.RLock()
	defer e.scopeMu.RUnlock()
	if e.scope == nil {
		return &executorpb.ExecutionScope{}
	}
	return proto.Clone(e.scope).(*executorpb.ExecutionScope)
}

func (e *Executor) setScope(scope *executorpb.ExecutionScope) {
	e.scopeMu.Lock()
	defer e.scopeMu.Unlock()
	if scope == nil {
		e.scope = &executorpb.ExecutionScope{}
		return
	}
	e.scope = proto.Clone(scope).(*executorpb.ExecutionScope)
}

// executorIncarnationFenceError is returned only for an authenticated
// SandboxIncarnationChanged detail. In particular, ordinary authorization or
// routing failures never carry a current UID through this error.
type executorIncarnationFenceError struct {
	currentIncarnationUID string
	remoteOutcome         NestedOutcome
	method                executionMethod
	intentID              string
	processID             string
	executionSessionID    string
	afterSequence         *uint64
	output                *headTailBuffer
	recorded              bool
	cause                 error
}

func (e *executorIncarnationFenceError) Error() string {
	if e.remoteOutcome == NestedUnknown {
		return fmt.Sprintf("execution incarnation fence rejected %s; the existing process outcome is unknown and was not replayed; current authorized incarnation UID is %s", e.method, e.currentIncarnationUID)
	}
	return fmt.Sprintf("execution incarnation fence rejected %s before dispatch; current authorized incarnation UID is %s", e.method, e.currentIncarnationUID)
}

func (e *executorIncarnationFenceError) Unwrap() error { return e.cause }

func (e *executorIncarnationFenceError) executionFailureRecord() executionFailureRecord {
	requestOutcome := NestedNotDispatched
	return executionFailureRecord{
		message: e.Error(), method: e.method, intentID: e.intentID, processID: e.processID,
		executionSessionID: e.executionSessionID, afterSequence: e.afterSequence,
		remoteOutcome: e.remoteOutcome, requestOutcome: &requestOutcome, automaticReplay: false,
		currentIncarnationUID: e.currentIncarnationUID, output: e.output,
	}
}

func (e *executorIncarnationFenceError) intentEvidence() executionFailureEvidence {
	return e.executionFailureRecord().intentEvidence()
}

func (e *Executor) incarnationFenceError(err error, scope *executorpb.ExecutionScope, method executionMethod, intentID, processID, executionSessionID string, afterSequence *uint64, output *headTailBuffer, remoteOutcome NestedOutcome) *executorIncarnationFenceError {
	changed, ok := sandboxIncarnationChanged(err, scope)
	if !ok {
		return nil
	}
	return &executorIncarnationFenceError{
		currentIncarnationUID: changed.GetIncarnationUid(), remoteOutcome: remoteOutcome,
		method: method, intentID: intentID, processID: processID, executionSessionID: executionSessionID,
		afterSequence: afterSequence, output: output, cause: err,
	}
}

func (e *Executor) stopCurrentSessionStream(ctx context.Context) error {
	if e.sessionCancel == nil {
		return nil
	}
	cancel, done := e.sessionCancel, e.sessionDone
	cancel()
	if done != nil {
		select {
		case <-done:
		case <-ctx.Done():
			return fmt.Errorf("wait for old execution session stream: %w", ctx.Err())
		}
	}
	e.sessionStream, e.sessionCancel, e.sessionDone = nil, nil, nil
	return nil
}

func sandboxIncarnationChanged(err error, scope *executorpb.ExecutionScope) (*executorpb.SandboxIncarnationChanged, bool) {
	if err == nil || scope == nil || scope.GetDestination() == nil || scope.GetDestination().GetSandboxUid() == "" || scope.GetExpectedIncarnationUid() == "" {
		return nil, false
	}
	for current := err; current != nil; current = errors.Unwrap(current) {
		statusValue, ok := status.FromError(current)
		if !ok || statusValue.Code() != codes.FailedPrecondition {
			continue
		}
		for _, detail := range statusValue.Details() {
			changed, ok := detail.(*executorpb.SandboxIncarnationChanged)
			if !ok || changed.GetSandboxUid() != scope.GetDestination().GetSandboxUid() || changed.GetIncarnationUid() == "" || changed.GetIncarnationUid() == scope.GetExpectedIncarnationUid() {
				continue
			}
			return changed, true
		}
	}
	return nil, false
}
