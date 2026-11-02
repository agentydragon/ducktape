package runtime

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/meta/spec"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
)

const (
	executionInputType  = "EmbeddedExecutionInputs.agentplane"
	executionStateType  = "EmbeddedExecutionRuntimes.agentplane"
	executionResourceID = "embedded-execution"
)

type executionInput struct {
	UID    string
	Demand uint64
	Start  executionStartCompletion
	Loss   executionLossCompletion
	Fence  executionFenceCompletion
}

// Completion facts are scoped to the generation that issued the effect. They
// wake the controller through its input, while native handles remain in memory.
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
	Generation uint64
	UID        string
	Error      string
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
type executionState struct {
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

func (value executionInput) DeepCopy() executionInput { return value }
func (value executionState) DeepCopy() executionState { return value }

type executionExtension[T any] struct{}

func (executionExtension[T]) ResourceDefinition() spec.ResourceDefinitionSpec {
	var value T
	var typ resource.Type
	switch any(value).(type) {
	case executionInput:
		typ = executionInputType
	case executionState:
		typ = executionStateType
	}
	return spec.ResourceDefinitionSpec{Type: typ, DefaultNamespace: lifecycle.Namespace}
}

func executionInputResource(value executionInput) *typed.Resource[executionInput, executionExtension[executionInput]] {
	return typed.NewResource[executionInput, executionExtension[executionInput]](
		resource.NewMetadata(lifecycle.Namespace, executionInputType, executionResourceID, resource.VersionUndefined), value,
	)
}

func executionStateResource(value executionState) *typed.Resource[executionState, executionExtension[executionState]] {
	return typed.NewResource[executionState, executionExtension[executionState]](
		resource.NewMetadata(lifecycle.Namespace, executionStateType, executionResourceID, resource.VersionUndefined), value,
	)
}

type executionStartResult struct {
	generation uint64
	handle     executionHandle
	epoch      context.Context
	err        error
}

type executionLifecycle struct {
	r                 *Runtime
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
	h.r.fail(err)
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
	return cosi.Publish(ctx, h.state, executionInputResource(executionInput{}), executionInput{}, "embedded-execution-host")
}

func (h *executionLifecycle) request(ctx context.Context, uid string) (uint64, error) {
	if err := h.terminalError(); err != nil {
		return 0, err
	}
	h.inputMu.Lock()
	defer h.inputMu.Unlock()
	input, found, err := cosi.Read(ctx, h.state, executionInputResource(executionInput{}))
	if err != nil {
		return 0, err
	}
	if !found {
		return 0, errors.New("embedded Execution input is not initialized")
	}
	if uid != "" && input.UID == uid {
		current, _, err := cosi.Read(ctx, h.state, executionStateResource(executionState{}))
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
	err = cosi.Publish(ctx, h.state, executionInputResource(input), input, "embedded-execution-host")
	if err != nil {
		h.mu.Lock()
		delete(h.startContexts, input.Demand)
		delete(h.activeDemands, input.Demand)
		h.mu.Unlock()
		h.failTerminal(fmt.Errorf("publish embedded Execution intent: %w", err))
	}
	if uid == "" {
		h.cancelStart()
	}
	return input.Demand, err
}

func (h *executionLifecycle) readyOwner(uid string) bool {
	h.r.mu.Lock()
	defer h.r.mu.Unlock()
	return h.r.executionOwner.executor(uid) != nil
}

func (h *executionLifecycle) publishCompletion(update func(*executionInput) bool) bool {
	h.inputMu.Lock()
	ctx := context.Background()
	input, found, err := cosi.Read(ctx, h.state, executionInputResource(executionInput{}))
	published := false
	if err == nil && found {
		if update(&input) {
			err = cosi.Publish(ctx, h.state, executionInputResource(input), input, "embedded-execution-host")
			published = err == nil
		}
	} else if err == nil {
		err = errors.New("embedded Execution input disappeared during lifecycle effect")
	}
	h.inputMu.Unlock()
	if err != nil {
		h.failTerminal(fmt.Errorf("publish embedded Execution completion: %w", err))
	}
	return published
}

func (h *executionLifecycle) completeStart(generation uint64, uid string, demand uint64, handle executionHandle, epoch context.Context, err error, skipped bool) {
	fact := executionStartCompletion{Generation: generation, UID: uid, Demand: demand, Skipped: skipped, Fenced: errors.Is(err, ErrFencedStartup)}
	if err != nil {
		fact.Error = err.Error()
	}
	if !h.publishCompletion(func(input *executionInput) bool {
		if input.Start.Generation >= generation {
			return false
		}
		h.mu.Lock()
		h.startResult = executionStartResult{generation: generation, handle: handle, epoch: epoch, err: err}
		h.mu.Unlock()
		input.Start = fact
		return true
	}) && handle != nil && !errors.Is(err, ErrFencedStartup) {
		// A rejected or unpublished start has no generation the controller can
		// claim. Keep its native handle for diagnosis and block further starts.
		h.mu.Lock()
		h.unfencedStarts = append(h.unfencedStarts, handle)
		h.mu.Unlock()
		h.r.mu.Lock()
		if owner := h.r.executionOwner; owner != nil {
			owner.admitted = false
			h.r.changedLocked()
		}
		h.r.mu.Unlock()
		h.failTerminal(errors.New("embedded Execution start handle has no published completion"))
	}
}

func (h *executionLifecycle) completeLoss(generation uint64, uid string, err error) {
	fact := executionLossCompletion{Generation: generation, UID: uid}
	if err != nil {
		fact.Error = err.Error()
	}
	h.publishCompletion(func(input *executionInput) bool {
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
	h.publishCompletion(func(input *executionInput) bool {
		if input.Fence.Generation >= generation {
			return false
		}
		input.Fence = fact
		return true
	})
}

func (h *executionLifecycle) launch(uid string, demand, generation uint64, recovery bool, lossBinding uint64) {
	h.r.lifecycleWorkers.Add(1)
	go func() {
		defer h.r.lifecycleWorkers.Done()
		launchCtx := h.ctx
		if !recovery {
			h.mu.Lock()
			launchCtx = h.startContexts[demand]
			delete(h.startContexts, demand)
			h.mu.Unlock()
			if launchCtx == nil {
				h.completeStart(generation, uid, demand, nil, nil, errors.New("accepted Execution start lost its caller epoch"), false)
				return
			}
			if launchCtx.Err() != nil {
				h.completeStart(generation, uid, demand, nil, nil, nil, true)
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
		input, found, readErr := cosi.Read(startCtx, h.state, executionInputResource(executionInput{}))
		if readErr != nil {
			if startCtx.Err() != nil {
				h.completeStart(generation, uid, demand, nil, nil, nil, true)
			} else {
				h.completeStart(generation, uid, demand, nil, nil, readErr, false)
			}
			return
		}
		if !found || input.UID != uid || startCtx.Err() != nil {
			h.completeStart(generation, uid, demand, nil, nil, nil, true)
			return
		}
		h.r.mu.Lock()
		allowed := !h.r.closing && (!recovery || h.r.intent != nil && h.r.intent.Mode == runtimepb.LifecycleIntent_RUNNING && h.r.intent.IncarnationUid == uid && h.r.intent.GetBinding().GetGeneration() == lossBinding)
		h.r.mu.Unlock()
		if !allowed {
			h.completeStart(generation, uid, demand, nil, nil, nil, true)
			return
		}
		handle, err := h.r.deps.startExecution(startCtx, h.r.config, uid)
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
			h.r.mu.Lock()
			stale := h.r.closing || recovery && (h.r.intent == nil || h.r.intent.Mode != runtimepb.LifecycleIntent_RUNNING || h.r.intent.IncarnationUid != uid || h.r.intent.GetBinding().GetGeneration() != lossBinding)
			h.r.mu.Unlock()
			latest, latestFound, readErr := cosi.Read(context.Background(), h.state, executionInputResource(executionInput{}))
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
		h.completeStart(generation, uid, demand, handle, launchCtx, err, false)
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
	h.r.lifecycleWorkers.Add(1)
	go func() {
		defer h.r.lifecycleWorkers.Done()
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
	h.r.mu.Lock()
	if owner := h.r.executionOwner; owner != nil && owner.handle == handle && owner.uid == uid {
		owner.admitted = false
		h.r.changedLocked()
	}
	h.r.mu.Unlock()
}

func (h *executionLifecycle) stop(handle executionHandle, uid string, generation uint64) {
	h.r.lifecycleWorkers.Add(1)
	go func() {
		defer h.r.lifecycleWorkers.Done()
		ctx, cancel := context.WithTimeout(context.Background(), executionFenceGrace)
		defer cancel()
		h.r.cancelExecutionCalls()
		err := h.r.waitForExecutionCalls(ctx)
		if err == nil {
			err = handle.StopAndFence(ctx)
		}
		h.completeFence(generation, uid, err)
	}()
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
		changed := h.r.changesSince()
		if err := h.terminalError(); err != nil {
			return err
		}
		current, _, err := cosi.Read(ctx, h.state, executionStateResource(executionState{}))
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
			h.r.mu.Lock()
			failure := h.r.failureLocked()
			h.r.mu.Unlock()
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
		case <-h.r.lifecycleDone:
			return errors.New("embedded Execution controller stopped before settlement")
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

type executionController struct{ host *executionLifecycle }

func (*executionController) Name() string { return "embedded-execution-lifecycle" }

func (*executionController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycle.Namespace, Type: executionInputType, Kind: controller.InputQPrimary},
			{Namespace: lifecycle.Namespace, Type: executionStateType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: executionStateType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (*executionController) MapInput(_ context.Context, _ *zap.Logger, _ controller.QRuntime, _ controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	return []resource.Pointer{executionInputResource(executionInput{}).Metadata()}, nil
}

func (c *executionController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, _ resource.Pointer) error {
	h := c.host
	if h.terminalError() != nil {
		return nil
	}
	input, found, err := cosi.Read(ctx, r, executionInputResource(executionInput{}))
	if err != nil || !found {
		if err != nil {
			h.failTerminal(fmt.Errorf("read embedded Execution input: %w", err))
		}
		return err
	}
	current, _, err := cosi.Read(ctx, r, executionStateResource(executionState{}))
	if err != nil {
		h.failTerminal(fmt.Errorf("read embedded Execution state: %w", err))
		return err
	}
	write := func() error {
		changed, err := cosi.Write(ctx, r, executionStateResource(current), current)
		if err != nil {
			h.failTerminal(fmt.Errorf("write embedded Execution state: %w", err))
		}
		if changed {
			h.r.changed()
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
				h.mu.Lock()
				if _, active := h.activeDemands[current.Demand]; active {
					if h.startOutcomes == nil {
						h.startOutcomes = make(map[uint64]error)
					}
					h.startOutcomes[current.Demand] = outcome
				}
				h.mu.Unlock()
			} else if result.err != nil {
				current.Phase, current.Error = executionFailed, result.err.Error()
				if result.handle != nil {
					h.r.mu.Lock()
					h.r.executionOwner = &executionLifetime{handle: result.handle, uid: start.UID}
					h.r.changedLocked()
					h.r.mu.Unlock()
				}
				h.r.fail(fmt.Errorf("start embedded Execution: %w", result.err))
			} else {
				h.r.mu.Lock()
				validEpoch := result.epoch == nil || result.epoch.Err() == nil
				h.r.executionOwner = &executionLifetime{
					handle: result.handle, uid: start.UID,
					admitted: input.UID == start.UID && input.Demand == start.Demand && validEpoch,
				}
				h.r.changedLocked()
				h.r.mu.Unlock()
				if input.UID == start.UID && input.Demand == start.Demand && validEpoch {
					current.Phase = executionReady
				} else {
					current.Phase = executionStopping
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
			h.r.mu.Lock()
			if h.r.intent == nil || h.r.intent.Mode != runtimepb.LifecycleIntent_RUNNING || h.r.intent.IncarnationUid != loss.UID {
				current.AwaitDemand = true
			} else {
				current.LossBinding = h.r.intent.GetBinding().GetGeneration()
			}
			owner := h.r.executionOwner
			if owner == nil || owner.uid != loss.UID {
				h.r.mu.Unlock()
				err := errors.New("lost embedded Execution has no matching owner")
				h.failTerminal(err)
				return err
			}
			owner.admitted = false
			h.r.changedLocked()
			h.r.mu.Unlock()
			h.stop(owner.handle, loss.UID, current.Generation)
		case executionStopping:
			fence := input.Fence
			if current.UID != fence.UID {
				err := errors.New("embedded Execution fence outcome lost its generation")
				h.failTerminal(err)
				return err
			}
			if fence.Error != "" {
				current.Phase, current.Error = executionFailed, fmt.Sprintf("embedded Execution fence failed: %s", fence.Error)
				h.r.fail(errors.New(current.Error))
			} else if current.Error != "" {
				current.Phase, current.FencedGeneration = executionFailed, current.Generation
				h.r.fail(errors.New(current.Error))
			} else {
				current.Phase, current.FencedGeneration = executionStopped, current.Generation
				if current.Lost {
					h.r.mu.Lock()
					allowed := !h.r.closing && h.r.intent != nil && h.r.intent.Mode == runtimepb.LifecycleIntent_RUNNING && h.r.intent.IncarnationUid == current.UID && h.r.intent.GetBinding().GetGeneration() == current.LossBinding
					h.r.mu.Unlock()
					if !allowed {
						current.AwaitDemand = true
					}
				}
				h.mu.Lock()
				if h.watchCancel != nil {
					h.watchCancel()
					h.watchCancel = nil
				}
				h.mu.Unlock()
				h.r.mu.Lock()
				h.r.executionOwner = nil
				h.r.changedLocked()
				h.r.mu.Unlock()
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
		h.r.mu.Lock()
		owner := h.r.executionOwner
		if owner != nil {
			owner.admitted = false
		}
		h.r.changedLocked()
		h.r.mu.Unlock()
		if owner == nil {
			err := errors.New("ready embedded Execution has no owned service")
			h.failTerminal(err)
			return err
		}
		if err := write(); err != nil {
			return err
		}
		h.stop(owner.handle, owner.uid, current.Generation)
		return nil
	}
	if current.Phase == executionStopped && input.UID != "" {
		if current.AwaitDemand && input.Demand == current.Demand {
			return nil
		}
		recovery := current.Lost && input.UID == current.UID && input.Demand == current.Demand
		if recovery {
			h.r.mu.Lock()
			allowed := !h.r.closing && h.r.intent != nil && h.r.intent.Mode == runtimepb.LifecycleIntent_RUNNING && h.r.intent.IncarnationUid == input.UID && h.r.intent.GetBinding().GetGeneration() == current.LossBinding
			h.r.mu.Unlock()
			if !allowed {
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
