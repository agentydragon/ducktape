package runtime

import (
	"context"
	"errors"
	"fmt"
	"github.com/agentydragon/ducktape/agentplane/env/cosi"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Accepted registration work remains counted until its component operation slot
// is visible. Publication failures and callbacks can report failure without
// reentering the admission mutex.
func (r *Runtime) admitLifecycleEffect(ctx context.Context, expected lifecycle.EnvironmentDesired, effect func(context.Context) error) (resultErr error) {
	r.mu.Lock()
	if err := ctx.Err(); err != nil {
		r.mu.Unlock()
		return err
	}
	if r.closing || r.failed || !r.admissionOpen || r.desired != expected || expected.Mode != lifecycle.Running {
		r.mu.Unlock()
		return nil
	}
	err := r.changeAdmissionsLocked(1)
	r.mu.Unlock()
	if err != nil {
		return err
	}
	defer r.finishAdmission(&resultErr)
	return effect(ctx)
}

func (r *Runtime) admitCodexRequest(ctx context.Context, effect func(context.Context) error) (resultErr error) {
	r.mu.Lock()
	if !r.acceptingLocked() {
		r.mu.Unlock()
		return status.Error(codes.Aborted, "this Sandbox is quiesced")
	}
	err := r.changeAdmissionsLocked(1)
	r.mu.Unlock()
	if err != nil {
		return err
	}
	defer r.finishAdmission(&resultErr)
	return effect(ctx)
}

// Publication and acceptance are different boundaries. The old gate closes
// synchronously; controllers observing the new resource cannot act until recovery
// finishes and Runtime accepts that exact revision.
func (r *Runtime) publishDesired(ctx context.Context, mode lifecycle.Mode, uid, sealUID string) (uint64, error) {
	r.mu.Lock()
	if r.closing || r.failed || !r.startupComplete {
		r.mu.Unlock()
		return 0, status.Error(codes.Unavailable, "environment Runtime is unavailable")
	}
	r.admissionOpen = false
	desired := lifecycle.EnvironmentDesired{Mode: mode, IncarnationUID: uid, SealUID: sealUID, Generation: r.desired.Generation + 1}
	err := lifecycle.PublishEnvironmentDesired(ctx, r.lifecycleState, desired)
	if err == nil {
		r.desired = desired
	}
	r.changedLocked()
	r.mu.Unlock()
	if err != nil {
		r.fail(fmt.Errorf("publish environment lifecycle: %w", err))
		return 0, err
	}
	if mode == lifecycle.Running {
		if err := r.sessions.ResetDemand(); err != nil {
			r.fail(err)
			return 0, err
		}
		if r.codexAppServer != nil {
			if err := r.codexAppServer.ResetDemand(ctx); err != nil {
				r.fail(err)
				return 0, err
			}
		}
		for _, s := range r.sessions.Inventory() {
			if err := r.sessions.Reopen(ctx, s); err != nil {
				r.fail(err)
				return 0, err
			}
			if err := r.sessions.ResumeRecovered(s); err != nil {
				r.fail(err)
				return 0, err
			}
		}
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closing || r.failed || r.desired != desired {
		return 0, status.Error(codes.Unavailable, "environment lifecycle changed before acceptance")
	}
	r.admissionOpen = mode == lifecycle.Running
	r.changedLocked()
	return desired.Generation, nil
}

func (r *Runtime) environment() session.Environment {
	r.mu.Lock()
	defer r.mu.Unlock()
	return session.Environment{IncarnationUID: r.desired.IncarnationUID, Quiesced: r.desired.Mode != lifecycle.Running || r.failed || r.closing}
}

func (r *Runtime) waitBootstrapForSession(ctx context.Context) error {
	r.mu.Lock()
	accepted := r.startupComplete && !r.failed && r.desired.Mode == lifecycle.Running
	r.mu.Unlock()
	if !accepted {
		return status.Error(codes.Aborted, "bootstrap wait is outside the active environment")
	}
	if r.bootstrapWait != nil {
		return r.bootstrapWait(ctx)
	}
	if r.bootstrapHost != nil {
		return r.bootstrapHost.Wait(ctx)
	}
	return nil
}

func (r *Runtime) executionReady(ctx context.Context) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.config.CodexExecServer == nil {
		return true, nil
	}
	return !r.closing && !r.failed && r.executionOwner.executor(r.desired.IncarnationUID) != nil, nil
}

func (r *Runtime) launchSessionProcess(ctx context.Context, owner string, supervisor *supervision.Supervisor, config supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
	r.mu.Lock()
	generation := r.desired.Generation
	r.mu.Unlock()
	return r.harnessProcesses.Launch(ctx, owner, generation, supervisor, config)
}

func (r *Runtime) cancelExecutionCalls() {
	r.mu.Lock()
	cancels := make([]context.CancelFunc, 0, len(r.execCalls)+len(r.execStreams))
	for _, cancel := range r.execCalls {
		cancels = append(cancels, cancel)
	}
	for _, cancel := range r.execStreams {
		cancels = append(cancels, cancel)
	}
	r.mu.Unlock()
	for _, cancel := range cancels {
		cancel()
	}
}

func (r *Runtime) waitForExecutionCalls(ctx context.Context) error {
	return r.waitForLifecycle(ctx, func() (bool, error) {
		r.mu.Lock()
		defer r.mu.Unlock()
		if failure := r.failureLocked(); failure != nil && !r.closing {
			return false, failure
		}
		return r.activeExecutionCalls == 0, nil
	})
}

func (r *Runtime) readSessionStatus(ctx context.Context) (lifecycle.Status, bool, error) {
	return lifecycle.ReadStatus(ctx, r.lifecycleState)
}

func (r *Runtime) waitSessionStage(ctx context.Context, minimum lifecycle.Stage) error {
	return r.waitForLifecycle(ctx, func() (bool, error) {
		r.mu.Lock()
		desired := r.desired
		failure := r.failureLocked()
		r.mu.Unlock()
		current, found, err := r.readSessionStatus(ctx)
		if err != nil {
			return false, err
		}
		if found && current.Generation == desired.Generation {
			if current.Error != "" {
				return false, errors.New(current.Error)
			}
			if current.Stage >= minimum {
				return true, nil
			}
		}
		return false, failure
	})
}

func (r *Runtime) waitForNativeStopped(ctx context.Context, generation uint64) error {
	return r.waitForLifecycle(ctx, func() (bool, error) {
		current, found, err := r.readSessionStatus(ctx)
		if err != nil {
			return false, err
		}
		if found && current.Generation == generation {
			if current.Error != "" {
				return false, errors.New(current.Error)
			}
			if current.NativeStopped {
				return true, nil
			}
		}
		r.mu.Lock()
		defer r.mu.Unlock()
		if r.desired.Generation != generation {
			return false, status.Error(codes.Aborted, "native-stop generation was superseded")
		}
		return false, r.failureLocked()
	})
}

func (r *Runtime) waitForLifecycle(ctx context.Context, predicate func() (bool, error)) error {
	for {
		changed := r.changesSince()
		done, err := predicate()
		if done || err != nil {
			return err
		}
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		case <-r.lifecycleDone:
			return errors.New("Runtime lifecycle engine stopped")
		}
	}
}

func (r *Runtime) requestExecutionFence(generation uint64) {
	r.mu.Lock()
	if r.fenceInFlight == generation {
		r.mu.Unlock()
		return
	}
	if r.fenceDone != nil {
		select {
		case <-r.fenceDone:
		default:
			r.mu.Unlock()
			r.fail(errors.New("overlapping Execution fences"))
			return
		}
	}
	r.fenceInFlight = generation
	r.fenceDone = make(chan struct{})
	r.fenceErr = nil
	done := r.fenceDone
	r.lifecycleWorkers.Add(1)
	r.mu.Unlock()
	go func() {
		defer r.lifecycleWorkers.Done()
		ctx, cancel := context.WithTimeout(context.Background(), executionFenceGrace)
		defer cancel()
		err := r.fenceExecution(ctx, generation)
		r.mu.Lock()
		r.fenceErr = err
		close(done)
		r.changedLocked()
		r.mu.Unlock()
		if err != nil {
			r.fail(err)
		}
	}()
}

func (r *Runtime) waitForExecutionFenced(ctx context.Context, generation uint64) error {
	r.requestExecutionFence(generation)
	r.mu.Lock()
	done := r.fenceDone
	r.mu.Unlock()
	select {
	case <-done:
	case <-ctx.Done():
		return ctx.Err()
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.fenceInFlight != generation {
		return errors.New("Execution fence generation changed")
	}
	return r.fenceErr
}

func (r *Runtime) fenceExecution(ctx context.Context, generation uint64) error {
	if err := r.stopExecution(ctx); err != nil {
		return err
	}
	r.mu.Lock()
	if r.desired.Generation != generation {
		r.mu.Unlock()
		return status.Error(codes.Aborted, "Execution fence belongs to a stale lifecycle generation")
	}
	r.mu.Unlock()
	if r.executionLifecycle != nil {
		current, found, err := cosi.Read(ctx, r.lifecycleState, executionStatusResource(executionStatus{}))
		if err != nil {
			return err
		}
		if !found || current.Phase != executionStopped || current.FencedGeneration != current.Generation {
			return errors.New("Execution attempt is not fenced")
		}
		if !r.executionLifecycle.publishCompletion(func(input *executionSignal) bool {
			if input.Fence.Generation != 0 && input.Fence.Generation != current.Generation {
				return false
			}
			input.Fence.LifecycleGeneration = generation
			return true
		}) {
			return errors.New("publish exact Execution fence")
		}
	}
	r.changed()
	return nil
}
