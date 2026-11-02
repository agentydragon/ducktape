package runtime

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/controller"
	"go.uber.org/zap"
	"google.golang.org/protobuf/proto"
)

const runtimeCloseControllerName = "environment-runtime-close"

// The controller owns Runtime's Close and epoch transition sequences. Each
// blocking boundary runs in a worker; its single event loop stays free to
// observe either completion and gives Close reconciliation priority.
type runtimeCloseController struct {
	r          *Runtime
	transition runtimeTransitionSequence
	stage      lifecycle.RuntimeCloseStage
	generation uint64
	plan       closePlan
	op         *closeOperation
	failure    string
}

type closePlan struct {
	binding            *runtimepb.Binding
	sealUID            string
	connected          bool
	hostLifetimeExists bool
	executionMayExist  bool
	hostRunErr         error
	fenceCtx           context.Context
	cancelFence        context.CancelFunc
}

type closeOperation struct {
	done   chan struct{}
	cancel context.CancelFunc
	err    error
}

func (*runtimeCloseController) Name() string { return runtimeCloseControllerName }

func (*runtimeCloseController) Inputs() []controller.Input {
	return []controller.Input{
		{Namespace: lifecycle.Namespace, Type: lifecycle.EnvironmentDesiredStateType, Kind: controller.InputWeak},
		{Namespace: lifecycle.Namespace, Type: lifecycle.RuntimeTransitionRequestType, Kind: controller.InputWeak},
	}
}

func (*runtimeCloseController) Outputs() []controller.Output {
	return []controller.Output{
		{Type: lifecycle.RuntimeCloseStatusType, Kind: controller.OutputExclusive},
		{Type: lifecycle.RuntimeTransitionStatusType, Kind: controller.OutputExclusive},
	}
}

func (c *runtimeCloseController) Run(ctx context.Context, r controller.Runtime, _ *zap.Logger) error {
	c.transition.engineCtx = ctx
	defer func() {
		// A transition caller holds transitionMu until it sees terminal status.
		// Wake it before joining workers so a Close Prepare worker cannot remain
		// blocked behind that mutex when the engine exits unexpectedly.
		c.r.mu.Lock()
		if (c.op != nil || c.r.activeTransitionCtx != nil) && c.r.closeControllerErr == nil {
			c.r.closeControllerErr = errors.New("Runtime lifecycle coordinator exited before active work settled")
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
			c.r.closeControllerErr = err
			c.r.changedLocked()
			c.r.mu.Unlock()
			c.r.fail(fmt.Errorf("reconcile Runtime Close: %w", err))
			return err
		}
		if err := c.transition.reconcile(ctx, r); err != nil {
			c.r.mu.Lock()
			c.r.closeControllerErr = err
			c.r.changedLocked()
			c.r.mu.Unlock()
			c.r.fail(fmt.Errorf("reconcile Runtime transition: %w", err))
			return err
		}
		r.ResetRestartBackoff()
	}
}

func (c *runtimeCloseController) reconcile(ctx context.Context, r controller.Runtime) error {
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
			c.stage++
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

func (c *runtimeCloseController) publish(ctx context.Context, r controller.Runtime) error {
	if err := lifecycle.WriteRuntimeCloseStatus(ctx, r, lifecycle.RuntimeCloseStatus{
		Generation: c.generation, Stage: c.stage, Error: c.failure,
	}); err != nil {
		return err
	}
	c.r.changed()
	return nil
}

func (c *runtimeCloseController) runStage(ctx context.Context, stage lifecycle.RuntimeCloseStage) error {
	p := &c.plan
	switch stage {
	case lifecycle.RuntimeClosePreparing:
		return c.prepare()
	case lifecycle.RuntimeCloseNativeStopping:
		if p.connected {
			return c.r.waitForHostNativeStopped(p.fenceCtx, p.binding)
		}
		if p.hostLifetimeExists {
			c.r.mu.Lock()
			host := c.r.hostLifetime
			c.r.mu.Unlock()
			if err := host.Stop(p.fenceCtx); err != nil {
				return fmt.Errorf("Runtime lost its Host lifecycle connection and native cleanup is unproved: %w", err)
			}
			if err := host.WaitClosed(p.fenceCtx); err != nil {
				return fmt.Errorf("Runtime lost its Host lifecycle connection and native cleanup is unproved: %w", err)
			}
			p.hostRunErr = errors.New("Harness Host lifecycle connection was lost")
			return nil
		}
		if p.executionMayExist {
			return errors.New("Runtime lost its Host lifecycle connection before proving native cleanup")
		}
		return nil
	case lifecycle.RuntimeCloseExecutionFencing:
		if p.connected {
			c.r.maybeFenceExecution(p.binding)
			return c.r.waitForExecutionFenced(p.fenceCtx, p.binding)
		}
		if p.hostLifetimeExists {
			if err := c.r.stopExecution(p.fenceCtx); err != nil {
				return fmt.Errorf("fence Execution after Host lifecycle loss: %w", err)
			}
		}
		return nil
	case lifecycle.RuntimeCloseHostFlushing:
		if p.connected && p.sealUID != "" {
			return c.r.waitForHostQuiesced(p.fenceCtx, p.binding)
		}
		return nil
	case lifecycle.RuntimeCloseHostJoining:
		if p.connected {
			if err := c.r.stopHost(p.fenceCtx); err != nil {
				return fmt.Errorf("stop and fence Harness Host: %w", err)
			}
		}
		c.r.mu.Lock()
		if c.r.host != nil {
			c.r.host.connected = false
		}
		c.r.mu.Unlock()
		return nil
	case lifecycle.RuntimeCloseBootstrapSettling:
		return c.r.waitBootstrapSettled(ctx)
	default:
		return fmt.Errorf("unexpected Runtime Close stage %d", stage)
	}
}

func (c *runtimeCloseController) prepare() error {
	r := c.r
	r.transitionMu.Lock()
	defer r.transitionMu.Unlock()
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.transitionUnsafe != nil {
		return fmt.Errorf("prior Runtime transition has an uncertain Execution fence: %w", r.transitionUnsafe)
	}
	snapshot := r.state.Snapshot()
	sealUID := ""
	if snapshot.Quiesced {
		sealUID = snapshot.IncarnationUID
	}
	if sealedUID, sealed, err := r.state.SealedUID(); err != nil {
		return fmt.Errorf("read environment seal during shutdown: %w", err)
	} else if sealed {
		sealUID = sealedUID
	}
	incarnationUID := snapshot.IncarnationUID
	if r.intent != nil && incarnationUID == "" {
		incarnationUID = r.intent.IncarnationUid
	}
	if sealUID != "" {
		incarnationUID = sealUID
	}
	p := &c.plan
	p.sealUID = sealUID
	p.connected = r.host != nil && r.host.connected && !r.hostExited
	p.executionMayExist = r.config.CodexExecServer != nil || r.executionOwner != nil
	p.hostLifetimeExists = r.hostLifetime != nil
	if p.connected {
		mode := runtimepb.LifecycleIntent_CLOSING
		if sealUID != "" {
			mode = runtimepb.LifecycleIntent_QUIESCING
		}
		if r.intent != nil && ((sealUID != "" && r.intent.Mode == runtimepb.LifecycleIntent_QUIESCING && r.intent.SealUid == sealUID) ||
			(sealUID == "" && r.intent.Mode == runtimepb.LifecycleIntent_CLOSING && r.intent.IncarnationUid == incarnationUID)) {
			p.binding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
		} else {
			p.binding = proto.Clone(r.setIntentLocked(mode, incarnationUID, sealUID, false).Binding).(*runtimepb.Binding)
		}
	}
	p.fenceCtx, p.cancelFence = context.WithTimeout(context.Background(), executionFenceGrace)
	return nil
}
