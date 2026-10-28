package runner

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"

	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/siderolabs/gen/optional"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
)

const (
	bootstrapInputType      = "BootstrapInputs.agentplane"
	bootstrapRuntimeType    = "BootstrapRuntimes.agentplane"
	bootstrapControllerName = "bootstrap-lifecycle"
)

var errBootstrapInterrupted = errors.New(bootstrapInterruptedError)

type bootstrapInputSpec struct {
	Requested bool
	Demand    uint64
}

type bootstrapPhase uint8

const (
	bootstrapDormant bootstrapPhase = iota
	bootstrapRunning
	bootstrapSucceeded
	bootstrapFailed
	bootstrapInterrupted
)

type bootstrapRuntimeSpec struct {
	Phase bootstrapPhase
	Error string
}

const (
	bootstrapSelectionConflict = "sandbox already selected a different bootstrap script"
	bootstrapInterruptedError  = "the Sandbox bootstrap was interrupted and never runs again; use a new Sandbox"
	bootstrapFailedError       = "the Sandbox bootstrap failed and never runs again; use a new Sandbox"
)

type bootstrapLifecycleController struct{ l *lifecycle }

func (*bootstrapLifecycleController) Name() string { return bootstrapControllerName }

func (*bootstrapLifecycleController) Settings() controller.QSettings {
	return controller.QSettings{
		Inputs: []controller.Input{
			{Namespace: lifecycleNamespace, Type: bootstrapInputType, Kind: controller.InputQPrimary},
			{Namespace: lifecycleNamespace, Type: runnerGoalType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: runnerInputType, Kind: controller.InputQMapped},
			{Namespace: lifecycleNamespace, Type: operationResultType, Kind: controller.InputQMapped},
		},
		Outputs:     []controller.Output{{Type: bootstrapRuntimeType, Kind: controller.OutputExclusive}},
		Concurrency: optional.Some(uint(1)),
	}
}

func (c *bootstrapLifecycleController) MapInput(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr controller.ReducedResourceMetadata) ([]resource.Pointer, error) {
	switch ptr.Type() {
	case runnerGoalType, runnerInputType:
		return []resource.Pointer{resource.NewMetadata(lifecycleNamespace, bootstrapInputType, "bootstrap", resource.VersionUndefined)}, nil
	case operationResultType:
		result, found, err := lifecycleRead[operationResultSpec](ctx, r, operationResultType, ptr.ID())
		if err != nil || !found || result.Kind != bootstrapControllerName {
			return nil, err
		}
		return []resource.Pointer{resource.NewMetadata(lifecycleNamespace, bootstrapInputType, "bootstrap", resource.VersionUndefined)}, nil
	default:
		return nil, nil
	}
}

func (c *bootstrapLifecycleController) Reconcile(ctx context.Context, _ *zap.Logger, r controller.QRuntime, ptr resource.Pointer) error {
	input, found, err := lifecycleRead[bootstrapInputSpec](ctx, r, bootstrapInputType, ptr.ID())
	if err != nil || !found {
		return err
	}
	current, _, err := lifecycleRead[bootstrapRuntimeSpec](ctx, r, bootstrapRuntimeType, ptr.ID())
	if err != nil {
		return err
	}
	write := func() error {
		changed, err := lifecycleWrite(ctx, r, bootstrapRuntimeType, ptr.ID(), current)
		if changed {
			c.l.runner.changed()
		}
		return err
	}

	if op, done := c.l.takeHostOperation(&c.l.runner.bootstrapMu, &c.l.runner.bootstrapOp); done {
		if op.err == nil {
			current = bootstrapRuntimeSpec{Phase: bootstrapSucceeded}
		} else if errors.Is(op.err, errBootstrapInterrupted) {
			current = bootstrapRuntimeSpec{Phase: bootstrapInterrupted, Error: op.err.Error()}
		} else if status.Code(op.err) == codes.AlreadyExists {
			current = bootstrapRuntimeSpec{Phase: bootstrapFailed, Error: bootstrapSelectionConflict}
		} else {
			current = bootstrapRuntimeSpec{Phase: bootstrapFailed, Error: op.err.Error()}
		}
		return write()
	}
	if isTerminalBootstrapPhase(current.Phase) || !input.Requested || input.Demand == 0 {
		return write()
	}
	if current.Phase == bootstrapDormant {
		current = bootstrapRuntimeSpec{Phase: bootstrapRunning}
		if err := write(); err != nil {
			return err
		}
		c.l.startHostOperation(&c.l.runner.bootstrapMu, &c.l.runner.bootstrapOp, bootstrapControllerName, c.l.runner.runBootstrap)
	}
	return nil
}

func (r *Runner) requestBootstrap() error {
	if r.config.Launch.Bootstrap == nil {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.lifecycle.publishMu.Lock()
	defer r.lifecycle.publishMu.Unlock()
	input, _, err := lifecycleRead[bootstrapInputSpec](r.ctx, r.lifecycle.state, bootstrapInputType, "bootstrap")
	if err != nil {
		return err
	}
	if input.Requested {
		return nil
	}
	goal := r.lifecycle.goal()
	if goal.Mode != runnerRunning {
		if goal.Mode == runnerClosing {
			return status.Error(codes.Unavailable, "runner is closing")
		}
		return status.Error(codes.Aborted, "this Sandbox is quiesced")
	}
	input.Requested = true
	input.Demand++
	return publishLifecycle(r.lifecycle, bootstrapInputType, "bootstrap", input)
}

func (r *Runner) bootstrap(ctx context.Context) error {
	if r.config.Launch.Bootstrap == nil {
		return nil
	}
	if err := r.requestBootstrap(); err != nil {
		return err
	}
	var outcome bootstrapRuntimeSpec
	if err := r.lifecycle.waitFor(ctx, func() (bool, error) {
		var found bool
		var err error
		outcome, found, err = lifecycleRead[bootstrapRuntimeSpec](r.ctx, r.lifecycle.state, bootstrapRuntimeType, "bootstrap")
		if err != nil {
			return true, err
		}
		return found && isTerminalBootstrapPhase(outcome.Phase), nil
	}); err != nil {
		return err
	}
	switch outcome.Phase {
	case bootstrapSucceeded:
		return nil
	case bootstrapInterrupted:
		if outcome.Error != "" && outcome.Error != bootstrapInterruptedError {
			return errors.New(outcome.Error)
		}
		return errBootstrapInterrupted
	case bootstrapFailed:
		if outcome.Error == bootstrapSelectionConflict {
			return status.Error(codes.AlreadyExists, outcome.Error)
		}
		if outcome.Error != "" {
			return errors.New(outcome.Error)
		}
		return errors.New(bootstrapFailedError)
	default:
		return errors.New("bootstrap has no terminal outcome")
	}
}

func isTerminalBootstrapPhase(phase bootstrapPhase) bool {
	return phase == bootstrapSucceeded || phase == bootstrapFailed || phase == bootstrapInterrupted
}

func (r *Runner) bootstrapSettled() bool {
	if r.config.Launch.Bootstrap == nil {
		return true
	}
	input, found, err := lifecycleRead[bootstrapInputSpec](r.ctx, r.lifecycle.state, bootstrapInputType, "bootstrap")
	if err != nil {
		r.fail(err)
		return false
	}
	if !found || !input.Requested {
		return true
	}
	current, found, err := lifecycleRead[bootstrapRuntimeSpec](r.ctx, r.lifecycle.state, bootstrapRuntimeType, "bootstrap")
	if err != nil {
		r.fail(err)
		return false
	}
	return found && isTerminalBootstrapPhase(current.Phase)
}

func (r *Runner) runBootstrap(ctx context.Context) error {
	script := r.config.Launch.Bootstrap
	if script == nil {
		return errors.New("bootstrap is not configured")
	}
	if len(*script) == 0 || len(*script) > 65536 {
		return errors.New("bootstrap must contain 1..65536 UTF-8 bytes")
	}
	root := filepath.Join(r.config.StateDir, "initialization")
	if err := makeDirectory(root); err != nil {
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
		if err := writeDurable(requestPath, encoded); err != nil {
			return err
		}
	} else {
		var previous struct {
			SHA string `json:"script_sha256"`
		}
		if err := DecodeStrict(raw, &previous); err != nil {
			return err
		}
		if previous.SHA != metadata.SHA {
			return status.Error(codes.AlreadyExists, bootstrapSelectionConflict)
		}
	}
	log, err := OpenInitializationLog(filepath.Join(root, "events.jsonl"))
	if err != nil {
		return err
	}
	r.bootstrapMu.Lock()
	r.initLog = log
	r.bootstrapMu.Unlock()
	if log.Ended() {
		return bootstrapErrorForLog(log)
	}
	if !selected || log.LastSequence() != 0 {
		if err := log.AppendInterrupted(1); err != nil {
			return err
		}
		return errBootstrapInterrupted
	}
	cwd := r.config.InitializationCwd
	if cwd == "" {
		cwd = r.config.StateDir
	}
	code, err := r.runScript(ctx, *script, cwd, func(stdout bool, data []byte) error {
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
		return errors.Join(errBootstrapInterrupted, err)
	}
	if _, err := log.AppendResult(1, int32(code)); err != nil {
		return err
	}
	if code != 0 {
		return errors.New(bootstrapFailedError)
	}
	return nil
}

func bootstrapErrorForLog(log *InitializationLog) error {
	if !log.Ended() {
		return errors.New("bootstrap has no terminal outcome")
	}
	last := log.Since(log.LastSequence() - 1)
	if len(last) != 1 {
		return errors.New("bootstrap has no terminal outcome")
	}
	switch {
	case last[0].GetInterrupted() != nil:
		return errBootstrapInterrupted
	case last[0].GetResult() == nil:
		return errors.New("bootstrap has no terminal outcome")
	case last[0].GetResult().ExitCode != 0:
		return errors.New(bootstrapFailedError)
	default:
		return nil
	}
}
