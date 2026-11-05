package runtime

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/cosi-project/runtime/pkg/controller"
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
	if r.closing || r.failed || !r.admissionOpen || r.suspendPrepared || r.desired != expected || expected.Mode != lifecycle.Running {
		r.mu.Unlock()
		return nil
	}
	err := r.changeAdmissionsLocked(1)
	r.idleSince = nil
	r.mu.Unlock()
	if err != nil {
		return err
	}
	defer r.finishAdmission(&resultErr)
	resultErr = effect(ctx)
	if resultErr != nil {
		r.fail(resultErr)
	}
	return resultErr
}

func (r *Runtime) admitCodexRequest(ctx context.Context, effect func(context.Context) error) (resultErr error) {
	r.mu.Lock()
	if !r.acceptingLocked() {
		suspending := r.suspendPrepared
		r.mu.Unlock()
		if suspending {
			return status.Error(codes.Unavailable, "environment Runtime is preparing to suspend")
		}
		return status.Error(codes.Aborted, "this Sandbox is quiesced")
	}
	err := r.changeAdmissionsLocked(1)
	r.idleSince = nil
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
	// Reserve the revision even on a failed write: its durable outcome can be
	// unknown, so a later Close must never reuse it for a different desire.
	r.desired = desired
	err := lifecycle.PublishEnvironmentDesired(ctx, r.lifecycleState, desired)
	if err != nil {
		r.failed = true
		if r.failureErr == nil {
			r.failureErr = err
		}
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
			if err := r.codexAppServer.ResetDemand(r.lifecycleContext); err != nil {
				r.fail(err)
				return 0, err
			}
		}
		for _, s := range r.sessions.Inventory() {
			if err := r.sessions.Reopen(r.lifecycleContext, s); err != nil {
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
	if mode == lifecycle.Running {
		if err := r.changeAdmissionsLocked(0); err != nil {
			return 0, err
		}
	}
	r.changedLocked()
	return desired.Generation, nil
}

func (r *Runtime) environment() session.Environment {
	r.mu.Lock()
	defer r.mu.Unlock()
	return session.Environment{IncarnationUID: r.desired.IncarnationUID, Quiesced: r.desired.Mode != lifecycle.Running || r.failed || r.closing}
}

func (r *Runtime) waitBootstrapForSession(ctx context.Context) error {
	// Session operations reach this dependency only after admission. Draining
	// closes new work while accepted setup still joins its retained bootstrap.
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
		current, found, err := r.readSessionStatus(ctx)
		r.mu.Lock()
		defer r.mu.Unlock()
		if err != nil {
			return false, err
		}
		if found && current.Generation == r.desired.Generation {
			if current.Error != "" {
				return false, errors.New(current.Error)
			}
			if current.Stage >= minimum {
				return true, nil
			}
		}
		return false, r.failureLocked()
	})
}

func (r *Runtime) waitForNativeStopped(ctx context.Context, generation uint64) error {
	return r.waitForNativeStop(ctx, generation, false)
}

func (r *Runtime) waitForNativeStop(ctx context.Context, generation uint64, closing bool) error {
	return r.waitForLifecycle(ctx, func() (bool, error) {
		return r.observeNativeStop(ctx, r.lifecycleState, generation, closing)
	})
}

// Close and transition waits use the same exact-generation native cleanup proof.
func (r *Runtime) observeNativeStop(ctx context.Context, reader controller.Reader, generation uint64, closing bool) (bool, error) {
	current, found, err := lifecycle.ReadStatus(ctx, reader)
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.desired.Generation != generation {
		return false, status.Error(codes.Aborted, "native-stop generation was superseded")
	}
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
	if closing {
		return false, nil
	}
	return false, r.failureLocked()
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
	if r.desired.Generation != generation || r.closing && r.desired.Mode != lifecycle.Closing {
		r.mu.Unlock()
		return
	}
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
	if r.fenceInFlight != generation {
		r.mu.Unlock()
		return status.Error(codes.Unavailable, "Execution fence is outside the current lifecycle")
	}
	r.mu.Unlock()
	select {
	case <-done:
	case <-ctx.Done():
		return ctx.Err()
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.fenceInFlight != generation || r.desired.Generation != generation {
		return status.Error(codes.Aborted, "Execution fence generation changed")
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
			r.mu.Lock()
			defer r.mu.Unlock()
			if r.desired.Generation != generation || input.Fence.LifecycleGeneration > generation {
				return false
			}
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
