package coordinator

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (r *Runtime) runQuiesce(ctx, engineCtx context.Context, request *runtimeTransitionRequest) error {
	if err := transitionContextError(ctx, engineCtx); err != nil {
		return err
	}
	facts, err := r.facts()
	if err != nil {
		return err
	}
	r.mu.Lock()
	if err := transitionContextError(ctx, engineCtx); err != nil {
		r.mu.Unlock()
		return err
	}
	if r.closing || r.failed || !r.startupComplete {
		r.mu.Unlock()
		return status.Error(codes.Unavailable, "environment Runtime is unavailable")
	}
	if !r.state.Lameduck() && request.HasIdleSince && (facts.IdleSince == nil || facts.IdleSince.AsTime().UnixMicro() != request.IdleSinceMicros || r.activeAdmissions != 0 || r.activeExecutionCalls != 0 || r.executionOwner.recovering() || r.idleSince == nil || r.idleSince.AsTime().UnixMicro() != request.IdleSinceMicros) {
		r.mu.Unlock()
		return status.Error(codes.Aborted, "the Sandbox is busy or its idle_since differs")
	}
	r.admissionOpen = false
	// Teardown supersedes a RAM-preserving suspend. Retire its separate gate
	// only after lifecycle admission is closed under this same lock; native
	// cleanup may now use the ordinary Quiesce path without opening new work.
	r.suspendPrepared = false
	r.suspendIdleSinceMicros = 0
	if err := r.state.EnterLameduck(); err != nil {
		r.mu.Unlock()
		r.fail(err)
		return err
	}
	uid, err := r.state.RequireIncarnationUID()
	if err != nil {
		r.mu.Unlock()
		return err
	}
	current := r.desired
	r.mu.Unlock()
	if current.Mode == lifecycle.Quiescing && current.SealUID == uid {
		progress, found, err := lifecycle.ReadStatus(ctx, r.lifecycleState)
		if err != nil {
			return err
		}
		if !found || progress.Generation != current.Generation || progress.Error == "" {
			return nil
		}
	}
	_, err = r.publishDesired(ctx, lifecycle.Quiescing, uid, uid)
	return err
}
