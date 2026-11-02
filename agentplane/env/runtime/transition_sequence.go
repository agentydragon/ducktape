package runtime

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/controller"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// A transition is an ordered sequence of effect barriers. The Runtime lifecycle
// controller owns the sequence and its status; each blocking barrier has one worker.
// A completed or uncertain barrier is never replayed by controller restart.
type runtimeTransitionSequence struct {
	r         *Runtime
	sequence  uint64
	request   lifecycle.RuntimeTransitionRequest
	adoption  adoptionPlan
	quiesce   quiescePlan
	steps     []runtimeTransitionStep
	index     int
	worker    *runtimeTransitionWorker
	ctx       context.Context
	engineCtx context.Context
	cancel    context.CancelFunc
	terminal  bool
}

type runtimeTransitionStep struct {
	stage    lifecycle.RuntimeTransitionStage
	adoption adoptionBarrier
	quiesce  quiesceBarrierKind
}

type runtimeTransitionWorker struct {
	done    chan struct{}
	step    runtimeTransitionStep
	outcome runtimeTransitionOutcome
	err     error
}

type runtimeTransitionOutcome struct {
	steps        []runtimeTransitionStep
	binding      *runtimepb.Binding
	adoption     adoptionOutcome
	adoptionPlan *adoptionPlan
	quiesce      *quiescePlan
}

func (c *runtimeTransitionSequence) reconcile(ctx context.Context, engine controller.Runtime) error {
	request, found, err := lifecycle.ReadRuntimeTransitionRequest(ctx, engine)
	if err != nil || !found {
		return err
	}
	if c.sequence != request.Sequence {
		if c.worker != nil || c.sequence != 0 && !c.terminal {
			return fmt.Errorf("Runtime transition %d superseded before settlement", c.sequence)
		}
		c.sequence, c.request, c.terminal = request.Sequence, request, false
		c.steps, c.index, c.adoption, c.quiesce = nil, -1, adoptionPlan{}, quiescePlan{}
		c.r.mu.Lock()
		transitionCtx := c.r.activeTransitionCtx
		activeSequence := c.r.activeTransitionSequence
		c.r.mu.Unlock()
		if activeSequence != request.Sequence || transitionCtx == nil {
			return fmt.Errorf("Runtime transition %d has no admitted caller", request.Sequence)
		}
		c.ctx, c.cancel = context.WithCancel(transitionCtx)
		return c.start(ctx, engine, runtimeTransitionStep{stage: lifecycle.RuntimeTransitionPlanning})
	}
	if c.worker == nil || c.terminal {
		return nil
	}
	select {
	case <-c.worker.done:
	default:
		return nil
	}
	completed := c.worker
	c.worker = nil
	if completed.err != nil {
		c.terminal = true
		c.cancel()
		code := status.Code(completed.err)
		if errors.Is(completed.err, context.Canceled) {
			code = codes.Canceled
		} else if errors.Is(completed.err, context.DeadlineExceeded) {
			code = codes.DeadlineExceeded
		}
		return c.publish(ctx, engine, lifecycle.RuntimeTransitionFailed, completed.err.Error(), code)
	}
	if err := c.recordOutcome(completed.step, completed.outcome); err != nil {
		c.terminal = true
		c.cancel()
		return c.publish(ctx, engine, lifecycle.RuntimeTransitionFailed, err.Error(), codes.Internal)
	}
	c.index++
	if c.index >= len(c.steps) {
		c.terminal = true
		c.cancel()
		return c.publish(ctx, engine, lifecycle.RuntimeTransitionDone, "", codes.OK)
	}
	return c.start(ctx, engine, c.steps[c.index])
}

func (c *runtimeTransitionSequence) start(ctx context.Context, engine controller.Runtime, step runtimeTransitionStep) error {
	if err := c.publish(ctx, engine, step.stage, "", codes.OK); err != nil {
		return err
	}
	worker := &runtimeTransitionWorker{done: make(chan struct{}), step: step}
	c.worker = worker
	go func() {
		worker.outcome, worker.err = c.runStep(step)
		close(worker.done)
	}()
	return nil
}

func (c *runtimeTransitionSequence) runStep(step runtimeTransitionStep) (runtimeTransitionOutcome, error) {
	if err := c.ctx.Err(); err != nil {
		return runtimeTransitionOutcome{}, err
	}
	var outcome runtimeTransitionOutcome
	var err error
	if step.stage == lifecycle.RuntimeTransitionPlanning {
		outcome, err = c.planTransition(c.ctx)
	} else if step.adoption.kind != 0 {
		outcome, err = c.runAdoptionBarrier(c.ctx, step.adoption)
	} else if step.quiesce != 0 {
		outcome, err = c.runQuiesceBarrier(c.ctx, step.quiesce)
	} else {
		err = fmt.Errorf("Runtime transition stage %d has no barrier", step.stage)
	}
	if err != nil && step.stage == lifecycle.RuntimeTransitionExecutionFencing {
		if errors.Is(err, context.Canceled) && !c.r.isClosing() && step.adoption.kind == adoptionExecutionFence {
			fenceCtx, cancel := context.WithTimeout(c.engineCtx, executionFenceGrace)
			defer cancel()
			if settleErr := c.r.waitForExecutionFenced(fenceCtx, c.binding(step.adoption.binding)); settleErr == nil {
				return outcome, err
			} else {
				err = errors.Join(err, fmt.Errorf("settle exact Execution fence after epoch loss: %w", settleErr))
			}
		}
		if !c.r.isClosing() {
			c.r.mu.Lock()
			c.r.transitionUnsafe = err
			c.r.mu.Unlock()
		}
		return outcome, err
	}
	if err == nil || step.adoption.kind != adoptionExecutionStart {
		return outcome, err
	}
	// startExecutionForUID can return on epoch cancellation while its COSI
	// startup is still in flight. The public stop barrier settles that start
	// even if no executionOwner handle has been published yet.
	fenceCtx, cancel := context.WithTimeout(c.engineCtx, executionFenceGrace)
	defer cancel()
	if fenceErr := c.r.stopExecution(fenceCtx); fenceErr != nil {
		unsafe := errors.Join(err, fmt.Errorf("fence interrupted Execution start: %w", fenceErr))
		c.r.mu.Lock()
		c.r.transitionUnsafe = unsafe
		c.r.mu.Unlock()
		return outcome, unsafe
	}
	return outcome, err
}

func (c *runtimeTransitionSequence) planTransition(ctx context.Context) (runtimeTransitionOutcome, error) {
	switch c.request.Kind {
	case lifecycle.RuntimeAdopt:
		plan, steps, err := c.planAdoption(ctx, c.request.UID)
		return runtimeTransitionOutcome{steps: steps, adoptionPlan: &plan}, err
	case lifecycle.RuntimeQuiesce:
		plan, steps, err := c.planQuiesce(c.request)
		return runtimeTransitionOutcome{steps: steps, quiesce: &plan}, err
	default:
		return runtimeTransitionOutcome{}, fmt.Errorf("unknown Runtime transition kind %d", c.request.Kind)
	}
}

func (c *runtimeTransitionSequence) recordOutcome(step runtimeTransitionStep, outcome runtimeTransitionOutcome) error {
	if step.stage == lifecycle.RuntimeTransitionPlanning {
		c.steps = outcome.steps
		if outcome.adoptionPlan != nil {
			c.adoption = *outcome.adoptionPlan
		}
	}
	if outcome.quiesce != nil {
		c.quiesce = *outcome.quiesce
	}
	return c.recordAdoptionOutcome(step, outcome)
}

func (c *runtimeTransitionSequence) publish(ctx context.Context, engine controller.Runtime, stage lifecycle.RuntimeTransitionStage, failure string, errorCode codes.Code) error {
	if err := lifecycle.WriteRuntimeTransitionStatus(ctx, engine, lifecycle.RuntimeTransitionStatus{
		Sequence: c.sequence, Stage: stage, Error: failure, ErrorCode: uint32(errorCode),
	}); err != nil {
		return err
	}
	c.r.changed()
	return nil
}
