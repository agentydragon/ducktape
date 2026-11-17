package coordinator

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

const executionSignalType = "ExecutionLifecycleSignals.agentplane"

type executionSignal struct {
	UID    string
	Demand uint64
	Start  executionStartCompletion
	Loss   executionLossCompletion
	Fence  executionFenceCompletion
}

// Completion facts are scoped to the generation that issued the effect. They
// wake the controller through this resource, while native handles remain in memory.
type executionStartCompletion struct {
	Generation uint64
	UID        string
	Demand     uint64
	Skipped    bool
	Fenced     bool
	Error      string
}

type executionLossCompletion struct {
	Generation uint64
	UID        string
	Error      string
}

type executionFenceCompletion struct {
	LifecycleGeneration uint64
	Generation          uint64
	UID                 string
	Error               string
}

type executionPhase uint8

const (
	executionStopped executionPhase = iota
	executionStarting
	executionReady
	executionStopping
	executionFailed
)

// A generation is fenced only after all forwarded calls join and the service
// proves its native process domain empty. An uncertain fence leaves the old
// owner retained and the controller terminal.
type executionStatus struct {
	UID              string
	Phase            executionPhase
	Generation       uint64
	FencedGeneration uint64
	Error            string
	Lost             bool
	Demand           uint64
	AwaitDemand      bool
	LossBinding      uint64
}

func (value executionSignal) DeepCopy() executionSignal { return value }
func (value executionStatus) DeepCopy() executionStatus { return value }

type executionExtension[T any] struct{}

func (executionExtension[T]) ResourceDefinition() spec.ResourceDefinitionSpec {
	var value T
	var typ resource.Type
	switch any(value).(type) {
	case executionSignal:
		typ = executionSignalType
	case executionStatus:
		typ = lifecycle.ExecutionStatusType
	}
	return spec.ResourceDefinitionSpec{Type: typ, DefaultNamespace: lifecycle.Namespace}
}

func executionSignalResource(value executionSignal) *typed.Resource[executionSignal, executionExtension[executionSignal]] {
	return typed.NewResource[executionSignal, executionExtension[executionSignal]](
		resource.NewMetadata(lifecycle.Namespace, executionSignalType, lifecycle.ExecutionResourceID, resource.VersionUndefined), value,
	)
}

func executionStatusResource(value executionStatus) *typed.Resource[executionStatus, executionExtension[executionStatus]] {
	return typed.NewResource[executionStatus, executionExtension[executionStatus]](
		resource.NewMetadata(lifecycle.Namespace, lifecycle.ExecutionStatusType, lifecycle.ExecutionResourceID, resource.VersionUndefined), value,
	)
}

type executionStartResult struct {
	generation  uint64
	handle      executionHandle
	epoch       context.Context
	err         error
	recovery    bool
	lossBinding uint64
}

type executionLifecycle struct {
	parent            executionParent
	ctx               context.Context
	state             state.State
	inputMu           sync.Mutex
	mu                sync.Mutex
	startResult       executionStartResult
	unfencedStarts    []executionHandle
	terminal          error
	startContexts     map[uint64]context.Context
	startOutcomes     map[uint64]error
	activeDemands     map[uint64]struct{}
	activeStartCancel context.CancelFunc
	activeStartEpoch  uint64
	watchCancel       context.CancelFunc
}

func (h *executionLifecycle) failTerminal(err error) {
	h.mu.Lock()
	if h.terminal == nil {
		h.terminal = err
	}
	h.mu.Unlock()
	h.parent.fail(err)
}

func (h *executionLifecycle) terminalError() error {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.terminal
}

func (h *executionLifecycle) cancelStart() {
	h.mu.Lock()
	cancel := h.activeStartCancel
	h.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (h *executionLifecycle) initialize(ctx context.Context) error {
	return cosi.Publish(ctx, h.state, executionSignalResource(executionSignal{}), executionSignal{}, "execution-host")
}

func (h *executionLifecycle) request(ctx context.Context, uid string) (uint64, error) {
	if err := h.terminalError(); err != nil {
		return 0, err
	}
	h.inputMu.Lock()
	defer h.inputMu.Unlock()
	input, found, err := cosi.Read(ctx, h.state, executionSignalResource(executionSignal{}))
	if err != nil {
		return 0, err
	}
	if !found {
		return 0, errors.New("Execution lifecycle signals are not initialized")
	}
	if uid != "" && input.UID == uid {
		current, _, err := cosi.Read(ctx, h.state, executionStatusResource(executionStatus{}))
		if err != nil {
			return 0, err
		}
		if current.Phase == executionReady && current.UID == uid && h.readyOwner(uid) {
			return current.Demand, nil
		}
	}
	input.UID, input.Demand = uid, input.Demand+1
	if uid != "" {
		h.mu.Lock()
		if h.startContexts == nil {
			h.startContexts = make(map[uint64]context.Context)
		}
		if h.activeDemands == nil {
			h.activeDemands = make(map[uint64]struct{})
		}
		h.startContexts[input.Demand] = ctx
		h.activeDemands[input.Demand] = struct{}{}
		h.mu.Unlock()
	}
	err = cosi.Publish(ctx, h.state, executionSignalResource(input), input, "execution-host")
	if err != nil {
		h.mu.Lock()
		delete(h.startContexts, input.Demand)
		delete(h.activeDemands, input.Demand)
		h.mu.Unlock()
		h.failTerminal(fmt.Errorf("publish Execution intent: %w", err))
	}
	if uid == "" {
		h.cancelStart()
	}
	return input.Demand, err
}

func (h *executionLifecycle) readyOwner(uid string) bool {
	return h.parent.hasExecutionOwner(uid)
}

func (h *executionLifecycle) publishCompletion(update func(*executionSignal) bool) bool {
	h.inputMu.Lock()
	ctx := context.Background()
	input, found, err := cosi.Read(ctx, h.state, executionSignalResource(executionSignal{}))
	published := false
	if err == nil && found {
		if update(&input) {
			err = cosi.Publish(ctx, h.state, executionSignalResource(input), input, "execution-host")
			published = err == nil
		}
	} else if err == nil {
		err = errors.New("Execution lifecycle signals disappeared during lifecycle effect")
	}
	h.inputMu.Unlock()
	if err != nil {
		h.failTerminal(fmt.Errorf("publish Execution completion: %w", err))
	}
	return published
}

func (h *executionLifecycle) completeStart(uid string, demand uint64, result executionStartResult, skipped bool) {
	generation, handle, err := result.generation, result.handle, result.err
	fact := executionStartCompletion{Generation: generation, UID: uid, Demand: demand, Skipped: skipped, Fenced: errors.Is(err, ErrFencedStartup)}
	if err != nil {
		fact.Error = err.Error()
	}
	if !h.publishCompletion(func(input *executionSignal) bool {
		if input.Start.Generation >= generation {
			return false
		}
		h.mu.Lock()
		h.startResult = result
		h.mu.Unlock()
		input.Start = fact
		return true
	}) && handle != nil && !errors.Is(err, ErrFencedStartup) {
		// A rejected or unpublished start has no generation the controller can
		// claim. Keep its native handle for diagnosis and block further starts.
		h.mu.Lock()
		h.unfencedStarts = append(h.unfencedStarts, handle)
		h.mu.Unlock()
		h.parent.revokeExecutionOwner(nil, "")
		h.failTerminal(errors.New("embedded Execution start handle has no published completion"))
	}
}

func (h *executionLifecycle) completeLoss(generation uint64, uid string, err error) {
	fact := executionLossCompletion{Generation: generation, UID: uid}
	if err != nil {
		fact.Error = err.Error()
	}
	h.publishCompletion(func(input *executionSignal) bool {
		if input.Loss.Generation >= generation {
			return false
		}
		input.Loss = fact
		return true
	})
}

func (h *executionLifecycle) completeFence(generation uint64, uid string, err error) {
	fact := executionFenceCompletion{Generation: generation, UID: uid}
	if err != nil {
		fact.Error = err.Error()
	}
	h.publishCompletion(func(input *executionSignal) bool {
		if input.Fence.Generation >= generation {
			return false
		}
		input.Fence = fact
		return true
	})
}

func (h *executionLifecycle) launch(uid string, demand, generation uint64, recovery bool, lossBinding uint64) {
	workerDone := h.parent.registerExecutionWorker()
	go func() {
		defer workerDone()
		complete := func(handle executionHandle, epoch context.Context, err error, skipped bool) {
			h.completeStart(uid, demand, executionStartResult{
				generation: generation, handle: handle, epoch: epoch, err: err,
				recovery: recovery, lossBinding: lossBinding,
			}, skipped)
		}
		launchCtx := h.ctx
		if !recovery {
			h.mu.Lock()
			launchCtx = h.startContexts[demand]
			delete(h.startContexts, demand)
			h.mu.Unlock()
			if launchCtx == nil {
				complete(nil, nil, errors.New("accepted Execution start lost its caller epoch"), false)
				return
			}
			if launchCtx.Err() != nil {
				complete(nil, nil, nil, true)
				return
			}
		}
		startCtx, cancelStart := context.WithCancel(launchCtx)
		h.mu.Lock()
		h.activeStartEpoch++
		epoch := h.activeStartEpoch
		h.activeStartCancel = cancelStart
		h.mu.Unlock()
		defer func() {
			h.mu.Lock()
			if h.activeStartEpoch == epoch {
				h.activeStartCancel = nil
			}
			h.mu.Unlock()
			cancelStart()
		}()
		input, found, readErr := cosi.Read(startCtx, h.state, executionSignalResource(executionSignal{}))
		if readErr != nil {
			if startCtx.Err() != nil {
				complete(nil, nil, nil, true)
			} else {
				complete(nil, nil, readErr, false)
			}
			return
		}
		if !found || input.UID != uid || startCtx.Err() != nil {
			complete(nil, nil, nil, true)
			return
		}
		if !h.parent.executionStartAllowed(uid, recovery, lossBinding) {
			complete(nil, nil, nil, true)
			return
		}
		handle, err := h.parent.startExecution(startCtx, uid)
		if err == nil && startCtx.Err() != nil && handle != nil {
			fenceCtx, cancel := context.WithTimeout(context.Background(), executionFenceGrace)
			fenceErr := handle.StopAndFence(fenceCtx)
			cancel()
			if fenceErr != nil {
				err = errors.Join(startCtx.Err(), fenceErr)
			} else {
				err = errors.Join(ErrFencedStartup, startCtx.Err())
			}
		}
		if err == nil && handle != nil {
			stale := !h.parent.executionStartAllowed(uid, recovery, lossBinding)
			latest, latestFound, readErr := cosi.Read(context.Background(), h.state, executionSignalResource(executionSignal{}))
			if readErr != nil {
				err = readErr
			}
			if !latestFound || latest.UID != uid {
				stale = true
			}
			if stale || err != nil {
				fenceCtx, cancel := context.WithTimeout(context.Background(), executionFenceGrace)
				fenceErr := handle.StopAndFence(fenceCtx)
				cancel()
				if fenceErr != nil {
					err = errors.Join(err, fenceErr)
				} else if err == nil {
					err = ErrFencedStartup
				}
			}
		}
		if err == nil && (handle == nil || handle.Executor() == nil) {
			err = errors.New("embedded Execution service returned no ready adapter")
			if handle != nil {
				err = errors.Join(err, handle.StopAndFence(context.Background()))
			}
		}
		complete(handle, launchCtx, err, false)
	}()
}

func (h *executionLifecycle) watch(handle executionHandle, uid string, generation uint64) {
	monitor, ok := handle.(interface {
		ProcessExit() <-chan childExit
		stopRequested() bool
	})
	if !ok || monitor.ProcessExit() == nil {
		return
	}
	watchCtx, cancel := context.WithCancel(h.ctx)
	h.mu.Lock()
	h.watchCancel = cancel
	h.mu.Unlock()
	workerDone := h.parent.registerExecutionWorker()
	go func() {
		defer workerDone()
		defer cancel()
		var exit childExit
		var ok bool
		select {
		case exit, ok = <-monitor.ProcessExit():
		case <-watchCtx.Done():
			return
		}
		if !ok || monitor.stopRequested() {
			return
		}
		if exit.code != 0 || exit.err != nil {
			h.closeAdmissionOnLoss(handle, uid)
			h.completeLoss(generation, uid, fmt.Errorf("embedded Execution exited without fence proof (status=%d): %w", exit.code, exit.err))
			return
		}
		h.closeAdmissionOnLoss(handle, uid)
		h.completeLoss(generation, uid, nil)
	}()
}

func (h *executionLifecycle) closeAdmissionOnLoss(handle executionHandle, uid string) {
	h.parent.revokeExecutionOwner(handle, uid)
}

func (h *executionLifecycle) stop(handle executionHandle, uid string, generation uint64) {
	workerDone := h.parent.registerExecutionWorker()
	go func() {
		defer workerDone()
		ctx, cancel := context.WithTimeout(context.Background(), executionFenceGrace)
		defer cancel()
		err := h.parent.drainExecutionCalls(ctx)
		if err == nil {
			err = handle.StopAndFence(ctx)
		}
		h.completeFence(generation, uid, err)
	}()
}

func (h *executionLifecycle) recordStartOutcome(demand uint64, outcome error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, active := h.activeDemands[demand]; active {
		if h.startOutcomes == nil {
			h.startOutcomes = make(map[uint64]error)
		}
		h.startOutcomes[demand] = outcome
	}
}

func (h *executionLifecycle) wait(ctx context.Context, uid string, demand uint64) error {
	if uid != "" {
		defer func() {
			h.mu.Lock()
			delete(h.activeDemands, demand)
			delete(h.startOutcomes, demand)
			h.mu.Unlock()
		}()
	}
	for {
		if err := h.terminalError(); err != nil {
			return err
		}
		changed, controllerDone, failure := h.parent.executionChanges()
		if err := h.terminalError(); err != nil {
			return err
		}
		current, _, err := cosi.Read(ctx, h.state, executionStatusResource(executionStatus{}))
		if err != nil {
			return err
		}
		if current.Phase == executionFailed {
			return errors.New(current.Error)
		}
		if uid != "" && (current.Demand > demand || current.Phase == executionStopped && current.Demand == demand && current.FencedGeneration == current.Generation) {
			h.mu.Lock()
			outcome, settled := h.startOutcomes[demand]
			if settled {
				delete(h.startOutcomes, demand)
			}
			h.mu.Unlock()
			if settled {
				return outcome
			}
		}
		if uid != "" {
			if failure != nil {
				return failure
			}
		}
		if uid == "" && current.Phase == executionStopped && current.FencedGeneration == current.Generation {
			return nil
		}
		if uid != "" && current.Phase == executionReady && current.UID == uid && current.Demand == demand && h.readyOwner(uid) {
			return nil
		}
		select {
		case <-changed:
		case <-h.ctx.Done():
			return errors.New("embedded Execution lifecycle stopped before settlement")
		case <-controllerDone:
			return errors.New("embedded Execution controller stopped before settlement")
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

type executionController struct{ host *executionLifecycle }

func (*executionController) Name() string { return "execution-lifecycle" }

func (*executionController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycle.Namespace, Type: executionSignalType, Kind: controller.InputQPrimary},
			{Namespace: lifecycle.Namespace, Type: lifecycle.ExecutionStatusType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: lifecycle.ExecutionStatusType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*executionController) MapInput(_ context.Context, _ *zap.Logger, _ controller.QRuntime, _ controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	return []resource.Pointer{executionSignalResource(executionSignal{}).Metadata()}, nil
}

func (c *executionController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, _ resource.Pointer) error {
	h := c.host
	if h.terminalError() != nil {
		return nil
	}
	input, found, err := cosi.Read(ctx, r, executionSignalResource(executionSignal{}))
	if err != nil || !found {
		if err != nil {
			h.failTerminal(fmt.Errorf("read Execution lifecycle signals: %w", err))
		}
		return err
	}
	current, _, err := cosi.Read(ctx, r, executionStatusResource(executionStatus{}))
	if err != nil {
		h.failTerminal(fmt.Errorf("read Execution status: %w", err))
		return err
	}
	write := func() error {
		changed, err := cosi.Write(ctx, r, executionStatusResource(current), current)
		if err != nil {
			h.failTerminal(fmt.Errorf("write Execution status: %w", err))
		}
		if changed {
			h.parent.changed()
		}
		return err
	}
	if current.Phase == executionStarting && input.Start.Generation == current.Generation ||
		current.Phase == executionReady && input.Loss.Generation == current.Generation ||
		current.Phase == executionStopping && input.Fence.Generation == current.Generation {
		switch current.Phase {
		case executionStarting:
			start := input.Start
			if current.UID != start.UID || current.Demand != start.Demand {
				err := errors.New("embedded Execution launch outcome lost its generation")
				h.failTerminal(err)
				return err
			}
			h.mu.Lock()
			result := h.startResult
			h.startResult = executionStartResult{}
			for demand := range h.startContexts {
				if demand <= current.Demand {
					delete(h.startContexts, demand)
				}
			}
			h.mu.Unlock()
			if result.generation != current.Generation {
				err := errors.New("embedded Execution launch handle lost its generation")
				h.failTerminal(err)
				return err
			}
			if start.Skipped || start.Fenced {
				current.Phase, current.FencedGeneration = executionStopped, current.Generation
				current.AwaitDemand = true
				outcome := result.err
				if outcome == nil {
					outcome = errors.New("embedded Execution startup was superseded")
				}
				h.recordStartOutcome(current.Demand, outcome)
			} else if result.err != nil {
				current.Phase, current.Error = executionFailed, result.err.Error()
				if result.handle != nil {
					h.parent.publishExecutionOwner(start.UID, result, false)
				}
				h.parent.fail(fmt.Errorf("start embedded Execution: %w", result.err))
			} else {
				if h.parent.publishExecutionOwner(start.UID, result, input.UID == start.UID && input.Demand == start.Demand) {
					current.Phase = executionReady
				} else {
					current.Phase, current.AwaitDemand = executionStopping, true
					h.recordStartOutcome(current.Demand, errors.New("embedded Execution startup was superseded"))
					h.stop(result.handle, start.UID, current.Generation)
				}
				h.watch(result.handle, start.UID, current.Generation)
			}
		case executionReady:
			loss := input.Loss
			if current.UID != loss.UID {
				err := errors.New("embedded Execution loss outcome lost its generation")
				h.failTerminal(err)
				return err
			}
			current.Phase, current.Lost = executionStopping, true
			if loss.Error != "" {
				current.Error = loss.Error
			}
			handle, binding, err := h.parent.stopExecutionOwner(loss.UID)
			if err != nil {
				h.failTerminal(err)
				return err
			}
			current.LossBinding = binding
			current.AwaitDemand = binding == 0
			h.stop(handle, loss.UID, current.Generation)
		case executionStopping:
			fence := input.Fence
			if current.UID != fence.UID {
				err := errors.New("embedded Execution fence outcome lost its generation")
				h.failTerminal(err)
				return err
			}
			if fence.Error != "" {
				current.Phase, current.Error = executionFailed, fmt.Sprintf("embedded Execution fence failed: %s", fence.Error)
				h.parent.fail(errors.New(current.Error))
			} else if current.Error != "" {
				current.Phase, current.FencedGeneration = executionFailed, current.Generation
				h.parent.fail(errors.New(current.Error))
			} else {
				current.Phase, current.FencedGeneration = executionStopped, current.Generation
				if current.Lost {
					if !h.parent.executionStartAllowed(current.UID, true, current.LossBinding) {
						current.AwaitDemand = true
					}
				}
				h.mu.Lock()
				if h.watchCancel != nil {
					h.watchCancel()
					h.watchCancel = nil
				}
				h.mu.Unlock()
				if err := h.parent.clearExecutionOwner(current.UID); err != nil {
					h.failTerminal(err)
					return err
				}
			}
		}
		if err := write(); err != nil {
			return err
		}
		// Reconcile the latest desired UID after recording the completed effect.
		return controller.NewRequeueError(nil, 0)
	}
	if current.Phase == executionFailed {
		return nil
	}
	if current.Phase == executionReady && input.UID != current.UID {
		current.Phase, current.Lost = executionStopping, false
		handle, _, err := h.parent.stopExecutionOwner(current.UID)
		if err != nil {
			h.failTerminal(err)
			return err
		}
		if err := write(); err != nil {
			return err
		}
		h.stop(handle, current.UID, current.Generation)
		return nil
	}
	if current.Phase == executionStopped && input.UID != "" {
		if current.AwaitDemand && input.Demand == current.Demand {
			return nil
		}
		recovery := current.Lost && input.UID == current.UID && input.Demand == current.Demand
		if recovery {
			if !h.parent.executionStartAllowed(input.UID, true, current.LossBinding) {
				// Quiescing or closing consumed this loss. A later RUNNING
				// intent needs a new admission demand; the old one cannot
				// authorize recovery after the fact.
				current.AwaitDemand = true
				return write()
			}
		}
		current.Phase, current.UID, current.Generation, current.Error, current.Lost = executionStarting, input.UID, current.Generation+1, "", false
		current.Demand = input.Demand
		current.AwaitDemand = false
		if err := write(); err != nil {
			return err
		}
		h.launch(input.UID, input.Demand, current.Generation, recovery, current.LossBinding)
		return nil
	}
	return write()
}
