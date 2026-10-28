package execserver

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/google/uuid"
)

type (
	Start func(context.Context) (*supervision.Instance, error)
	Probe func(context.Context) error
)

type Adapters struct {
	Start Start
	Probe Probe
}

type Dependencies struct {
	Context         context.Context
	State           state.State
	Workers         *sync.WaitGroup
	Processes       *supervision.Host
	Admission       lifecycle.AdmissionGuard
	OnError         func(error)
	OnRuntimeChange func()
}

type Host struct {
	config          Config
	ctx             context.Context
	state           state.State
	workers         *sync.WaitGroup
	processes       *supervision.Host
	admission       lifecycle.AdmissionGuard
	onError         func(error)
	onRuntimeChange func()
	start           Start
	probe           Probe

	inputMu sync.Mutex
	mu      sync.Mutex
	probeOp *operation
}

type operation struct {
	id     string
	cancel context.CancelFunc
	done   chan struct{}
	err    error
}

func NewHost(config Config, deps Dependencies, adapters Adapters) (*Host, error) {
	if config.URL == "" {
		return nil, errors.New("Codex exec-server URL is required")
	}
	if deps.Context == nil || deps.State == nil || deps.Workers == nil || deps.Processes == nil || deps.Admission == nil || deps.OnError == nil || adapters.Start == nil || adapters.Probe == nil {
		return nil, errors.New("Codex exec-server host dependencies are incomplete")
	}
	return &Host{
		config: config, ctx: deps.Context, state: deps.State, workers: deps.Workers,
		processes: deps.Processes, admission: deps.Admission, onError: deps.OnError,
		onRuntimeChange: deps.OnRuntimeChange, start: adapters.Start, probe: adapters.Probe,
	}, nil
}

func (h *Host) Controller() controller.QController {
	return &LifecycleController{host: h}
}

func (h *Host) Initialize(ctx context.Context) error {
	return safeModifyInput(ctx, h.state, Input{})
}

func (h *Host) URL() string { return h.config.URL }

func (h *Host) startProcess(ctx context.Context) (*supervision.Instance, error) {
	instance, err := h.start(ctx)
	var refusal Refusal
	if errors.As(err, &refusal) {
		err = &supervision.LaunchRefusal{Reason: refusal.Reason, Cause: err}
	}
	return instance, err
}

// RequestLocked publishes accepted demand. The caller must hold the Runner admission barrier and
// have verified that the current desired state is Running.
func (h *Host) RequestLocked(ctx context.Context) error {
	if h == nil {
		return errors.New("Codex exec-server is not configured")
	}
	h.inputMu.Lock()
	defer h.inputMu.Unlock()
	input, found, err := ReadInput(ctx, h.state)
	if err != nil {
		return err
	}
	if !found {
		return errors.New("Codex exec-server input is not initialized")
	}
	input.Demand++
	return safeModifyInput(ctx, h.state, input)
}

// ResetDemand drops execution requests while the Runner still suppresses new admissions.
func (h *Host) ResetDemand(ctx context.Context) error {
	if h == nil {
		return nil
	}
	h.inputMu.Lock()
	defer h.inputMu.Unlock()
	return safeModifyInput(ctx, h.state, Input{})
}

func (h *Host) RuntimeState(ctx context.Context) (Runtime, bool, error) {
	return ReadRuntime(ctx, h.state)
}

func (h *Host) withRuntimeChange(changed bool) {
	if changed && h.onRuntimeChange != nil {
		h.onRuntimeChange()
	}
}

func (h *Host) startProbe(work func(context.Context) error) {
	h.mu.Lock()
	if h.probeOp != nil {
		h.mu.Unlock()
		return
	}
	ctx, cancel := context.WithCancel(h.ctx)
	op := &operation{id: uuid.NewString(), cancel: cancel, done: make(chan struct{})}
	h.probeOp = op
	h.workers.Add(1)
	h.mu.Unlock()

	if err := h.publishOperation(op, false); err != nil {
		h.mu.Lock()
		if h.probeOp == op {
			h.probeOp = nil
		}
		h.mu.Unlock()
		cancel()
		h.workers.Done()
		h.onError(err)
		return
	}
	go func() {
		defer h.workers.Done()
		defer cancel()
		err := work(ctx)
		h.mu.Lock()
		op.err = err
		if publishErr := h.publishOperation(op, true); publishErr != nil {
			h.onError(publishErr)
		}
		close(op.done)
		h.mu.Unlock()
	}()
}

func (h *Host) probePending() bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.probeOp != nil
}

func (h *Host) cancelProbe() {
	h.mu.Lock()
	op := h.probeOp
	h.mu.Unlock()
	if op != nil && op.cancel != nil {
		op.cancel()
	}
}

func (h *Host) takeProbe() (*operation, bool) {
	h.mu.Lock()
	if !operationDone(h.probeOp) {
		h.mu.Unlock()
		return nil, false
	}
	op := h.probeOp
	h.probeOp = nil
	h.mu.Unlock()

	err := h.state.Destroy(h.ctx, resource.NewMetadata(lifecycle.Namespace, OperationType, op.id, resource.VersionUndefined), state.WithDestroyOwner(HostOwner))
	if err != nil && !state.IsNotFoundError(err) {
		h.onError(err)
	}
	return op, true
}

func operationDone(op *operation) bool {
	if op == nil {
		return false
	}
	select {
	case <-op.done:
		return true
	default:
		return false
	}
}

func (h *Host) publishOperation(op *operation, done bool) error {
	value := operationResult{Controller: ControllerName, Done: done}
	return safe.StateModify(h.ctx, h.state, componentResource(op.id, value), func(current *typed.Resource[operationResult, componentExtension[operationResult]]) error {
		*current.TypedSpec() = value
		return nil
	}, state.WithUpdateOwner(HostOwner))
}

func safeModifyInput(ctx context.Context, writer state.State, value Input) error {
	err := safe.StateModify(ctx, writer, componentResource(ResourceID, value), func(current *typed.Resource[Input, componentExtension[Input]]) error {
		*current.TypedSpec() = value
		return nil
	}, state.WithUpdateOwner(HostOwner))
	if err != nil {
		return fmt.Errorf("publish Codex exec-server input: %w", err)
	}
	return nil
}
