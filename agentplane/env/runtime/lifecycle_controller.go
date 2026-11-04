package runtime

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"go.uber.org/zap"
)

const runtimeLifecycleControllerName = "environment-runtime-lifecycle"

var errSessionComponentLost = errors.New("Session component became unavailable before shutdown")

// The controller owns Runtime's Close and epoch transition sequences. Each
// blocking boundary runs in a worker; its single event loop stays free to
// observe either completion and gives Close reconciliation priority.
type runtimeLifecycleController struct {
	r          *Runtime
	transition runtimeTransitionSequence
	stage      runtimeCloseStage
	generation uint64
	plan       closePlan
	op         *closeOperation
	failure    error
	requests   chan *runtimeTransitionRequest
	closeDone  chan struct{}
	stopped    chan struct{}
	result     runtimeCloseResult
}

type closePlan struct {
	generation       uint64
	sealUID          string
	sessionsStarted  bool
	sessionLossErr   error
	nativeCleanupErr error
	fenceCtx         context.Context
	cancelFence      context.CancelFunc
}

type closeOperation struct {
	done chan struct{}
	err  error
}

func (*runtimeLifecycleController) Name() string { return runtimeLifecycleControllerName }

func (*runtimeLifecycleController) Inputs() []controller.Input {
	return []controller.Input{
		{Namespace: lifecycle.Namespace, Type: lifecycle.EnvironmentDesiredStateType, Kind: controller.InputWeak},
		{Namespace: lifecycle.Namespace, Type: bootstrap.RequestType, Kind: controller.InputWeak},
		{Namespace: lifecycle.Namespace, Type: bootstrap.StatusType, Kind: controller.InputWeak},
	}
}

func (*runtimeLifecycleController) Outputs() []controller.Output { return nil }

func (c *runtimeLifecycleController) Run(ctx context.Context, r controller.Runtime, _ *zap.Logger) error {
	select {
	case <-c.stopped:
		return nil
	default:
	}
	c.transition.engineCtx = ctx
	// Close stages share this Run lifetime, independent of the Close caller.
	// Cancel on any controller exit, including reconciliation failure.
	closeCtx, cancelClose := context.WithCancel(context.Background())
	defer func() {
		// A transition caller holds transitionMu until its accepted work settles.
		// Wake it before joining workers so a Close Prepare worker cannot remain
		// blocked behind that mutex when the engine exits unexpectedly.
		c.r.mu.Lock()
		if (c.op != nil || c.r.activeTransitionCancel != nil) && c.r.lifecycleControllerErr == nil {
			c.r.lifecycleControllerErr = errors.New("Runtime lifecycle coordinator exited before active work settled")
			c.r.changedLocked()
		}
		c.r.mu.Unlock()
		close(c.stopped)
		if c.plan.cancelFence != nil {
			c.plan.cancelFence()
		}
		if c.transition.cancel != nil {
			c.transition.cancel()
		}
		cancelClose()
		if c.transition.worker != nil {
			<-c.transition.worker.done
		}
		if c.op != nil {
			<-c.op.done
		}
	}()
	for {
		var closeDone, transitionDone <-chan struct{}
		if c.op != nil {
			closeDone = c.op.done
		}
		if c.transition.worker != nil {
			transitionDone = c.transition.worker.done
		}
		select {
		case <-ctx.Done():
			return nil
		case request := <-c.requests:
			if err := c.transition.begin(request); err != nil {
				return err
			}
		case <-r.EventCh():
		case <-closeDone:
		case <-transitionDone:
		}
		if err := c.reconcile(ctx, closeCtx, r); err != nil {
			c.r.mu.Lock()
			c.r.lifecycleControllerErr = err
			c.r.changedLocked()
			c.r.mu.Unlock()
			c.r.fail(fmt.Errorf("reconcile Runtime Close: %w", err))
			return err
		}
		c.transition.reconcile()
		r.ResetRestartBackoff()
	}
}

func (c *runtimeLifecycleController) reconcile(ctx, closeCtx context.Context, r controller.Runtime) error {
	desired, found, err := lifecycle.ReadEnvironmentDesired(ctx, r)
	if err != nil || !found || desired.Mode != lifecycle.Closing {
		return err
	}
	if c.generation == 0 {
		c.generation = desired.Generation
		c.stage = runtimeClosePreparing
	} else if c.generation != desired.Generation {
		return fmt.Errorf("Runtime Close generation changed from %d to %d", c.generation, desired.Generation)
	}
	if c.op != nil {
		select {
		case <-c.op.done:
			workErr := c.op.err
			c.op = nil
			if workErr != nil {
				c.stage = runtimeCloseFailed
				c.failure = workErr
				if c.plan.cancelFence != nil {
					c.plan.cancelFence()
				}
				c.publish()
				return nil
			}
			next, err := nextRuntimeCloseStage(c.stage)
			if err != nil {
				return err
			}
			c.stage = next
		default:
			return nil
		}
	}
	if c.stage == runtimeCloseBootstrapSettling {
		settled, err := c.bootstrapSettled(ctx, r)
		if err != nil {
			c.stage = runtimeCloseFailed
			c.failure = err
			if c.plan.cancelFence != nil {
				c.plan.cancelFence()
			}
			c.publish()
			return nil
		}
		if settled {
			if err := ctx.Err(); err != nil {
				return err
			}
			c.stage = runtimeCloseReadyToJoin
		}
	}
	c.publish()
	if c.stage == runtimeCloseReadyToJoin || c.stage == runtimeCloseFailed {
		if c.plan.cancelFence != nil {
			c.plan.cancelFence()
		}
		return nil
	}
	if c.stage == runtimeCloseBootstrapSettling {
		return nil
	}
	op := &closeOperation{done: make(chan struct{})}
	c.op = op
	stage := c.stage
	go func() {
		op.err = c.runStage(closeCtx, stage)
		close(op.done)
	}()
	return nil
}

// Bootstrap status is the host's cleanup outcome. Both inputs are subscribed
// before Run starts, so a change concurrent with these reads wakes reconciliation.
func (c *runtimeLifecycleController) bootstrapSettled(ctx context.Context, r controller.Runtime) (bool, error) {
	if c.r.bootstrapHost == nil || !c.r.bootstrapHost.Configured() {
		return true, nil
	}
	request, found, err := bootstrap.ReadRequest(ctx, r)
	if err != nil {
		return false, fmt.Errorf("read accepted bootstrap state during shutdown: %w", err)
	}
	if !found || !request.Requested {
		return true, nil
	}
	status, found, err := bootstrap.ReadStatus(ctx, r)
	if err != nil {
		return false, fmt.Errorf("observe accepted bootstrap cleanup: %w", err)
	}
	return found && status.Phase.IsTerminal(), nil
}

// A completed worker advances to the next shutdown barrier. Keep the sequence
// explicit so changing a status value cannot silently skip a fence or join.
func nextRuntimeCloseStage(stage runtimeCloseStage) (runtimeCloseStage, error) {
	switch stage {
	case runtimeClosePreparing:
		return runtimeCloseNativeStopping, nil
	case runtimeCloseNativeStopping:
		return runtimeCloseExecutionFencing, nil
	case runtimeCloseExecutionFencing:
		return runtimeCloseHistoryFlushing, nil
	case runtimeCloseHistoryFlushing:
		return runtimeCloseSessionsJoining, nil
	case runtimeCloseSessionsJoining:
		return runtimeCloseBootstrapSettling, nil
	default:
		return 0, fmt.Errorf("unexpected completed Runtime Close stage %d", stage)
	}
}

func (c *runtimeLifecycleController) publish() {
	c.r.mu.Lock()
	c.result = runtimeCloseResult{generation: c.generation, stage: c.stage, err: c.failure}
	c.r.changedLocked()
	c.r.mu.Unlock()
	if c.stage == runtimeCloseReadyToJoin || c.stage == runtimeCloseFailed {
		select {
		case <-c.closeDone:
		default:
			close(c.closeDone)
		}
	}
}

func (c *runtimeLifecycleController) closeProgress() runtimeCloseResult {
	c.r.mu.Lock()
	defer c.r.mu.Unlock()
	return c.result
}

func (c *runtimeLifecycleController) stopSessions(ctx context.Context) error {
	if err := c.r.stopSessions(ctx); err != nil {
		return err
	}
	return c.r.waitSessionsClosed(ctx)
}

func (c *runtimeLifecycleController) runStage(ctx context.Context, stage runtimeCloseStage) error {
	p := &c.plan
	switch stage {
	case runtimeClosePreparing:
		return c.prepare()
	case runtimeCloseNativeStopping:
		if p.sessionsStarted {
			p.nativeCleanupErr = c.r.waitForNativeStop(p.fenceCtx, p.generation, true)
			return nil
		}
		return nil
	case runtimeCloseExecutionFencing:
		return c.r.waitForExecutionFenced(p.fenceCtx, p.generation)
	case runtimeCloseHistoryFlushing:
		if p.nativeCleanupErr != nil {
			return errors.Join(p.nativeCleanupErr, p.sessionLossErr)
		}
		if p.sealUID != "" && p.sessionsStarted {
			return c.r.waitSessionStage(p.fenceCtx, lifecycle.Quiesced)
		}
		return nil
	case runtimeCloseSessionsJoining:
		if err := c.r.stopIngress(p.fenceCtx); err != nil {
			return fmt.Errorf("join History delivery: %w", err)
		}
		if p.sessionsStarted {
			return c.stopSessions(p.fenceCtx)
		}
		return nil
	default:
		return fmt.Errorf("unexpected Runtime Close stage %d", stage)
	}
}

func (c *runtimeLifecycleController) prepare() error {
	r := c.r
	r.transitionMu.Lock()
	defer r.transitionMu.Unlock()
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.transitionUnsafe != nil {
		return fmt.Errorf("prior Runtime transition has an uncertain Execution fence: %w", r.transitionUnsafe)
	}
	c.plan.generation = r.desired.Generation
	c.plan.sessionsStarted = r.sessions != nil
	c.plan.sessionLossErr = r.failureLocked()
	c.plan.sealUID = r.desired.SealUID
	c.plan.fenceCtx, c.plan.cancelFence = context.WithTimeout(context.Background(), executionFenceGrace)
	return nil
}
