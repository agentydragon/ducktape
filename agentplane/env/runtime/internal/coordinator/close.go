package coordinator

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
)

// Close stops Runtime-owned work, proves its fences, and releases the state store.
// Canceling ctx only stops this caller's wait; shutdown and worker joining continue.
func (r *Runtime) Close(ctx context.Context) error {
	r.mu.Lock()
	r.closing = true
	r.admissionOpen = false
	if r.activeTransitionCancel != nil {
		r.activeTransitionCancel()
	}
	r.changedLocked()
	r.mu.Unlock()
	if r.executionLifecycle != nil {
		r.executionLifecycle.cancelStart()
	}
	r.closeOnce.Do(func() {
		go func() {
			r.lifecycleStartMu.Lock()
			if r.lifecycleInitErr != nil {
				r.closeErr = fmt.Errorf("Session construction left lifecycle engine unsafe to run: %w", r.lifecycleInitErr)
				r.lifecycleStartMu.Unlock()
			} else {
				r.startLifecycleEngineLocked()
				r.lifecycleStartMu.Unlock()
				r.closeErr = r.finishClose()
			}
			close(r.closeDone)
		}()
	})
	select {
	case <-r.closeDone:
		return r.closeErr
	default:
	}
	select {
	case <-r.closeDone:
		return r.closeErr
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (r *Runtime) finishClose() error {
	desired, err := r.publishClosing()
	if err != nil {
		return err
	}
	sessionLossErr, err := r.closeComponents(desired)
	if r.lifecycleContext.Err() != nil {
		return r.coordinatorFailure("Close completion")
	}
	if err != nil {
		return err
	}
	return r.joinClose(sessionLossErr)
}

func (r *Runtime) publishClosing() (lifecycle.EnvironmentDesired, error) {
	// Settle the accepted transition and any quiescence fence before publishing a
	// successor shutdown generation. A late old fence must never fail or overwrite
	// the generation that Close is about to own.
	r.transitionMu.Lock()
	defer r.transitionMu.Unlock()
	r.mu.Lock()
	priorFence := r.executionFence
	r.mu.Unlock()
	if priorFence != nil {
		select {
		case <-priorFence.done:
		case <-r.lifecycleContext.Done():
		}
	}
	if r.lifecycleContext.Err() != nil {
		return lifecycle.EnvironmentDesired{}, r.coordinatorFailure("Close transition settlement")
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	desired := r.desired
	desired.Mode, desired.Generation = lifecycle.Closing, desired.Generation+1
	if sealedUID, sealed, err := r.state.SealedUID(); err != nil {
		return lifecycle.EnvironmentDesired{}, err
	} else if sealed {
		desired.SealUID = sealedUID
		desired.IncarnationUID = sealedUID
	}
	if err := lifecycle.PublishEnvironmentDesired(context.Background(), r.lifecycleState, desired); err != nil {
		return lifecycle.EnvironmentDesired{}, fmt.Errorf("publish Runtime closing state: %w", err)
	}
	r.desired = desired
	r.changedLocked()
	return desired, nil
}

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
	sessionLossErr := r.failureErr
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

func (r *Runtime) joinClose(sessionLossErr error) error {
	// The aggregate has stopped every component that can need the engine. Its
	// completion means only that it is now safe to join that engine.
	r.lifecycleCancel()
	<-r.lifecycleDone
	r.mu.Lock()
	lifecycleRunErr := r.lifecycleRunErr
	lifecycleControllerErr := r.lifecycleControllerErr
	r.mu.Unlock()
	if lifecycleRunErr != nil || lifecycleControllerErr != nil {
		return errors.Join(lifecycleRunErr, lifecycleControllerErr)
	}
	if r.sessions != nil {
		<-r.sessionWorkerDone
		// Recovery completion does not prove native cleanup. Retain Runtime's
		// owner if the Session component cannot finish its joined work.
		if cleanupErr := r.closeSessions(context.Background()); cleanupErr != nil {
			return errors.Join(sessionLossErr, r.sessionWorkerErr, fmt.Errorf("Session cleanup unproved: %w", cleanupErr))
		}
		sessionLossErr = errors.Join(sessionLossErr, r.sessionWorkerErr)
	}
	r.lifecycleWorkers.Wait()
	if r.config.Supervision == "pid1" {
		if err := r.supervisor.FenceNamespace(context.Background()); err != nil {
			return fmt.Errorf("fence Pod PID namespace: %w", err)
		}
	}
	if r.security != nil {
		sessionLossErr = errors.Join(sessionLossErr, r.security.Close())
	}
	if r.lifecycleStore != nil {
		sessionLossErr = errors.Join(sessionLossErr, r.lifecycleStore.Close())
	}
	return sessionLossErr
}

func (r *Runtime) waitCloseNativeStop(fenceCtx context.Context, generation uint64) error {
	for {
		changed := r.changesSince()
		settled, err := r.closeNativeStopSettled(r.lifecycleContext, r.lifecycleState, generation, fenceCtx)
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
func (r *Runtime) closeNativeStopSettled(ctx context.Context, reader controller.Reader, generation uint64, fenceCtx context.Context) (bool, error) {
	settled, err := r.observeNativeStop(ctx, reader, generation, true)
	if settled || err != nil {
		return true, err
	}
	if err := fenceCtx.Err(); err != nil {
		return true, err
	}
	return false, nil
}
