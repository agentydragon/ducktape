package coordinator

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
)

// executionParent is the aggregate authority needed by the Execution lifecycle.
// Its methods acquire Runtime.mu themselves; never call them while holding
// executionLifecycle.mu. Runtime releases its mutex before calling the lifecycle.
// inputMu may precede Runtime.mu for completion publication; Runtime must not
// call into completion publication while holding Runtime.mu.
// Worker registration precedes launch. Runtime joins the engine before waiting
// for workers, so controllers cannot register new workers after that join begins.
type executionParent interface {
	hasExecutionOwner(string) bool
	executionStartAllowed(uid string, recovery bool, lossBinding uint64) bool
	startExecution(context.Context, string) (executionHandle, error)
	publishExecutionOwner(uid string, result executionStartResult, requested bool) bool
	revokeExecutionOwner(executionHandle, string)
	stopExecutionOwner(string) (executionHandle, uint64, error)
	clearExecutionOwner(string) error
	registerExecutionWorker() func()
	drainExecutionCalls(context.Context) error
	executionChanges() (changed, done <-chan struct{}, failure error)
	changed()
	fail(error)
}

var _ executionParent = (*Runtime)(nil)

func (r *Runtime) hasExecutionOwner(uid string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.executionOwner.executor(uid) != nil
}

func (r *Runtime) executionStartAllowed(uid string, recovery bool, lossBinding uint64) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.executionStartAllowedLocked(uid, recovery, lossBinding)
}

func (r *Runtime) executionStartAllowedLocked(uid string, recovery bool, lossBinding uint64) bool {
	return !r.closing && r.failureErr == nil && (!recovery || r.executionRecoveryAllowedLocked(uid, lossBinding))
}

func (r *Runtime) startExecution(ctx context.Context, uid string) (executionHandle, error) {
	return r.deps.startExecution(ctx, r.config, uid)
}

func (r *Runtime) publishExecutionOwner(uid string, result executionStartResult, requested bool) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	// Native startup and completion publication happen outside admission. Recheck
	// the caller epoch and recovery revision at the actual owner publication.
	admitted := requested && (result.epoch == nil || result.epoch.Err() == nil) &&
		r.executionStartAllowedLocked(uid, result.recovery, result.lossBinding)
	r.executionOwner = &executionLifetime{handle: result.handle, uid: uid, admitted: admitted}
	r.changedLocked()
	return admitted
}

func (r *Runtime) revokeExecutionOwner(handle executionHandle, uid string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if owner := r.executionOwner; owner != nil && (handle == nil || owner.handle == handle && owner.uid == uid) {
		owner.admitted = false
		r.changedLocked()
	}
}

func (r *Runtime) stopExecutionOwner(uid string) (executionHandle, uint64, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	owner := r.executionOwner
	if owner == nil || owner.uid != uid {
		return nil, 0, errors.New("embedded Execution has no matching owner to fence")
	}
	owner.admitted = false
	var binding uint64
	if r.executionRecoveryAllowedLocked(uid, 0) {
		binding = r.desired.Generation
	}
	r.changedLocked()
	return owner.handle, binding, nil
}

func (r *Runtime) clearExecutionOwner(uid string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.executionOwner == nil || r.executionOwner.uid != uid {
		return errors.New("fenced embedded Execution has no matching owner")
	}
	r.executionOwner = nil
	r.changedLocked()
	return nil
}

func (r *Runtime) registerExecutionWorker() func() {
	r.mu.Lock()
	r.lifecycleWorkers.Add(1)
	r.mu.Unlock()
	return r.lifecycleWorkers.Done
}

func (r *Runtime) drainExecutionCalls(ctx context.Context) error {
	r.cancelExecutionCalls()
	return r.waitForExecutionCalls(ctx)
}

func (r *Runtime) executionChanges() (changed, done <-chan struct{}, failure error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.changes, r.lifecycleDone, r.failureErr
}

// Execution already owns an admitted native lifetime while Session preparation
// keeps its admission gate closed. A durable seal revokes recovery immediately,
// including the interval before Quiescing desired state is published.
func (r *Runtime) executionRecoveryAllowedLocked(uid string, generation uint64) bool {
	return !r.closing && r.failureErr == nil && !r.state.Lameduck() && r.desired.Mode == lifecycle.Running && r.desired.IncarnationUID == uid && (generation == 0 || r.desired.Generation == generation)
}
