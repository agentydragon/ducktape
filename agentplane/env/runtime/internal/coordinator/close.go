package coordinator

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
)

// closeComponents is owned by the one Close worker. Component controllers keep
// reconciling until their exact-generation cleanup and delivery proofs settle.
func (r *Runtime) closeComponents(desired lifecycle.EnvironmentDesired) (error, error) {
	current, found, err := lifecycle.ReadEnvironmentDesired(r.lifecycleContext, r.lifecycleState)
	if err != nil {
		return nil, fmt.Errorf("read Runtime closing state: %w", err)
	}
	if !found || current != desired {
		return nil, fmt.Errorf("Runtime closing state does not match generation %d", desired.Generation)
	}
	r.mu.Lock()
	if r.transitionUnsafe != nil {
		err := r.transitionUnsafe
		r.mu.Unlock()
		return nil, fmt.Errorf("prior Runtime transition has an uncertain Execution fence: %w", err)
	}
	sessionsStarted := r.sessions != nil
	sessionLossErr := r.failureLocked()
	r.mu.Unlock()

	fenceCtx, cancel := context.WithTimeout(r.lifecycleContext, executionFenceGrace)
	defer cancel()
	var nativeErr error
	if sessionsStarted {
		nativeErr = r.waitCloseNativeStop(fenceCtx, desired.Generation)
	}
	if r.lifecycleContext.Err() != nil {
		return sessionLossErr, r.lifecycleContext.Err()
	}
	// Even if native cleanup fails, attempt the Execution fence before returning.
	if err := r.waitForExecutionFenced(fenceCtx, desired.Generation); err != nil {
		return sessionLossErr, err
	}
	if nativeErr != nil {
		return sessionLossErr, errors.Join(nativeErr, sessionLossErr)
	}
	if desired.SealUID != "" && sessionsStarted {
		if err := r.waitSessionStage(fenceCtx, lifecycle.Quiesced); err != nil {
			return sessionLossErr, err
		}
	}
	if err := r.stopIngress(fenceCtx); err != nil {
		return sessionLossErr, fmt.Errorf("join History delivery: %w", err)
	}
	if sessionsStarted {
		if err := r.stopSessions(fenceCtx); err != nil {
			return sessionLossErr, err
		}
		if err := r.waitSessionsClosed(fenceCtx); err != nil {
			return sessionLossErr, err
		}
	}
	if err := r.waitForLifecycle(r.lifecycleContext, func() (bool, error) {
		return r.bootstrapHost.Settled(r.lifecycleContext)
	}); err != nil {
		return sessionLossErr, fmt.Errorf("observe accepted bootstrap cleanup: %w", err)
	}
	return sessionLossErr, nil
}

func (r *Runtime) waitCloseNativeStop(fenceCtx context.Context, generation uint64) error {
	for {
		changed := r.changesSince()
		settled, err := r.closeNativeStopSettled(r.lifecycleContext, r.lifecycleState, generation, true, fenceCtx)
		if settled || err != nil {
			return err
		}
		select {
		case <-changed:
		case <-fenceCtx.Done():
			// Give the exact-generation status a final read before timing out.
		}
	}
}

// Read the exact-generation proof before checking its deadline, so a settled
// observation remains usable even when the deadline expires during the read.
func (r *Runtime) closeNativeStopSettled(ctx context.Context, reader controller.Reader, generation uint64, sessionsStarted bool, fenceCtx context.Context) (bool, error) {
	if !sessionsStarted {
		return true, nil
	}
	settled, err := r.observeNativeStop(ctx, reader, generation, true)
	if settled || err != nil {
		return true, err
	}
	if err := fenceCtx.Err(); err != nil {
		return true, err
	}
	return false, nil
}
