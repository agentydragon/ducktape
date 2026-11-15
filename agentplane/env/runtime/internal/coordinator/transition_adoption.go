package coordinator

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (r *Runtime) runAdoption(ctx, engineCtx context.Context, uid string) error {
	if err := transitionContextError(ctx, engineCtx); err != nil {
		return err
	}
	r.mu.Lock()
	if r.closing || r.failed || !r.startupComplete {
		r.mu.Unlock()
		return status.Error(codes.Unavailable, "environment Runtime is unavailable")
	}
	desired := r.desired
	accepted := r.admissionOpen
	r.mu.Unlock()
	sealedUID, sealed, err := r.state.SealedUID()
	if err != nil {
		return err
	}
	oldUID, known := r.state.IncarnationUID()
	if sealed {
		oldUID, known = sealedUID, true
	}
	if known && oldUID == uid && !sealed && desired.Mode == lifecycle.Running {
		if !accepted {
			return status.Error(codes.Unavailable, "environment revision has not completed preparation")
		}
		if err := transitionContextError(ctx, engineCtx); err != nil {
			return err
		}
		return r.requestBootstrap(ctx)
	}
	if desired.Mode == lifecycle.Inactive {
		if err := r.waitForNativeStopped(ctx, desired.Generation); err != nil {
			return err
		}
	}
	generation := desired.Generation
	if known {
		seal := ""
		if sealed {
			seal = oldUID
		}
		if desired.Mode != lifecycle.Quiescing || desired.IncarnationUID != oldUID || desired.SealUID != seal {
			if sealed {
				// Recover the sealed incarnation's Execution owner before asking
				// its controllers to finish native cleanup and sealed delivery.
				if err := r.startTransitionExecution(ctx, engineCtx, oldUID); err != nil {
					return err
				}
			}
			if err := transitionContextError(ctx, engineCtx); err != nil {
				return err
			}
			generation, err = r.publishDesired(ctx, lifecycle.Quiescing, oldUID, seal)
			if err != nil {
				return err
			}
		}
		if err := transitionContextError(ctx, engineCtx); err != nil {
			return err
		}
		if err := r.waitForNativeStopped(ctx, generation); err != nil {
			return err
		}
		if err := r.fenceTransitionExecution(ctx, engineCtx, generation); err != nil {
			return err
		}
		if sealed {
			if err := transitionContextError(ctx, engineCtx); err != nil {
				return err
			}
			if err := r.waitSessionStage(ctx, lifecycle.Quiesced); err != nil {
				return err
			}
		}
	}
	if err := transitionContextError(ctx, engineCtx); err != nil {
		return err
	}
	if err := r.state.AdoptIncarnation(uid); err != nil {
		return err
	}
	if sealed && oldUID == uid {
		return nil
	}
	if err := r.startTransitionExecution(ctx, engineCtx, uid); err != nil {
		return err
	}
	if err := transitionContextError(ctx, engineCtx); err != nil {
		return err
	}
	if _, err := r.publishDesired(ctx, lifecycle.Running, uid, ""); err != nil {
		return err
	}
	if err := transitionContextError(ctx, engineCtx); err != nil {
		return err
	}
	return r.requestBootstrap(ctx)
}
