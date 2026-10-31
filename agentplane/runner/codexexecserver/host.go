package codexexecserver

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/cosi"
	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/operation"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
)

type (
	Start func(context.Context) (*supervision.Instance, error)
	Probe func(context.Context) error
)

type Adapters struct {
	Start Start
	Probe Probe
}

// Readiness contains the independently configurable executor startup windows.
type Readiness struct {
	Timeout       time.Duration
	ProbeTimeout  time.Duration
	ProbeInterval time.Duration
}

// Resolve supplies defaults for omitted windows and rejects invalid direct Go configuration.
func (r Readiness) Resolve() (Readiness, error) {
	if err := resolveDuration("Codex exec-server readiness timeout", &r.Timeout, 20*time.Second); err != nil {
		return Readiness{}, err
	}
	if err := resolveDuration("Codex exec-server readiness probe timeout", &r.ProbeTimeout, 2*time.Second); err != nil {
		return Readiness{}, err
	}
	if err := resolveDuration("Codex exec-server readiness probe interval", &r.ProbeInterval, 100*time.Millisecond); err != nil {
		return Readiness{}, err
	}
	return r, nil
}

func resolveDuration(name string, value *time.Duration, defaultValue time.Duration) error {
	if *value < 0 {
		return fmt.Errorf("%s must be positive", name)
	}
	if *value == 0 {
		*value = defaultValue
	}
	return nil
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
	probeOp *operation.Operation
}

func NewHost(config Config, deps Dependencies, adapters Adapters) (*Host, error) {
	if config.URL == "" {
		return nil, errors.New("Codex exec-server URL is required")
	}
	readiness, err := config.Readiness.Resolve()
	if err != nil {
		return nil, err
	}
	if deps.Context == nil || deps.State == nil || deps.Workers == nil || deps.Processes == nil || deps.Admission == nil || deps.OnError == nil || adapters.Start == nil || adapters.Probe == nil {
		return nil, errors.New("Codex exec-server host dependencies are incomplete")
	}
	config.Readiness = readiness
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
	op := operation.New(h.ctx)
	h.probeOp = op
	op.Register(h.workers)
	h.mu.Unlock()

	if err := op.Start(
		&h.mu,
		func(done bool) error { return h.publishOperation(op, done) },
		h.onError,
		work,
		nil,
		nil,
	); err != nil {
		h.mu.Lock()
		if h.probeOp == op {
			h.probeOp = nil
		}
		h.mu.Unlock()
	}
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
	if op != nil {
		op.Cancel()
	}
}

func (h *Host) takeProbe() (*operation.Operation, bool) {
	op, done := operation.Consume(&h.mu, &h.probeOp)
	if !done {
		return nil, false
	}

	err := h.state.Destroy(h.ctx, resource.NewMetadata(lifecycle.Namespace, OperationType, op.ID(), resource.VersionUndefined), state.WithDestroyOwner(HostOwner))
	if err != nil && !state.IsNotFoundError(err) {
		h.onError(err)
	}
	return op, true
}

func (h *Host) publishOperation(op *operation.Operation, done bool) error {
	value := operationResult{Controller: ControllerName, Done: done}
	return cosi.Publish(h.ctx, h.state, componentResource(op.ID(), value), value, HostOwner)
}

func safeModifyInput(ctx context.Context, writer state.State, value Input) error {
	err := cosi.Publish(ctx, writer, componentResource(ResourceID, value), value, HostOwner)
	if err != nil {
		return fmt.Errorf("publish Codex exec-server input: %w", err)
	}
	return nil
}
