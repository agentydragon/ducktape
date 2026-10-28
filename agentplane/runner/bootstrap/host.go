package bootstrap

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/agentydragon/ducktape/agentplane/runner/scripts"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/google/uuid"
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
	Admission       lifecycle.AdmissionGuard
	OnError         func(error)
	OnRuntimeChange func()
}

type Host struct {
	config          Config
	ctx             context.Context
	state           state.State
	workers         *sync.WaitGroup
	scripts         *scripts.Runner
	admission       lifecycle.AdmissionGuard
	onError         func(error)
	onRuntimeChange func()

	requestMu sync.Mutex
	mu        sync.Mutex
	operation *operation
	logMu     sync.Mutex
	log       *Log
}

type operation struct {
	id   string
	done chan struct{}
	err  error
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
	return publish(ctx, h.state, ResourceID, Input{})
}

// Request records accepted bootstrap demand while the Runner's admission guard
// holds its admission lock. The retained request file and event log are the
// durable authorities. An already accepted request remains successful after
// admissions close.
func (h *Host) Request(ctx context.Context) error {
	if h == nil || !h.Configured() {
		return nil
	}

	h.requestMu.Lock()
	defer h.requestMu.Unlock()

	input, found, err := ReadInput(ctx, h.state)
	if err != nil {
		return err
	}
	if !found {
		return errors.New("bootstrap input is not initialized")
	}
	if input.Requested {
		return nil
	}

	expected, found, err := lifecycle.ReadDesired(ctx, h.state)
	if err != nil {
		return err
	}
	if !found {
		return errors.New("Runner desired state is not initialized")
	}
	if expected.Mode != lifecycle.Running {
		return requestRefusal(expected.Mode)
	}

	effectInvoked := false
	err = h.admission(ctx, expected, func(effectCtx context.Context) error {
		effectInvoked = true
		current, found, err := ReadInput(effectCtx, h.state)
		if err != nil {
			return err
		}
		if !found {
			return errors.New("bootstrap input is not initialized")
		}
		if current.Requested {
			return nil
		}
		current.Requested = true
		return publish(effectCtx, h.state, ResourceID, current)
	})
	if err != nil || effectInvoked {
		return err
	}

	current, found, err := lifecycle.ReadDesired(ctx, h.state)
	if err != nil {
		return err
	}
	if found {
		return requestRefusal(current.Mode)
	}
	return status.Error(codes.Unavailable, "Runner desired state is not initialized")
}

func requestRefusal(mode lifecycle.Mode) error {
	if mode == lifecycle.Closing {
		return status.Error(codes.Unavailable, "runner is closing")
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
	if _, err := safe.StateWatchFor[*typed.Resource[Runtime, componentExtension[Runtime]]](
		waitCtx,
		h.state,
		componentResource(ResourceID, Runtime{}).Metadata(),
		state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			current, ok := value.(*typed.Resource[Runtime, componentExtension[Runtime]])
			if !ok {
				return false, fmt.Errorf("unexpected bootstrap runtime resource %T", value)
			}
			return isTerminal(current.TypedSpec().Phase), nil
		}),
	); err != nil {
		return err
	}
	runtime, found, err := ReadRuntime(waitCtx, h.state)
	if err != nil {
		return err
	}
	if !found {
		return errors.New(operationHasNoOutcome)
	}
	switch runtime.Phase {
	case Succeeded:
		return nil
	case Interrupted:
		if runtime.Error != "" && runtime.Error != interruptedMessage {
			return errors.New(runtime.Error)
		}
		return ErrInterrupted
	case Failed:
		if runtime.Error == selectionConflict {
			return status.Error(codes.AlreadyExists, runtime.Error)
		}
		if runtime.Error != "" {
			return errors.New(runtime.Error)
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
	input, found, err := ReadInput(ctx, h.state)
	if err != nil {
		return false, err
	}
	if !found || !input.Requested {
		return true, nil
	}
	runtime, found, err := ReadRuntime(ctx, h.state)
	if err != nil {
		return false, err
	}
	return found && isTerminal(runtime.Phase), nil
}

func (h *Host) RuntimeState(ctx context.Context) (Runtime, bool, error) {
	return ReadRuntime(ctx, h.state)
}

func (h *Host) CloseLog() error {
	h.logMu.Lock()
	log := h.log
	h.log = nil
	h.logMu.Unlock()
	if log == nil {
		return nil
	}
	return log.Close()
}

func (h *Host) setLog(log *Log) {
	h.logMu.Lock()
	h.log = log
	h.logMu.Unlock()
}

func (h *Host) startOperation(work func(context.Context) error) {
	h.mu.Lock()
	if h.operation != nil {
		h.mu.Unlock()
		return
	}
	op := &operation{id: uuid.NewString(), done: make(chan struct{})}
	h.operation = op
	h.workers.Add(1)
	h.mu.Unlock()

	if err := publish(h.ctx, h.state, op.id, operationResult{}); err != nil {
		h.onError(err)
	}
	h.runtimeChanged(true)
	go func() {
		defer h.workers.Done()
		ctx, cancel := context.WithCancel(h.ctx)
		defer cancel()
		err := work(ctx)
		h.mu.Lock()
		op.err = err
		if publishErr := publish(h.ctx, h.state, op.id, operationResult{Done: true}); publishErr != nil {
			h.onError(publishErr)
		}
		close(op.done)
		h.mu.Unlock()
		h.runtimeChanged(false)
	}()
}

func (h *Host) takeOperation(ctx context.Context) (*operation, bool, error) {
	h.mu.Lock()
	op := h.operation
	if op == nil {
		h.mu.Unlock()
		return nil, false, nil
	}
	select {
	case <-op.done:
	default:
		h.mu.Unlock()
		return nil, false, nil
	}
	h.mu.Unlock()

	err := h.state.Destroy(ctx, resource.NewMetadata(lifecycle.Namespace, OperationType, op.id, resource.VersionUndefined), state.WithDestroyOwner(HostOwner))
	if err != nil && !state.IsNotFoundError(err) {
		return nil, false, err
	}
	h.mu.Lock()
	if h.operation == op {
		h.operation = nil
	}
	h.mu.Unlock()
	return op, true, nil
}

func (h *Host) runtimeChanged(changed bool) {
	if changed && h.onRuntimeChange != nil {
		h.onRuntimeChange()
	}
}

func (h *Host) runBootstrap(ctx context.Context) error {
	script := h.config.Script
	if script == nil {
		return errors.New("bootstrap is not configured")
	}
	if len(*script) == 0 || len(*script) > maximumBootstrapBytes {
		return errors.New("bootstrap must contain 1..65536 UTF-8 bytes")
	}
	root := filepath.Join(h.config.StateDir, "initialization")
	if err := retained.EnsureDirectory(root); err != nil {
		return err
	}
	digest := sha256.Sum256([]byte(*script))
	metadata := struct {
		SHA string `json:"script_sha256"`
	}{hex.EncodeToString(digest[:])}
	requestPath := filepath.Join(root, "request.json")
	raw, err := os.ReadFile(requestPath)
	selected := errors.Is(err, os.ErrNotExist)
	if err != nil && !selected {
		return err
	}
	if selected {
		encoded, err := json.Marshal(metadata)
		if err != nil {
			return err
		}
		if err := retained.WriteDurable(requestPath, encoded); err != nil {
			return err
		}
	} else {
		var previous struct {
			SHA string `json:"script_sha256"`
		}
		if err := decodeSelection(raw, &previous); err != nil {
			return err
		}
		if previous.SHA != metadata.SHA {
			return status.Error(codes.AlreadyExists, selectionConflict)
		}
	}
	log, err := OpenLog(filepath.Join(root, "events.jsonl"))
	if err != nil {
		return err
	}
	h.setLog(log)
	if log.Ended() {
		return bootstrapErrorForLog(log)
	}
	if !selected || log.LastSequence() != 0 {
		if err := log.AppendInterrupted(1); err != nil {
			return err
		}
		return ErrInterrupted
	}
	cwd := h.config.WorkingDirectory
	if cwd == "" {
		cwd = h.config.StateDir
	}
	code, err := h.scripts.Run(ctx, ControllerName, *script, cwd, func(stdout bool, data []byte) error {
		stream := runnerpb.InitializationStream_INITIALIZATION_STREAM_STDERR
		if stdout {
			stream = runnerpb.InitializationStream_INITIALIZATION_STREAM_STDOUT
		}
		return log.AppendOutput(1, stream, data)
	})
	if err != nil {
		if appendErr := log.AppendInterrupted(1); appendErr != nil {
			return errors.Join(err, appendErr)
		}
		return errors.Join(ErrInterrupted, err)
	}
	if _, err := log.AppendResult(1, int32(code)); err != nil {
		return err
	}
	if code != 0 {
		return errors.New(failedMessage)
	}
	return nil
}

func bootstrapErrorForLog(log *Log) error {
	if !log.Ended() {
		return errors.New(operationHasNoOutcome)
	}
	last := log.Since(log.LastSequence() - 1)
	if len(last) != 1 {
		return errors.New(operationHasNoOutcome)
	}
	switch {
	case last[0].GetInterrupted() != nil:
		return ErrInterrupted
	case last[0].GetResult() == nil:
		return errors.New(operationHasNoOutcome)
	case last[0].GetResult().ExitCode != 0:
		return errors.New(failedMessage)
	default:
		return nil
	}
}

func decodeSelection(data []byte, value any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return errors.New("unexpected trailing JSON data")
	}
	return nil
}
