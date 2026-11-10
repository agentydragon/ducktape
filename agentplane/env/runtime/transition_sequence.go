package runtime

import (
	"context"
	"errors"
	"fmt"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// A transition is an ordered sequence of effect barriers. The Runtime lifecycle
// coordinator owns the sequence; each blocking barrier has one worker.
// A completed or uncertain barrier is never replayed by controller restart.
type runtimeTransitionSequence struct {
	r         *Runtime
	request   *runtimeTransitionRequest
	adoption  adoptionPlan
	quiesce   quiescePlan
	steps     []runtimeTransitionStep
	index     int
	worker    *runtimeTransitionWorker
	ctx       context.Context
	engineCtx context.Context
	cancel    context.CancelFunc
}

type runtimeTransitionStep struct {
	kind     runtimeTransitionStepKind
	adoption adoptionBarrier
}

type runtimeTransitionWorker struct {
	done    chan struct{}
	step    runtimeTransitionStep
	outcome runtimeTransitionOutcome
	err     error
}

type runtimeTransitionOutcome struct {
	steps        []runtimeTransitionStep
	generation   uint64
	adoption     adoptionOutcome
	adoptionPlan *adoptionPlan
	quiesce      *quiescePlan
}

func (c *runtimeTransitionSequence) begin(request *runtimeTransitionRequest) error {
	if c.worker != nil {
		return errors.New("Runtime transition superseded before settlement")
	}
	c.request = request
	c.steps, c.index, c.adoption, c.quiesce = nil, -1, adoptionPlan{}, quiescePlan{}
	c.ctx, c.cancel = context.WithCancel(request.ctx)
	c.start(runtimeTransitionStep{kind: runtimeTransitionPlanning})
	return nil
}

func (c *runtimeTransitionSequence) reconcile() {
	if c.worker == nil {
		return
	}
	select {
	case <-c.worker.done:
	default:
		return
	}
	completed := c.worker
	c.worker = nil
	if completed.err != nil {
		c.complete(completed.err)
		return
	}
	if err := c.recordOutcome(completed.step, completed.outcome); err != nil {
		c.complete(status.Error(codes.Internal, err.Error()))
		return
	}
	c.index++
	if c.index >= len(c.steps) {
		c.complete(nil)
		return
	}
	c.start(c.steps[c.index])
}

func (c *runtimeTransitionSequence) complete(err error) {
	c.cancel()
	if errors.Is(err, context.Canceled) {
		err = status.Error(codes.Canceled, err.Error())
	} else if errors.Is(err, context.DeadlineExceeded) {
		err = status.Error(codes.DeadlineExceeded, err.Error())
	}
	c.request.err = err
	close(c.request.done)
	c.request = nil
}

func (c *runtimeTransitionSequence) start(step runtimeTransitionStep) {
	worker := &runtimeTransitionWorker{done: make(chan struct{}), step: step}
	c.worker = worker
	go func() {
		worker.outcome, worker.err = c.runStep(step)
		close(worker.done)
	}()
}

func (c *runtimeTransitionSequence) runStep(step runtimeTransitionStep) (runtimeTransitionOutcome, error) {
	if err := c.ctx.Err(); err != nil {
		return runtimeTransitionOutcome{}, err
	}
	var outcome runtimeTransitionOutcome
	var err error
	switch step.kind {
	case runtimeTransitionPlanning:
		outcome, err = c.planTransition(c.ctx)
	case runtimeTransitionQuiesceSealing:
		outcome, err = c.runQuiesceBarrier(c.ctx)
	case runtimeTransitionNativeStopping,
		runtimeTransitionExecutionStarting,
		runtimeTransitionDesiredPublishing,
		runtimeTransitionExecutionFencing,
		runtimeTransitionHistoryAcknowledging,
		runtimeTransitionIncarnationAdopting,
		runtimeTransitionBootstrapRequesting:
		outcome, err = c.runAdoptionBarrier(c.ctx, step)
	default:
		err = fmt.Errorf("Runtime transition step kind %d has no barrier", step.kind)
	}
	if err != nil && step.kind == runtimeTransitionExecutionFencing {
		if errors.Is(err, context.Canceled) && !c.r.isClosing() {
			fenceCtx, cancel := context.WithTimeout(c.engineCtx, executionFenceGrace)
			defer cancel()
			if settleErr := c.r.waitForExecutionFenced(fenceCtx, c.adoption.generation); settleErr == nil {
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
	if err == nil || step.kind != runtimeTransitionExecutionStarting {
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
	case runtimeAdopt:
		plan, steps, err := c.planAdoption(ctx, c.request.UID)
		return runtimeTransitionOutcome{steps: steps, adoptionPlan: &plan}, err
	case runtimeQuiesce:
		plan, steps, err := c.planQuiesce(*c.request)
		return runtimeTransitionOutcome{steps: steps, quiesce: &plan}, err
	default:
		return runtimeTransitionOutcome{}, fmt.Errorf("unknown Runtime transition kind %d", c.request.Kind)
	}
}

func (c *runtimeTransitionSequence) recordOutcome(step runtimeTransitionStep, outcome runtimeTransitionOutcome) error {
	if step.kind == runtimeTransitionPlanning {
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
