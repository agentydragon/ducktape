package coordinator

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"go.uber.org/zap"
)

const runtimeLifecycleControllerName = "environment-runtime-lifecycle"

var errSessionComponentLost = errors.New("Session component became unavailable before shutdown")

// The controller owns only Runtime's Close sequence. Transition requests have
// a separate Go owner so a failed COSI engine cannot wait for their workers.
type runtimeCloseController struct {
	r          *Runtime
	stage      runtimeCloseStage
	generation uint64
	plan       closePlan
	op         *closeOperation
	stopped    chan struct{}
}

type closePlan struct {
	sealUID          string
	sessionsStarted  bool
	sessionLossErr   error
	nativeCleanupErr error
	fenceCtx         context.Context
	cancelFence      context.CancelFunc
}

type closeOperation struct {
	done chan struct{}
	next runtimeCloseStage
	err  error
}

func (*runtimeCloseController) Name() string { return runtimeLifecycleControllerName }

func (*runtimeCloseController) Inputs() []controller.Input {
	return []controller.Input{
		{Namespace: lifecycle.Namespace, Type: lifecycle.EnvironmentDesiredStateType, Kind: controller.InputWeak},
		{Namespace: lifecycle.Namespace, Type: bootstrap.RequestType, Kind: controller.InputWeak},
		{Namespace: lifecycle.Namespace, Type: bootstrap.StatusType, Kind: controller.InputWeak},
		{Namespace: lifecycle.Namespace, Type: lifecycle.AggregateStatusType, Kind: controller.InputWeak},
	}
}

func (*runtimeCloseController) Outputs() []controller.Output {
	return []controller.Output{{Type: runtimeCloseStatusType, Kind: controller.OutputExclusive}}
}

func (c *runtimeCloseController) Run(ctx context.Context, r controller.Runtime, _ *zap.Logger) error {
	select {
	case <-c.stopped:
		return nil
	default:
	}
	// Close stages share this Run lifetime, independent of the Close caller.
	// Cancel on any controller exit, including reconciliation failure.
	closeCtx, cancelClose := context.WithCancel(context.Background())
	defer func() {
		// This controller's first exit ends Runtime's lifecycle ownership.
		// Cancel the Go operation owner here, without relying on Run's fatal
		// loop or waiting for COSI's engine to finish joining controllers.
		c.r.lifecycleCancel()
		close(c.stopped)
		// Preparation may still be installing the fence context. Cancel its
		// parent before joining, then read the plan after the worker settles.
		cancelClose()
		if c.op != nil {
			<-c.op.done
		}
		if c.plan.cancelFence != nil {
			c.plan.cancelFence()
		}
	}()
	for {
		var closeDone, nativeDeadline <-chan struct{}
		if c.op != nil {
			closeDone = c.op.done
		}
		if c.stage == runtimeCloseNativeStopping && c.op == nil {
			nativeDeadline = c.plan.fenceCtx.Done()
		}
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		case <-closeDone:
		case <-nativeDeadline:
		}
		if err := c.reconcile(ctx, closeCtx, r); err != nil {
			c.r.mu.Lock()
			c.r.lifecycleControllerErr = err
			c.r.changedLocked()
			c.r.mu.Unlock()
			c.r.fail(fmt.Errorf("reconcile Runtime Close: %w", err))
			return err
		}
		r.ResetRestartBackoff()
	}
}

func (c *runtimeCloseController) reconcile(ctx, closeCtx context.Context, r controller.Runtime) error {
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
	if c.stage == runtimeCloseReadyToJoin || c.stage == runtimeCloseFailed {
		return nil
	}
	if c.op != nil {
		select {
		case <-c.op.done:
			completed := c.op
			c.op = nil
			if completed.err != nil {
				c.stage = runtimeCloseFailed
				if c.plan.cancelFence != nil {
					c.plan.cancelFence()
				}
				return c.publish(ctx, r, completed.err)
			}
			c.stage = completed.next
		default:
			return nil
		}
	}
	if c.stage == runtimeCloseNativeStopping {
		settled, nativeErr := c.nativeStopSettled(ctx, r)
		if settled {
			if err := ctx.Err(); err != nil {
				return err
			}
			c.plan.nativeCleanupErr = nativeErr
			c.stage = runtimeCloseExecutionFencing
		}
	}
	if c.stage == runtimeCloseBootstrapSettling {
		settled, err := c.bootstrapSettled(ctx, r)
		if err != nil {
			c.stage = runtimeCloseFailed
			if c.plan.cancelFence != nil {
				c.plan.cancelFence()
			}
			return c.publish(ctx, r, err)
		}
		if settled {
			if err := ctx.Err(); err != nil {
				return err
			}
			c.stage = runtimeCloseReadyToJoin
		}
	}
	if err := c.publish(ctx, r, nil); err != nil {
		return err
	}
	if c.stage == runtimeCloseReadyToJoin || c.stage == runtimeCloseFailed {
		if c.plan.cancelFence != nil {
			c.plan.cancelFence()
		}
		return nil
	}
	if c.stage == runtimeCloseNativeStopping || c.stage == runtimeCloseBootstrapSettling {
		return nil
	}
	op := &closeOperation{done: make(chan struct{})}
	c.op = op
	stage := c.stage
	go func() {
		op.next, op.err = c.runStage(closeCtx, stage)
		close(op.done)
	}()
	return nil
}

// Bootstrap status is the host's cleanup outcome. Both inputs are subscribed
// before Run starts, so a change concurrent with these reads wakes reconciliation.
func (c *runtimeCloseController) bootstrapSettled(ctx context.Context, r controller.Runtime) (bool, error) {
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

// Observe the exact-generation COSI proof before the shared fence deadline.
// Native cleanup errors are saved so Execution fencing still runs first.
func (c *runtimeCloseController) nativeStopSettled(ctx context.Context, reader controller.Reader) (bool, error) {
	if !c.plan.sessionsStarted {
		return true, nil
	}
	settled, err := c.r.observeNativeStop(ctx, reader, c.generation, true)
	if settled || err != nil {
		return true, err
	}
	if err := c.plan.fenceCtx.Err(); err != nil {
		return true, err
	}
	return false, nil
}

func (c *runtimeCloseController) publish(ctx context.Context, writer controller.Writer, failure error) error {
	return writeRuntimeCloseStatus(ctx, writer, RuntimeCloseStatus{Generation: c.generation, Stage: c.stage, Failure: failure})
}

func (c *runtimeCloseController) stopSessions(ctx context.Context) error {
	if err := c.r.stopSessions(ctx); err != nil {
		return err
	}
	return c.r.waitSessionsClosed(ctx)
}

// The worker selects the next barrier. Reconciliation advances only after
// the worker finishes successfully.
func (c *runtimeCloseController) runStage(ctx context.Context, stage runtimeCloseStage) (runtimeCloseStage, error) {
	p := &c.plan
	switch stage {
	case runtimeClosePreparing:
		return runtimeCloseNativeStopping, c.prepare(ctx)
	case runtimeCloseExecutionFencing:
		return runtimeCloseHistoryFlushing, c.r.waitForExecutionFenced(p.fenceCtx, c.generation)
	case runtimeCloseHistoryFlushing:
		var err error
		if p.nativeCleanupErr != nil {
			err = errors.Join(p.nativeCleanupErr, p.sessionLossErr)
		} else if p.sealUID != "" && p.sessionsStarted {
			err = c.r.waitSessionStage(p.fenceCtx, lifecycle.Quiesced)
		}
		return runtimeCloseSessionsJoining, err
	case runtimeCloseSessionsJoining:
		if err := c.r.stopIngress(p.fenceCtx); err != nil {
			return 0, fmt.Errorf("join History delivery: %w", err)
		}
		var err error
		if p.sessionsStarted {
			err = c.stopSessions(p.fenceCtx)
		}
		return runtimeCloseBootstrapSettling, err
	default:
		return 0, fmt.Errorf("unexpected Runtime Close stage %d", stage)
	}
}

func (c *runtimeCloseController) prepare(ctx context.Context) error {
	r := c.r
	// Close published its generation under transitionMu after the accepted
	// transition settled. No new transition can start after closing was set.
	// Avoid taking transitionMu here: engine failure must be able to join this
	// controller without waiting for a Go operation worker to settle.
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.transitionUnsafe != nil {
		return fmt.Errorf("prior Runtime transition has an uncertain Execution fence: %w", r.transitionUnsafe)
	}
	c.plan.sessionsStarted = r.sessions != nil
	c.plan.sessionLossErr = r.failureLocked()
	c.plan.sealUID = r.desired.SealUID
	c.plan.fenceCtx, c.plan.cancelFence = context.WithTimeout(ctx, executionFenceGrace)
	return nil
}
