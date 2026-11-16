package transport

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/outputbuffer"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type expectedIncarnationUIDContextKey struct{}

func WithExpectedIncarnationUID(ctx context.Context, uid *string) context.Context {
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

func (e *Transport) ScopeSnapshot() *executorpb.ExecutionScope {
	e.scopeMu.RLock()
	defer e.scopeMu.RUnlock()
	if e.scope == nil {
		return &executorpb.ExecutionScope{}
	}
	return proto.Clone(e.scope).(*executorpb.ExecutionScope)
}

func (e *Transport) setScope(scope *executorpb.ExecutionScope) {
	e.scopeMu.Lock()
	defer e.scopeMu.Unlock()
	if scope == nil {
		e.scope = &executorpb.ExecutionScope{}
		return
	}
	e.scope = proto.Clone(scope).(*executorpb.ExecutionScope)
}

// incarnationFenceError accepts only authenticated SandboxIncarnationChanged details.
func (e *Transport) incarnationFenceError(err error, scope *executorpb.ExecutionScope, method Method, intentID, processID, executionSessionID string, afterSequence *uint64, output *outputbuffer.Buffer, remoteOutcome Outcome) *Failure {
	changed, ok := sandboxIncarnationChanged(err, scope)
	if !ok {
		return nil
	}
	return &Failure{
		Kind: FailureIncarnationFence, Method: method, IntentID: intentID, ProcessID: processID,
		ExecutionSessionID: executionSessionID, AfterSequence: afterSequence, Output: output,
		RemoteOutcome: remoteOutcome, RequestOutcome: outcomePointer(NotDispatched),
		CurrentIncarnationUID: changed.GetIncarnationUid(), Cause: err,
	}
}

func (e *Transport) stopCurrentSessionStream(ctx context.Context) error {
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
	if err == nil || scope == nil || scope.GetDestination() == nil || scope.GetDestination().GetTarget().GetUid() == "" || scope.GetExpectedIncarnationUid() == "" {
		return nil, false
	}
	for current := err; current != nil; current = errors.Unwrap(current) {
		statusValue, ok := status.FromError(current)
		if !ok || statusValue.Code() != codes.FailedPrecondition {
			continue
		}
		for _, detail := range statusValue.Details() {
			changed, ok := detail.(*executorpb.SandboxIncarnationChanged)
			if !ok || changed.GetSandboxUid() != scope.GetDestination().GetTarget().GetUid() || changed.GetIncarnationUid() == "" || changed.GetIncarnationUid() == scope.GetExpectedIncarnationUid() {
				continue
			}
			return changed, true
		}
	}
	return nil, false
}
