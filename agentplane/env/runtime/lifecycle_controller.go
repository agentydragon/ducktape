package runtime

import (
	"context"
	"errors"
	"fmt"

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
	stage      lifecycle.RuntimeCloseStage
	generation uint64
	plan       closePlan
	op         *closeOperation
	failure    string
}

type closePlan struct {
 generation uint64
 sealUID string
 sessionsStarted bool
 sessionLossErr error
 fenceCtx context.Context
 cancelFence context.CancelFunc
}

type closeOperation struct {
	done   chan struct{}
	cancel context.CancelFunc
	err    error
}

func (*runtimeLifecycleController) Name() string { return runtimeLifecycleControllerName }

func (*runtimeLifecycleController) Inputs() []controller.Input {
	return []controller.Input{
		{Namespace: lifecycle.Namespace, Type: lifecycle.EnvironmentDesiredStateType, Kind: controller.InputWeak},
		{Namespace: lifecycle.Namespace, Type: lifecycle.RuntimeTransitionRequestType, Kind: controller.InputWeak},
	}
}

func (*runtimeLifecycleController) Outputs() []controller.Output {
	return []controller.Output{
		{Type: lifecycle.RuntimeCloseStatusType, Kind: controller.OutputExclusive},
		{Type: lifecycle.RuntimeTransitionStatusType, Kind: controller.OutputExclusive},
	}
}

func (c *runtimeLifecycleController) Run(ctx context.Context, r controller.Runtime, _ *zap.Logger) error {
	c.transition.engineCtx = ctx
	defer func() {
		// A transition caller holds transitionMu until it sees terminal status.
		// Wake it before joining workers so a Close Prepare worker cannot remain
		// blocked behind that mutex when the engine exits unexpectedly.
		c.r.mu.Lock()
		if (c.op != nil || c.r.activeTransitionCtx != nil) && c.r.lifecycleControllerErr == nil {
			c.r.lifecycleControllerErr = errors.New("Runtime lifecycle coordinator exited before active work settled")
			c.r.changedLocked()
		}
		c.r.mu.Unlock()
		if c.plan.cancelFence != nil {
			c.plan.cancelFence()
		}
		if c.transition.cancel != nil {
			c.transition.cancel()
		}
		if c.op != nil {
			c.op.cancel()
		}
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
		case <-r.EventCh():
		case <-closeDone:
		case <-transitionDone:
		}
		if err := c.reconcile(ctx, r); err != nil {
			c.r.mu.Lock()
			c.r.lifecycleControllerErr = err
			c.r.changedLocked()
			c.r.mu.Unlock()
			c.r.fail(fmt.Errorf("reconcile Runtime Close: %w", err))
			return err
		}
		if err := c.transition.reconcile(ctx, r); err != nil {
			c.r.mu.Lock()
			c.r.lifecycleControllerErr = err
			c.r.changedLocked()
			c.r.mu.Unlock()
			c.r.fail(fmt.Errorf("reconcile Runtime transition: %w", err))
			return err
		}
		r.ResetRestartBackoff()
	}
}

func (c *runtimeLifecycleController) reconcile(ctx context.Context, r controller.Runtime) error {
	desired, found, err := lifecycle.ReadEnvironmentDesired(ctx, r)
	if err != nil || !found || desired.Mode != lifecycle.Closing {
		return err
	}
	if c.generation == 0 {
		c.generation = desired.Generation
		c.stage = lifecycle.RuntimeClosePreparing
	} else if c.generation != desired.Generation {
		return fmt.Errorf("Runtime Close generation changed from %d to %d", c.generation, desired.Generation)
	}
	if c.op != nil {
		select {
		case <-c.op.done:
			workErr := c.op.err
			c.op.cancel()
			c.op = nil
			if workErr != nil {
				c.stage = lifecycle.RuntimeCloseFailed
				c.failure = workErr.Error()
				if c.plan.cancelFence != nil {
					c.plan.cancelFence()
				}
				return c.publish(ctx, r)
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
	if err := c.publish(ctx, r); err != nil {
		return err
	}
	if c.stage == lifecycle.RuntimeCloseReadyToJoin || c.stage == lifecycle.RuntimeCloseFailed {
		if c.plan.cancelFence != nil {
			c.plan.cancelFence()
		}
		return nil
	}
	opCtx, cancel := context.WithCancel(context.Background())
	op := &closeOperation{done: make(chan struct{}), cancel: cancel}
	c.op = op
	stage := c.stage
	go func() {
		op.err = c.runStage(opCtx, stage)
		close(op.done)
	}()
	return nil
}

// A completed worker advances to the next shutdown barrier. Keep the sequence
// explicit so changing a status value cannot silently skip a fence or join.
func nextRuntimeCloseStage(stage lifecycle.RuntimeCloseStage) (lifecycle.RuntimeCloseStage, error) {
	switch stage {
	case lifecycle.RuntimeClosePreparing:
		return lifecycle.RuntimeCloseNativeStopping, nil
	case lifecycle.RuntimeCloseNativeStopping:
		return lifecycle.RuntimeCloseExecutionFencing, nil
	case lifecycle.RuntimeCloseExecutionFencing:
		return lifecycle.RuntimeCloseHostFlushing, nil
	case lifecycle.RuntimeCloseHostFlushing:
		return lifecycle.RuntimeCloseHostJoining, nil
	case lifecycle.RuntimeCloseHostJoining:
		return lifecycle.RuntimeCloseBootstrapSettling, nil
	case lifecycle.RuntimeCloseBootstrapSettling:
		return lifecycle.RuntimeCloseReadyToJoin, nil
	default:
		return 0, fmt.Errorf("unexpected completed Runtime Close stage %d", stage)
	}
}

func (c *runtimeLifecycleController) publish(ctx context.Context, r controller.Runtime) error {
	if err := lifecycle.WriteRuntimeCloseStatus(ctx, r, lifecycle.RuntimeCloseStatus{
		Generation: c.generation, Stage: c.stage, Error: c.failure,
	}); err != nil {
		return err
	}
	c.r.changed()
	return nil
}

func (c *runtimeLifecycleController) stopSessions(ctx context.Context) error {
	if err := c.r.stopSessions(ctx); err != nil {
		return err
	}
	return c.r.waitSessionsClosed(ctx)
}

func (c *runtimeLifecycleController) runStage(ctx context.Context, stage lifecycle.RuntimeCloseStage) error {
 p:=&c.plan
 switch stage {
 case lifecycle.RuntimeClosePreparing:return c.prepare()
 case lifecycle.RuntimeCloseNativeStopping:
  if p.sessionsStarted{return c.r.waitForNativeStopped(p.fenceCtx,p.generation)}
  return nil
 case lifecycle.RuntimeCloseExecutionFencing:return c.r.waitForExecutionFenced(p.fenceCtx,p.generation)
 case lifecycle.RuntimeCloseHostFlushing:
  if p.sealUID!=""&&p.sessionsStarted{return c.r.waitSessionStage(p.fenceCtx,lifecycle.Quiesced)}
  return nil
 case lifecycle.RuntimeCloseHostJoining:
  if err:=c.r.stopIngress(p.fenceCtx);err!=nil{return fmt.Errorf("join History delivery: %w",err)}
  if p.sessionsStarted{return c.stopSessions(p.fenceCtx)}
  return nil
 case lifecycle.RuntimeCloseBootstrapSettling:return c.r.waitBootstrapSettled(ctx)
 default:return fmt.Errorf("unexpected Runtime Close stage %d",stage)
 }
}

func (c *runtimeLifecycleController) prepare() error {
 r:=c.r
 r.transitionMu.Lock()
 defer r.transitionMu.Unlock()
 r.mu.Lock()
 defer r.mu.Unlock()
 if r.transitionUnsafe!=nil{return fmt.Errorf("prior Runtime transition has an uncertain Execution fence: %w",r.transitionUnsafe)}
 c.plan.generation=r.desired.Generation
 c.plan.sessionsStarted=r.sessions!=nil
 c.plan.sealUID=r.desired.SealUID
 c.plan.fenceCtx,c.plan.cancelFence=context.WithTimeout(context.Background(),executionFenceGrace)
 return nil
}
