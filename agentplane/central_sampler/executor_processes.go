package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand/v2"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/google/uuid"
)

// rejectedProcessReadEvidence preserves the existing projection for a known exec-server
// read rejection while keeping the values typed until JSON serialization.
type rejectedProcessReadEvidence struct {
	Error              string        `json:"error"`
	ExecutorIntentID   string        `json:"executor_intent_id"`
	ProcessID          string        `json:"process_id"`
	ExecutionSessionID string        `json:"execution_session_id"`
	RemoteOutcome      NestedOutcome `json:"remote_outcome"`
	AutomaticReplay    bool          `json:"automatic_replay"`
}

// startProcess registers a model-visible process under a fresh unified exec session number and
// launches it. The returned unlock releases the process lock held from registration on.
// Codex rust-v0.162.1 core/src/unified_exec/process_manager.rs allocate_process_id: random in
// [1000, 100000), unused by unreleased processes; the exec-server process ID embeds it.
func (e *Executor) startProcess(ctx context.Context, argv []string, cwd string, environment map[string]string, tty bool) (string, int, func(), error) {
	e.dispatchMu.Lock()
	defer e.dispatchMu.Unlock()
	if e.stopping {
		return "", 0, nil, errors.New("process dispatch stopped by interrupt")
	}
	var processID string
	var sessionID int
	var lock *sync.Mutex
	for {
		sessionID = 1_000 + rand.IntN(99_000)
		processID = fmt.Sprintf("%d-%s", sessionID, uuid.NewString())
		lock = e.processLock(processID)
		lock.Lock()
		err := e.store.RegisterModelProcess(ctx, e.owner, processID, sessionID, tty)
		if err == nil {
			break
		}
		lock.Unlock()
		if !errors.Is(err, ErrConflict) {
			return "", 0, nil, err
		}
	}
	_, err := executorRPC[executorStarted](ctx, e, executorMethodProcessStart, executorStart{ProcessID: processID, Argv: argv, Cwd: fileURI(cwd), EnvPolicy: e.envPolicy(), Env: environment, TTY: tty})
	if err != nil {
		var rejected *executorRejectedError
		var fence *executorIncarnationFenceError
		if errors.As(err, &rejected) && rejected.method == executorMethodProcessStart ||
			errors.As(err, &fence) && fence.method == executorMethodProcessStart && fence.remoteOutcome == NestedNotDispatched {
			// No process exists; release its session number.
			if settleErr := e.store.ProcessResult(ctx, e.owner, processID, nil, protocol.ProcessExited, nil, true); settleErr != nil {
				lock.Unlock()
				return "", 0, nil, fmt.Errorf("record rejected process start %s: %w", processID, settleErr)
			}
		}
		lock.Unlock()
		e.signalProcessChange()
		return "", 0, nil, err
	}
	if err := e.store.ProcessResult(ctx, e.owner, processID, nil, protocol.ProcessRunning, nil, false); err != nil {
		lock.Unlock()
		e.signalProcessChange()
		return "", 0, nil, err
	}
	return processID, sessionID, func() {
		lock.Unlock()
		e.signalProcessChange()
	}, nil
}

// envPolicy makes processes inherit the executor's environment, as wrapped Codex's do: Codex's
// default shell environment policy (inherit all, default excludes ignored) with the Session's
// variables set. Without a policy the exec-server passes only `env`, so PATH and HOME are missing.
// Codex rust-v0.162.1 config/src/shell_environment_policy.rs From<ShellEnvironmentPolicyToml>.
func (e *Executor) envPolicy() *executorEnvPolicy {
	return &executorEnvPolicy{Inherit: "all", IgnoreDefaultExcludes: true, Exclude: []string{}, Set: e.environment, IncludeOnly: []string{}}
}

// newChunkID is six random hex digits.
// Codex rust-v0.162.1 core/src/unified_exec/mod.rs generate_chunk_id.
func newChunkID() string {
	return fmt.Sprintf("%06x", rand.IntN(1<<24))
}

func (e *Executor) Setup(ctx context.Context, script string, output func(string, []byte) error) (int, error) {
	if err := e.prepareWorkspace(ctx); err != nil {
		return 0, err
	}
	processID := uuid.NewString()
	if err := e.store.RegisterProcess(ctx, e.owner, processID, false); err != nil {
		return 0, err
	}
	if _, err := executorRPC[executorStarted](ctx, e, executorMethodProcessStart, executorStart{ProcessID: processID, Argv: []string{"/bin/sh", "-eu", "-c", script}, Cwd: fileURI(e.cwd), EnvPolicy: e.envPolicy(), Env: e.environment}); err != nil {
		return 0, e.outcomeToolError(ctx, err)
	}
	if err := e.store.ProcessResult(ctx, e.owner, processID, nil, protocol.ProcessRunning, nil, false); err != nil {
		return 0, err
	}
	var after *uint64
	observed := newHeadTailBuffer(unifiedExecOutputMaxBytes)
	for {
		changed := e.processChanges()
		pageCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		result, err := executorRPC[executorReadResult](pageCtx, e, executorMethodProcessRead, executorRead{ProcessID: processID, AfterSeq: after, MaxBytes: 65536, WaitMS: 1000, ObservedOutput: observed})
		cancel()
		if err != nil {
			return 0, e.outcomeToolError(ctx, err)
		}
		chunks := result.Chunks
		for _, chunk := range chunks {
			observed.push(chunk.Chunk)
			if err := output(chunk.Stream, chunk.Chunk); err != nil {
				return 0, err
			}
		}
		if result.NextSeq > 0 {
			sequence := result.NextSeq - 1
			after = &sequence
		}
		if result.uncertain() {
			_ = e.store.ProcessResult(ctx, e.owner, processID, after, protocol.ProcessUnknown, nil, false)
			return 0, fmt.Errorf("setup process outcome unknown: %v", result.Failure)
		}
		if result.drained() {
			exitCode := result.ExitCode
			if exitCode == nil {
				return 0, errors.New("setup process exited without an exit code")
			}
			if err := e.store.ProcessResult(ctx, e.owner, processID, after, protocol.ProcessExited, exitCode, false); err != nil {
				return 0, err
			}
			return *exitCode, nil
		}
		if len(chunks) == 0 {
			select {
			case <-ctx.Done():
				return 0, ctx.Err()
			case <-changed:
			}
		}
	}
}

// Codex rust-v0.162.1 core/src/tools/handlers/unified_exec/write_stdin.rs handle_call and
// core/src/unified_exec/process_manager.rs write_stdin_inner.
func (e *Executor) writeStdin(ctx context.Context, arguments json.RawMessage) (*unifiedExecResult, error) {
	plan, err := planWriteStdin(arguments)
	if err != nil {
		return nil, err
	}
	process, err := e.store.ProcessByModelSession(ctx, e.owner, plan.sessionID)
	if errors.Is(err, protocol.ErrMissing) {
		return nil, &toolError{fmt.Sprintf("write_stdin failed: Unknown process id %d", plan.sessionID)}
	}
	if err != nil {
		return nil, err
	}
	lock := e.processLock(process.ID)
	lock.Lock()
	defer func() {
		lock.Unlock()
		e.signalProcessChange()
	}()
	// Revalidate under the lock: the number may have been released and reused meanwhile.
	process, err = e.store.ProcessByModelSession(ctx, e.owner, plan.sessionID)
	if errors.Is(err, protocol.ErrMissing) {
		return nil, &toolError{fmt.Sprintf("write_stdin failed: Unknown process id %d", plan.sessionID)}
	}
	if err != nil {
		return nil, err
	}
	if process.Status != protocol.ProcessRunning && process.Status != protocol.ProcessExitedUnread {
		if process.Status == protocol.ProcessAbandoned {
			return nil, &toolError{fmt.Sprintf("write_stdin failed: process %d has an unknown remote outcome and its handle is abandoned; no input was sent", plan.sessionID)}
		}
		return nil, &toolError{fmt.Sprintf("session %d is %s; its remote outcome is unknown", plan.sessionID, process.Status)}
	}
	if plan.chars != "" {
		if !process.TTY {
			if plan.chars != interruptInput {
				return nil, &toolError{"write_stdin failed: " + stdinClosedMessage}
			}
			if process.Status == protocol.ProcessRunning && process.ExitCode == nil {
				if err := e.dispatchInput(ctx, func() error {
					_, err := executorRPC[executorEmpty](ctx, e, executorMethodProcessSignal, executorSignal{ProcessID: process.ID, Signal: "interrupt"})
					return err
				}); err != nil {
					return nil, err
				}
			}
		} else if process.Status == protocol.ProcessRunning && process.ExitCode == nil {
			// A write to an exited terminal is moot; the poll below reports the exit.
			if err := e.dispatchInput(ctx, func() error {
				result, err := executorRPC[executorWriteResult](ctx, e, executorMethodProcessWrite, executorWrite{ProcessID: process.ID, Chunk: []byte(plan.chars), WriteID: uuid.NewString()})
				if err != nil {
					return err
				}
				if result.Status != "accepted" {
					return &toolError{"write_stdin failed: " + writeToStdinFailedMessage}
				}
				return nil
			}); err != nil {
				return nil, err
			}
			// Give the process a brief window to react, as upstream does, before polling.
			select {
			case <-time.After(100 * time.Millisecond):
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
	}
	observation, err := e.readProcess(ctx, process.ID, plan.yieldMS)
	if err != nil {
		return nil, err
	}
	return observation.unifiedExecResult(process.ModelSessionID, plan.maxOutputTokens, newChunkID()), nil
}

func (e *Executor) dispatchInput(ctx context.Context, send func() error) error {
	e.dispatchMu.Lock()
	defer e.dispatchMu.Unlock()
	if e.stopping {
		return errors.New("stdin dispatch stopped by interrupt")
	}
	return send()
}

// readProcess collects output until the process exits or the yield deadline passes. The caller
// holds the process lock.
func (e *Executor) readProcess(ctx context.Context, processID string, yieldMS int) (*processObservation, error) {
	process, err := e.store.Process(ctx, e.owner, processID)
	if err != nil {
		return nil, err
	}
	output, err := restoreHeadTailBuffer(unifiedExecOutputMaxBytes, process.BufferedOutput, process.OmittedBytes)
	if err != nil {
		return nil, err
	}
	after := process.AfterSequence
	exitCode := process.ExitCode
	if process.Status == protocol.ProcessExitedUnread {
		if err := e.store.ProcessResult(ctx, e.owner, processID, after, protocol.ProcessExited, process.ExitCode, true); err != nil {
			return nil, err
		}
		return &processObservation{output: output, exitCode: process.ExitCode, exited: true}, nil
	}
	start := time.Now()
	deadline := start.Add(time.Duration(yieldMS) * time.Millisecond)
	yield := func() (*processObservation, error) {
		if err := e.store.ProcessResult(ctx, e.owner, processID, after, protocol.ProcessRunning, exitCode, true); err != nil {
			return nil, err
		}
		return &processObservation{output: output, exitCode: exitCode, wallTime: time.Since(start).Seconds()}, nil
	}
	for {
		changed := e.processChanges()
		waitMS := min(max(int(time.Until(deadline)/time.Millisecond), 0), 1000)
		attemptTimeout := time.Duration(waitMS+eRPCSlackMS) * time.Millisecond
		pageCtx, cancel := context.WithTimeout(ctx, e.readBudget(attemptTimeout))
		pageCtx = context.WithValue(pageCtx, executorReadAttemptTimeoutKey{}, attemptTimeout)
		result, rpcErr := executorRPC[executorReadResult](pageCtx, e, executorMethodProcessRead, executorRead{ProcessID: processID, AfterSeq: after, MaxBytes: 65536, WaitMS: waitMS, ObservedOutput: output})
		cancel()
		if rpcErr != nil {
			var notDispatched *executorNotDispatchedError
			if errors.As(rpcErr, &notDispatched) {
				notDispatched.afterSequence, notDispatched.output = after, output
			}
			var transport *executorTransportError
			if errors.As(rpcErr, &transport) {
				transport.afterSequence, transport.output = after, output
			}
			var rejected *executorRejectedError
			if errors.As(rpcErr, &rejected) && rejected.method == executorMethodProcessRead {
				rejected.afterSequence, rejected.output = after, output
			}
			var environment *executorEnvironmentFailureError
			if errors.As(rpcErr, &environment) && environment.method == executorMethodProcessRead {
				environment.afterSequence, environment.output = after, output
			}
			var fence *executorIncarnationFenceError
			if errors.As(rpcErr, &fence) && fence.method == executorMethodProcessRead {
				fence.afterSequence, fence.output = after, output
			}
			return nil, rpcErr
		}
		for _, chunk := range result.Chunks {
			output.push(chunk.Chunk)
		}
		if result.NextSeq > 0 {
			seq := result.NextSeq - 1
			after = &seq
		}
		if result.Exited && result.ExitCode != nil {
			exitCode = result.ExitCode
		}
		if result.uncertain() {
			return nil, &executorTransportError{
				method: executorMethodProcessRead, phase: "observation", processID: processID,
				retainedSession: e.retainedSessionID(ctx), afterSequence: after, output: output,
				cause: errors.New("process observation ended without a confirmed outcome: " + describeProcessReadFailure(result)),
			}
		}
		if result.drained() {
			if err := e.store.ProcessResult(ctx, e.owner, processID, after, protocol.ProcessExited, result.ExitCode, true); err != nil {
				return nil, err
			}
			return &processObservation{output: output, exitCode: result.ExitCode, exited: true, wallTime: time.Since(start).Seconds()}, nil
		}
		if !time.Now().Before(deadline) {
			return yield()
		}
		if len(result.Chunks) == 0 {
			timer := time.NewTimer(time.Until(deadline))
			select {
			case <-ctx.Done():
				timer.Stop()
				return nil, ctx.Err()
			case <-changed:
				timer.Stop()
			case <-timer.C:
				return yield()
			}
		}
	}
}

const eRPCSlackMS = 5000

func (e *Executor) ReconcileProcesses(ctx context.Context, timeout time.Duration) (bool, error) {
	state, err := e.store.ExecutionState(ctx, e.owner)
	if err != nil {
		return false, err
	}
	progressed := false
	for _, process := range state.Processes {
		if process.Status == protocol.ProcessExited || process.Status == protocol.ProcessExitedUnread {
			continue
		}
		lock := e.processLock(process.ID)
		if !lock.TryLock() {
			continue
		}
		row, err := e.store.Process(ctx, e.owner, process.ID)
		if err != nil {
			lock.Unlock()
			return progressed, err
		}
		if row.Status == protocol.ProcessAbandoned || row.Status == protocol.ProcessExited || row.Status == protocol.ProcessExitedUnread {
			lock.Unlock()
			continue
		}
		if row.Status != protocol.ProcessRunning {
			lock.Unlock()
			return progressed, fmt.Errorf("cannot reconcile remote process in state %s", row.Status)
		}
		readOutput, err := restoreHeadTailBuffer(unifiedExecOutputMaxBytes, row.BufferedOutput, row.OmittedBytes)
		if err != nil {
			lock.Unlock()
			return progressed, err
		}
		pageCtx, cancel := context.WithTimeout(ctx, e.readBudget(timeout))
		pageCtx = context.WithValue(pageCtx, executorReadAttemptTimeoutKey{}, timeout)
		result, rpcErr := executorRPC[executorReadResult](pageCtx, e, executorMethodProcessRead, executorRead{ProcessID: row.ID, AfterSeq: row.AfterSequence, MaxBytes: 65536, ObservedOutput: readOutput})
		cancel()
		if rpcErr != nil {
			var fence *executorIncarnationFenceError
			if errors.As(rpcErr, &fence) && fence.method == executorMethodProcessRead {
				handled := e.outcomeToolError(ctx, rpcErr)
				var toolFailure *toolError
				if !errors.As(handled, &toolFailure) {
					lock.Unlock()
					return progressed, handled
				}
				progressed = true
				lock.Unlock()
				continue
			}
			var rejected *executorRejectedError
			if errors.As(rpcErr, &rejected) && rejected.method == executorMethodProcessRead {
				handled := e.outcomeToolError(ctx, rpcErr)
				var toolFailure *toolError
				if !errors.As(handled, &toolFailure) {
					lock.Unlock()
					return progressed, handled
				}
				progressed = true
				lock.Unlock()
				continue
			}
			var transport *executorTransportError
			var notDispatched *executorNotDispatchedError
			if errors.As(rpcErr, &transport) || errors.As(rpcErr, &notDispatched) {
				if transport != nil {
					transport.afterSequence = row.AfterSequence
				}
				handled := e.outcomeToolError(ctx, rpcErr)
				var toolOutcome *toolOutcomeError
				if !errors.As(handled, &toolOutcome) {
					lock.Unlock()
					return progressed, handled
				}
				lock.Unlock()
				return true, nil
			}
			var failureSource executionFailureSource
			if errors.As(rpcErr, &failureSource) {
				handled := e.outcomeToolError(ctx, rpcErr)
				var toolOutcome *toolOutcomeError
				if !errors.As(handled, &toolOutcome) {
					lock.Unlock()
					return progressed, handled
				}
				lock.Unlock()
				return true, nil
			}
			lock.Unlock()
			return progressed, rpcErr
		}
		if result.uncertain() {
			output, err := restoreHeadTailBuffer(unifiedExecOutputMaxBytes, row.BufferedOutput, row.OmittedBytes)
			if err != nil {
				lock.Unlock()
				return progressed, err
			}
			for _, chunk := range result.Chunks {
				output.push(chunk.Chunk)
			}
			after := row.AfterSequence
			if result.NextSeq > 0 {
				sequence := result.NextSeq - 1
				after = &sequence
			}
			failure := &executorTransportError{
				method: executorMethodProcessRead, phase: "observation", processID: row.ID,
				retainedSession: e.retainedSessionID(ctx), afterSequence: after, output: output,
				cause: errors.New("process observation ended without a confirmed outcome: " + describeProcessReadFailure(result)),
			}
			handled := e.outcomeToolError(ctx, failure)
			lock.Unlock()
			if !errors.As(handled, new(*toolOutcomeError)) {
				return progressed, handled
			}
			return true, nil
		}
		chunks := result.Chunks
		output := result.output()
		afterValue := result.NextSeq
		newExit := result.Exited && result.ExitCode != nil && (row.ExitCode == nil || *row.ExitCode != *result.ExitCode)
		if len(chunks) > 0 || result.drained() || newExit {
			seq := uint64(0)
			if afterValue > 0 {
				seq = uint64(afterValue - 1)
			}
			exitCode := result.ExitCode
			if err := e.store.RetainProcessOutput(ctx, e.owner, row.ID, seq, output, result.drained(), exitCode); err != nil {
				lock.Unlock()
				return progressed, err
			}
			progressed = true
		}
		lock.Unlock()
	}
	return progressed, nil
}

func (e *Executor) InterruptAll(ctx context.Context) (map[string]any, error) {
	e.dispatchMu.Lock()
	e.stopping = true
	state, err := e.store.ExecutionState(ctx, e.owner)
	e.dispatchMu.Unlock()
	if err != nil {
		return nil, err
	}
	outcomes := map[string]any{}
	for _, process := range state.Processes {
		if process.Status == protocol.ProcessExitedUnread || process.Status == protocol.ProcessExited {
			continue
		}
		intent, err := e.store.RecordNested(ctx, e.owner, IntentInterruptProcess, map[string]any{"session_id": process.ID})
		if err != nil {
			return outcomes, err
		}
		requestCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		_, terminateErr := executorRPC[executorTerminateResult](requestCtx, e, executorMethodProcessTerminate, executorTerminate{ProcessID: process.ID})
		cancel()
		if terminateErr == nil {
			lock := e.processLock(process.ID)
			lock.Lock()
			result, readErr := e.readProcess(ctx, process.ID, 3000)
			lock.Unlock()
			if readErr == nil && result.exited {
				outcomes[process.ID] = map[string]any{"exit_observed": true, "result": map[string]any{
					"output": lossyUTF8(result.output.withOmissionMarker()), "exit_code": result.exitCode,
				}}
				if err := e.store.FinishNested(ctx, e.owner, intent, outcomes[process.ID].(map[string]any)); err != nil {
					return outcomes, err
				}
				continue
			}
			terminateErr = readErr
		}
		outcomes[process.ID] = map[string]any{"exit_observed": false, "effects_unknown": true, "error": fmt.Sprint(terminateErr)}
	}
	return outcomes, nil
}

func (e *Executor) processLock(id string) *sync.Mutex {
	e.processMu.Lock()
	defer e.processMu.Unlock()
	if e.processLocks[id] == nil {
		e.processLocks[id] = &sync.Mutex{}
	}
	return e.processLocks[id]
}

// processChanges broadcasts executor stream changes to foreground readers and the idle driver.
// The channel is replaced after every change, so a subscriber can take a snapshot before reading
// and cannot miss a notification that races that read.
func (e *Executor) processChanges() <-chan struct{} {
	e.processChangesMu.Lock()
	defer e.processChangesMu.Unlock()
	if e.processChanged == nil {
		e.processChanged = make(chan struct{})
	}
	return e.processChanged
}

func (e *Executor) signalProcessChange() {
	e.processChangesMu.Lock()
	defer e.processChangesMu.Unlock()
	if e.processChanged != nil {
		close(e.processChanged)
	}
	e.processChanged = make(chan struct{})
}

func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\\''") + "'"
}

func fileURI(path string) string {
	return (&url.URL{Scheme: "file", Path: path}).String()
}
