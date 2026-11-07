package bootstrap

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/oneoff"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/scripts"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const (
	selectionConflict     = "sandbox already selected a different bootstrap script"
	interruptedMessage    = "the Sandbox bootstrap was interrupted and never runs again; use a new Sandbox"
	failedMessage         = "the Sandbox bootstrap failed and never runs again; use a new Sandbox"
	operationHasNoOutcome = "bootstrap has no terminal outcome"
	maximumBootstrapBytes = 65536
)

var ErrInterrupted = errors.New(interruptedMessage)

type Config struct {
	Script           *string
	StateDir         string
	WorkingDirectory string
}

type Dependencies struct {
	Context         context.Context
	State           state.State
	Workers         *sync.WaitGroup
	Scripts         *scripts.Runner
	Admission       lifecycle.EnvironmentAdmissionGuard
	OnError         func(error)
	OnRuntimeChange func()
}

// Host coordinates the Environment Runtime's durable, one-shot bootstrap operation.
type Host struct {
	config          Config
	ctx             context.Context
	state           state.State
	workers         *sync.WaitGroup
	scripts         *scripts.Runner
	admission       lifecycle.EnvironmentAdmissionGuard
	onError         func(error)
	onRuntimeChange func()

	requestMu          sync.Mutex
	mu                 sync.Mutex
	operation          *oneoff.Operation
	eventWritePoisoned error
}

func NewHost(config Config, deps Dependencies) (*Host, error) {
	if deps.Context == nil || deps.State == nil || deps.Workers == nil || deps.Admission == nil || deps.OnError == nil {
		return nil, errors.New("bootstrap host dependencies are incomplete")
	}
	if config.Script != nil && config.StateDir == "" {
		return nil, errors.New("bootstrap state directory is required")
	}
	if config.Script != nil && deps.Scripts == nil {
		return nil, errors.New("bootstrap script runner is required when configured")
	}

	return &Host{
		config: config, ctx: deps.Context, state: deps.State, workers: deps.Workers,
		scripts: deps.Scripts, admission: deps.Admission, onError: deps.OnError,
		onRuntimeChange: deps.OnRuntimeChange,
	}, nil
}

func (h *Host) Controller() controller.QController {
	return &LifecycleController{host: h}
}

func (h *Host) Configured() bool { return h != nil && h.config.Script != nil }

func (h *Host) Initialize(ctx context.Context) error {
	if err := lifecycle.Publish(ctx, h.state, ResourceID, Request{}, HostOwner); err != nil {
		return err
	}
	if !h.Configured() {
		return nil
	}
	return lifecycle.Publish(ctx, h.state, ResourceID, operationSignal{}, HostOwner)
}

// Request records accepted bootstrap demand while Runtime's admission guard
// holds its admission lock. The selection and event log are the durable
// authorities. An already accepted request remains successful after
// admissions close.
func (h *Host) Request(ctx context.Context) error {
	if h == nil || !h.Configured() {
		return nil
	}

	h.requestMu.Lock()
	defer h.requestMu.Unlock()

	input, found, err := ReadRequest(ctx, h.state)
	if err != nil {
		return err
	}
	if !found {
		return errors.New("bootstrap request is not initialized")
	}
	if input.Requested {
		return nil
	}

	expected, found, err := lifecycle.ReadEnvironmentDesired(ctx, h.state)
	if err != nil {
		return err
	}
	if !found {
		return errors.New("Environment Runtime desired state is not initialized")
	}
	if expected.Mode != lifecycle.Running {
		return requestRefusal(expected.Mode)
	}

	effectInvoked := false
	err = h.admission(ctx, expected, func(effectCtx context.Context) error {
		effectInvoked = true
		current, found, err := ReadRequest(effectCtx, h.state)
		if err != nil {
			return err
		}
		if !found {
			return errors.New("bootstrap request is not initialized")
		}
		if current.Requested {
			return nil
		}
		current.Requested = true
		return lifecycle.Publish(effectCtx, h.state, ResourceID, current, HostOwner)
	})
	if err != nil || effectInvoked {
		return err
	}

	current, found, err := lifecycle.ReadEnvironmentDesired(ctx, h.state)
	if err != nil {
		return err
	}
	if found {
		return requestRefusal(current.Mode)
	}
	return status.Error(codes.Unavailable, "Runtime desired state is not initialized")
}

func requestRefusal(mode lifecycle.Mode) error {
	if mode == lifecycle.Closing {
		return status.Error(codes.Unavailable, "Runtime is closing")
	}
	return status.Error(codes.Aborted, "this Sandbox is quiesced or bootstrap admission changed")
}

// Wait observes the admitted attempt. Canceling ctx only stops this wait; the
// operation uses the host lifetime and remains joined by the shared worker barrier.
func (h *Host) Wait(ctx context.Context) error {
	if h == nil || !h.Configured() {
		return nil
	}
	if err := h.Request(h.ctx); err != nil {
		return err
	}

	waitCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	stop := context.AfterFunc(h.ctx, cancel)
	defer stop()
	if _, err := safe.StateWatchFor[*typed.Resource[Status, lifecycle.Extension[Status]]](
		waitCtx,
		h.state,
		lifecycle.ResourceFor(ResourceID, Status{}).Metadata(),
		state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			current, ok := value.(*typed.Resource[Status, lifecycle.Extension[Status]])
			if !ok {
				return false, fmt.Errorf("unexpected bootstrap status resource %T", value)
			}
			return current.TypedSpec().Phase.IsTerminal(), nil
		}),
	); err != nil {
		return err
	}
	bootstrapStatus, found, err := ReadStatus(waitCtx, h.state)
	if err != nil {
		return err
	}
	if !found {
		return errors.New(operationHasNoOutcome)
	}
	switch bootstrapStatus.Phase {
	case Succeeded:
		return nil
	case Interrupted:
		if bootstrapStatus.Error != "" && bootstrapStatus.Error != interruptedMessage {
			return errors.New(bootstrapStatus.Error)
		}
		return ErrInterrupted
	case Failed:
		if bootstrapStatus.Error == selectionConflict {
			return status.Error(codes.AlreadyExists, bootstrapStatus.Error)
		}
		if bootstrapStatus.Error != "" {
			return errors.New(bootstrapStatus.Error)
		}
		return errors.New(failedMessage)
	default:
		return errors.New(operationHasNoOutcome)
	}
}

func (h *Host) Settled(ctx context.Context) (bool, error) {
	if h == nil || !h.Configured() {
		return true, nil
	}
	input, found, err := ReadRequest(ctx, h.state)
	if err != nil {
		return false, err
	}
	if !found || !input.Requested {
		return true, nil
	}
	bootstrapStatus, found, err := ReadStatus(ctx, h.state)
	if err != nil {
		return false, err
	}
	return found && bootstrapStatus.Phase.IsTerminal(), nil
}

func (h *Host) Status(ctx context.Context) (Status, bool, error) {
	return ReadStatus(ctx, h.state)
}

func (h *Host) startOperation(work func(context.Context) error) error {
	h.mu.Lock()
	if h.operation != nil {
		h.mu.Unlock()
		return nil
	}
	op := oneoff.New(h.ctx)
	op.Register(h.workers)
	h.operation = op
	h.mu.Unlock()

	err := op.Start(
		&h.mu,
		func(bool) error { return h.publishOperationSignal() },
		h.onError,
		work,
		func() { h.runtimeChanged(true) },
		func() { h.runtimeChanged(false) },
	)
	if err != nil {
		h.mu.Lock()
		if h.operation == op {
			h.operation = nil
		}
		h.mu.Unlock()
	}
	return err
}

func (h *Host) publishOperationSignal() error {
	_, err := safe.StateUpdateWithConflicts(h.ctx, h.state, lifecycle.ResourceFor(ResourceID, operationSignal{}).Metadata(), func(current *typed.Resource[operationSignal, lifecycle.Extension[operationSignal]]) error {
		current.TypedSpec().Revision++
		return nil
	}, state.WithUpdateOwner(HostOwner))
	return err
}

func (h *Host) completedOperation() *oneoff.Operation {
	h.mu.Lock()
	defer h.mu.Unlock()
	op := h.operation
	if op == nil || !op.Finished() {
		return nil
	}
	return op
}

func (h *Host) consumeOperation(op *oneoff.Operation) {
	h.mu.Lock()
	if h.operation == op {
		h.operation = nil
	}
	h.mu.Unlock()
}

func (h *Host) runtimeChanged(changed bool) {
	if changed && h.onRuntimeChange != nil {
		h.onRuntimeChange()
	}
}

func (h *Host) runBootstrap(ctx context.Context) error {
	h.mu.Lock()
	eventWritePoisoned := h.eventWritePoisoned
	h.mu.Unlock()
	if eventWritePoisoned != nil {
		return errors.Join(ErrInterrupted, fmt.Errorf("bootstrap event writer is unavailable until restart: %w", eventWritePoisoned))
	}
	script := h.config.Script
	if script == nil {
		return errors.New("bootstrap is not configured")
	}
	if err := validateScript(*script); err != nil {
		return err
	}
	log, err := OpenLog(ctx, h.state)
	if err != nil {
		return err
	}
	selected, found, err := ReadSelection(ctx, h.state)
	if err != nil {
		return err
	}
	newSelection := !found
	if !found {
		if log.LastSequence() != 0 {
			return errors.New("bootstrap events exist without a bootstrap selection")
		}
		if err := publishSelection(ctx, h.state, *script); err != nil {
			return err
		}
		selected, found, err = ReadSelection(ctx, h.state)
		if err != nil || !found {
			return errors.Join(err, errors.New("durable bootstrap selection unavailable after publication"))
		}
	}
	if selected != *script {
		return status.Error(codes.AlreadyExists, selectionConflict)
	}
	if log.Ended() {
		return bootstrapErrorForLog(ctx, log)
	}
	if !newSelection || log.LastSequence() != 0 {
		if err := log.AppendInterrupted(context.WithoutCancel(ctx), 1); err != nil {
			h.poisonEventWriter(err)
			return errors.Join(ErrInterrupted, err)
		}
		return ErrInterrupted
	}
	cwd := h.config.WorkingDirectory
	if cwd == "" {
		cwd = h.config.StateDir
	}
	writeCtx := context.WithoutCancel(ctx)
	code, err := h.scripts.Run(ctx, ControllerName, selected, cwd, func(stdout bool, data []byte) error {
		stream := sessionpb.InitializationStream_INITIALIZATION_STREAM_STDERR
		if stdout {
			stream = sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT
		}
		if err := log.AppendOutput(writeCtx, 1, stream, data); err != nil {
			h.poisonEventWriter(err)
			return err
		}
		return nil
	})
	if err != nil {
		if appendErr := log.AppendInterrupted(writeCtx, 1); appendErr != nil {
			h.poisonEventWriter(appendErr)
			return errors.Join(ErrInterrupted, err, appendErr)
		}
		return errors.Join(ErrInterrupted, err)
	}
	if _, err := log.AppendResult(writeCtx, 1, int32(code)); err != nil {
		h.poisonEventWriter(err)
		return errors.Join(ErrInterrupted, fmt.Errorf("persist bootstrap result: %w", err))
	}
	if code != 0 {
		return errors.New(failedMessage)
	}
	return nil
}

func (h *Host) poisonEventWriter(err error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.eventWritePoisoned == nil {
		h.eventWritePoisoned = err
	}
}

func validateScript(script string) error {
	if len(script) == 0 || len(script) > maximumBootstrapBytes {
		return errors.New("bootstrap must contain 1..65536 bytes")
	}
	return nil
}

func bootstrapErrorForLog(ctx context.Context, log *Log) error {
	terminal, err := log.terminalEvent(ctx)
	if err != nil {
		return err
	}
	if terminal == nil {
		return errors.New(operationHasNoOutcome)
	}
	switch {
	case terminal.GetInterrupted() != nil:
		return ErrInterrupted
	case terminal.GetResult() == nil:
		return errors.New(operationHasNoOutcome)
	case terminal.GetResult().ExitCode != 0:
		return errors.New(failedMessage)
	default:
		return nil
	}
}
