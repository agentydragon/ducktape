package runner

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
)

func (r *Runner) requestBootstrap() error {
	if r.config.Launch.Bootstrap == nil {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.lifecycle.publishMu.Lock()
	defer r.lifecycle.publishMu.Unlock()
	input, _, err := lifecycleRead[bootstrapInputSpec](r.ctx, r.lifecycle.state, "bootstrap")
	if err != nil {
		return err
	}
	if input.Requested {
		return nil
	}
	desiredState := r.lifecycle.desiredState()
	if desiredState.Mode != runnerlifecycle.Running {
		if desiredState.Mode == runnerlifecycle.Closing {
			return status.Error(codes.Unavailable, "runner is closing")
		}
		return status.Error(codes.Aborted, "this Sandbox is quiesced")
	}
	input.Requested = true
	return publishLifecycle(r.lifecycle, "bootstrap", input)
}

func (r *Runner) bootstrap(ctx context.Context) error {
	if r.config.Launch.Bootstrap == nil {
		return nil
	}
	if err := r.requestBootstrap(); err != nil {
		return err
	}
	var outcome bootstrapRuntimeSpec
	if err := watchLifecycle(ctx, r.lifecycle, "bootstrap", func(current bootstrapRuntimeSpec) (bool, error) {
		outcome = current
		return isTerminalBootstrapPhase(current.Phase), nil
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

func (r *Runner) bootstrapSettled() bool {
	if r.config.Launch.Bootstrap == nil {
		return true
	}
	input, found, err := lifecycleRead[bootstrapInputSpec](r.ctx, r.lifecycle.state, "bootstrap")
	if err != nil {
		r.fail(err)
		return false
	}
	if !found || !input.Requested {
		return true
	}
	current, found, err := lifecycleRead[bootstrapRuntimeSpec](r.ctx, r.lifecycle.state, "bootstrap")
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
	code, err := r.scripts.Run(ctx, bootstrapControllerName, *script, cwd, func(stdout bool, data []byte) error {
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
