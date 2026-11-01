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

// The controller owns the Close sequence. Each blocking boundary runs in one
// worker; the COSI event loop only publishes the next barrier after that worker
// has completed. A failed worker is retained as a terminal status, never retried.
type runtimeCloseController struct {
	r          *Runtime
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
	return []controller.Input{{Namespace: lifecycle.Namespace, Type: lifecycle.DesiredStateType, Kind: controller.InputWeak}}
}

func (*runtimeCloseController) Outputs() []controller.Output {
	return []controller.Output{{Type: lifecycle.RuntimeCloseStatusType, Kind: controller.OutputExclusive}}
}

func (c *runtimeCloseController) Run(ctx context.Context, r controller.Runtime, _ *zap.Logger) error {
	defer func() {
		if c.plan.cancelFence != nil {
			c.plan.cancelFence()
		}
		if c.op != nil {
			c.op.cancel()
			<-c.op.done
		}
	}()
	for {
		var done <-chan struct{}
		if c.op != nil {
			done = c.op.done
		}
		select {
		case <-ctx.Done():
			return nil
		case <-r.EventCh():
		case <-done:
		}
		if err := c.reconcile(ctx, r); err != nil {
			c.r.mu.Lock()
			c.r.closeControllerErr = err
			c.r.changedLocked()
			c.r.mu.Unlock()
			c.r.fail(fmt.Errorf("reconcile Runtime Close: %w", err))
			return err
		}
		r.ResetRestartBackoff()
	}
}

func (c *runtimeCloseController) reconcile(ctx context.Context, r controller.Runtime) error {
	desired, found, err := lifecycle.ReadDesired(ctx, r)
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
			_, hostRunErr := host.Wait(p.fenceCtx)
			p.hostRunErr = errors.Join(errors.New("Harness Host lifecycle connection was lost"), hostRunErr)
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
			c.r.cancelExecutionCalls()
			if err := c.r.waitForExecutionCalls(p.fenceCtx); err != nil {
				return fmt.Errorf("drain Execution calls after Host lifecycle loss: %w", err)
			}
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
	r.mu.Lock()
	defer r.mu.Unlock()
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
		if sealUID != "" && r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_QUIESCING && r.intent.SealUid == sealUID {
			p.binding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
		} else {
			p.binding = proto.Clone(r.setIntentLocked(mode, incarnationUID, sealUID, false).Binding).(*runtimepb.Binding)
		}
	}
	p.fenceCtx, p.cancelFence = context.WithTimeout(context.Background(), executionFenceGrace)
	return nil
}
