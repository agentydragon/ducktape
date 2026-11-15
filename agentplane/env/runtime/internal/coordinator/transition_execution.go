package coordinator

import (
	"context"
	"errors"
	"fmt"
)

func (r *Runtime) startTransitionExecution(ctx, engineCtx context.Context, uid string) error {
	if err := transitionContextError(ctx, engineCtx); err != nil {
		return err
	}
	err := r.startExecutionForUID(ctx, uid)
	if err == nil {
		return nil
	}
	// Startup may return on epoch cancellation while its COSI operation is still
	// in flight. Settle that start even if no owner handle was published yet.
	fenceCtx, cancel := context.WithTimeout(engineCtx, executionFenceGrace)
	defer cancel()
	if fenceErr := r.stopExecution(fenceCtx); fenceErr != nil {
		unsafe := errors.Join(err, fmt.Errorf("fence interrupted Execution start: %w", fenceErr))
		r.mu.Lock()
		r.transitionUnsafe = unsafe
		r.mu.Unlock()
		return unsafe
	}
	return err
}

func (r *Runtime) fenceTransitionExecution(ctx, engineCtx context.Context, generation uint64) error {
	if err := transitionContextError(ctx, engineCtx); err != nil {
		return err
	}
	err := r.waitForExecutionFenced(ctx, generation)
	if err == nil {
		return nil
	}
	if errors.Is(err, context.Canceled) && !r.isClosing() {
		fenceCtx, cancel := context.WithTimeout(engineCtx, executionFenceGrace)
		defer cancel()
		if settleErr := r.waitForExecutionFenced(fenceCtx, generation); settleErr == nil {
			return err
		} else {
			err = errors.Join(err, fmt.Errorf("settle exact Execution fence after epoch loss: %w", settleErr))
		}
	}
	if !r.isClosing() {
		r.mu.Lock()
		r.transitionUnsafe = err
		r.mu.Unlock()
	}
	return err
}
