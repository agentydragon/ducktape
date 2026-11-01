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
	Event  uint64
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

type executionEventKind uint8

const (
	executionStarted executionEventKind = iota
	executionLost
	executionStoppedEvent
)

type executionEvent struct {
	kind    executionEventKind
	handle  executionHandle
	uid     string
	demand  uint64
	epoch   context.Context
	err     error
	skipped bool
}

type executionLifecycle struct {
	r                 *Runtime
	ctx               context.Context
	state             state.State
	inputMu           sync.Mutex
	mu                sync.Mutex
	events            []executionEvent
	handle            executionHandle
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
		if current.Phase == executionReady && current.UID == uid && h.readyOwner() {
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

func (h *executionLifecycle) readyOwner() bool {
	h.mu.Lock()
	handle := h.handle
	h.mu.Unlock()
	if handle == nil {
		return false
	}
	if service, ok := handle.(*executionServiceHandle); ok {
		return service.serving()
	}
	return true
}

func (h *executionLifecycle) signal(event executionEvent) {
	h.mu.Lock()
	h.events = append(h.events, event)
	h.mu.Unlock()
	h.inputMu.Lock()
	ctx := context.Background()
	input, found, err := cosi.Read(ctx, h.state, executionInputResource(executionInput{}))
	if err == nil && found {
		input.Event++
		err = cosi.Publish(ctx, h.state, executionInputResource(input), input, "embedded-execution-host")
	} else if err == nil {
		err = errors.New("embedded Execution input disappeared during lifecycle effect")
	}
	h.inputMu.Unlock()
	if err != nil {
		h.failTerminal(fmt.Errorf("publish embedded Execution lifecycle event: %w", err))
	}
}

func (h *executionLifecycle) take() (executionEvent, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.events) == 0 {
		return executionEvent{}, false
	}
	event := h.events[0]
	h.events = h.events[1:]
	return event, true
}

func (h *executionLifecycle) launch(uid string, demand uint64, recovery bool, lossBinding uint64) {
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
				h.signal(executionEvent{kind: executionStarted, uid: uid, demand: demand, err: errors.New("accepted Execution start lost its caller epoch")})
				return
			}
			if launchCtx.Err() != nil {
				h.signal(executionEvent{kind: executionStarted, uid: uid, demand: demand, skipped: true})
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
				h.signal(executionEvent{kind: executionStarted, uid: uid, demand: demand, skipped: true})
			} else {
				h.signal(executionEvent{kind: executionStarted, uid: uid, demand: demand, err: readErr})
			}
			return
		}
		if !found || input.UID != uid || startCtx.Err() != nil {
			h.signal(executionEvent{kind: executionStarted, uid: uid, demand: demand, skipped: true})
			return
		}
		h.r.mu.Lock()
		allowed := !h.r.closing && (!recovery || h.r.intent != nil && h.r.intent.Mode == runtimepb.LifecycleIntent_RUNNING && h.r.intent.IncarnationUid == uid && h.r.intent.GetBinding().GetGeneration() == lossBinding)
		h.r.mu.Unlock()
		if !allowed {
			h.signal(executionEvent{kind: executionStarted, uid: uid, demand: demand, skipped: true})
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
		h.signal(executionEvent{kind: executionStarted, handle: handle, uid: uid, demand: demand, epoch: launchCtx, err: err})
	}()
}

func (h *executionLifecycle) watch(handle executionHandle, uid string) {
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
			h.signal(executionEvent{
				kind: executionLost, handle: handle, uid: uid,
				err: fmt.Errorf("embedded Execution exited without fence proof (status=%d): %w", exit.code, exit.err),
			})
			return
		}
		h.signal(executionEvent{kind: executionLost, handle: handle, uid: uid})
	}()
}

func (h *executionLifecycle) stop(handle executionHandle, uid string) {
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
		h.signal(executionEvent{kind: executionStoppedEvent, handle: handle, uid: uid, err: err})
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
		if uid != "" && current.Phase == executionReady && current.UID == uid && current.Demand == demand && h.readyOwner() {
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
	if event, ok := h.take(); ok {
		switch event.kind {
		case executionStarted:
			if current.Phase != executionStarting || current.UID != event.uid || current.Demand != event.demand {
				err := errors.New("embedded Execution launch outcome lost its generation")
				h.failTerminal(err)
				return err
			}
			h.mu.Lock()
			for demand := range h.startContexts {
				if demand <= current.Demand {
					delete(h.startContexts, demand)
				}
			}
			h.mu.Unlock()
			if event.skipped || errors.Is(event.err, ErrFencedStartup) {
				current.Phase, current.FencedGeneration = executionStopped, current.Generation
				current.AwaitDemand = true
				outcome := event.err
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
			} else if event.err != nil {
				current.Phase, current.Error = executionFailed, event.err.Error()
				if event.handle != nil {
					h.mu.Lock()
					h.handle = event.handle
					h.mu.Unlock()
					h.r.mu.Lock()
					h.r.executionOwner, h.r.executionUID = event.handle, event.uid
					h.r.executionServer = nil
					h.r.executionRecovering = true
					h.r.changedLocked()
					h.r.mu.Unlock()
				}
				h.r.fail(fmt.Errorf("start embedded Execution: %w", event.err))
			} else {
				h.mu.Lock()
				h.handle = event.handle
				h.mu.Unlock()
				h.r.mu.Lock()
				h.r.executionOwner, h.r.executionUID = event.handle, event.uid
				validEpoch := event.epoch == nil || event.epoch.Err() == nil
				if input.UID == event.uid && input.Demand == event.demand && validEpoch {
					h.r.executionServer = event.handle.Executor()
					h.r.executionRecovering = false
				} else {
					h.r.executionServer = nil
					h.r.executionRecovering = true
				}
				h.r.changedLocked()
				h.r.mu.Unlock()
				if input.UID == event.uid && input.Demand == event.demand && validEpoch {
					current.Phase = executionReady
				} else {
					current.Phase = executionStopping
					h.stop(event.handle, event.uid)
				}
				h.watch(event.handle, event.uid)
			}
		case executionLost:
			if current.Phase != executionReady || current.UID != event.uid {
				break
			}
			current.Phase, current.Lost = executionStopping, true
			if event.err != nil {
				current.Error = event.err.Error()
			}
			h.r.mu.Lock()
			if h.r.intent == nil || h.r.intent.Mode != runtimepb.LifecycleIntent_RUNNING || h.r.intent.IncarnationUid != event.uid {
				current.AwaitDemand = true
			} else {
				current.LossBinding = h.r.intent.GetBinding().GetGeneration()
			}
			h.r.executionServer = nil
			h.r.executionRecovering = true
			h.r.changedLocked()
			h.r.mu.Unlock()
			h.stop(event.handle, event.uid)
		case executionStoppedEvent:
			if current.Phase != executionStopping || current.UID != event.uid {
				err := errors.New("embedded Execution fence outcome lost its generation")
				h.failTerminal(err)
				return err
			}
			if event.err != nil {
				current.Phase, current.Error = executionFailed, fmt.Sprintf("embedded Execution fence failed: %v", event.err)
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
				h.handle = nil
				if h.watchCancel != nil {
					h.watchCancel()
					h.watchCancel = nil
				}
				h.mu.Unlock()
				h.r.mu.Lock()
				h.r.executionOwner, h.r.executionServer, h.r.executionUID = nil, nil, ""
				h.r.executionRecovering = false
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
		h.r.executionServer = nil
		h.r.executionRecovering = true
		h.r.changedLocked()
		h.r.mu.Unlock()
		if err := write(); err != nil {
			return err
		}
		h.mu.Lock()
		handle := h.handle
		h.mu.Unlock()
		if handle == nil {
			err := errors.New("ready embedded Execution has no owned service")
			h.failTerminal(err)
			return err
		}
		h.stop(handle, current.UID)
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
		h.launch(input.UID, input.Demand, recovery, current.LossBinding)
		return nil
	}
	return write()
}
