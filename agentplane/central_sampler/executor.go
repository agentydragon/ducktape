package centralsampler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"math/rand/v2"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

type Executor struct {
	store                *Store
	owner                Owner
	client               executorClient
	connection           *grpc.ClientConn
	scopeMu              sync.RWMutex
	scope                *executorpb.ExecutionScope
	cwd                  string
	codexPath            string
	environment          map[string]string
	shell                *execShell
	prepareMu            sync.Mutex
	workspacePrepared    bool
	rpcMu                sync.Mutex
	sessionStateMu       sync.Mutex
	dispatchMu           sync.Mutex
	stopping             bool
	reconnectRequired    bool
	lostSessionConfirmed bool
	processMu            sync.Mutex
	processLocks         map[string]*sync.Mutex
	rpcTimeout           time.Duration
	initializeTimeout    time.Duration
	lifetime             context.Context
	sessionStream        executorSessionStream
	sessionCancel        context.CancelFunc
	sessionDone          chan struct{}
	sessionID            string
	sessionGeneration    uint64
	sessionError         error
	closing              bool
}

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

// NewExecutor restores local executor state without contacting the execution environment.
func NewExecutor(ctx context.Context, store *Store, owner Owner, session *Session, settings Settings) (*Executor, func() error, error) {
	request := &samplerpb.OpenSessionRequest{}
	if err := proto.Unmarshal(session.Configuration, request); err != nil {
		return nil, nil, fmt.Errorf("decode stored sampler Session: %w", err)
	}
	var (
		connection *grpc.ClientConn
		err        error
	)
	if settings.GatewayTarget != "" {
		address, err := settings.GatewayAddress(request.GetExecutor().GetOwner().GetNamespace(), request.GetExecutor().GetSandbox())
		if err != nil {
			return nil, nil, err
		}
		connection, err = bearer.Dial(address, settings.GatewayTokenFile, time.Duration(settings.RPCTimeoutSeconds)*time.Second)
		if err != nil {
			return nil, nil, err
		}
	} else {
		connection, err = grpc.NewClient(
			settings.DirectExecutorTarget,
			grpc.WithTransportCredentials(insecure.NewCredentials()),
			grpc.WithDisableRetry(),
		)
		if err != nil {
			return nil, nil, err
		}
	}
	client := typedExecutorClient{ExecutorClient: executorpb.NewExecutorClient(connection)}
	env, err := json.Marshal(map[string]any{"destination_ref": map[string]any{
		"namespace": request.GetExecutor().GetOwner().GetNamespace(), "name": request.GetExecutor().GetSandbox(), "uid": request.GetExecutor().GetSandboxUid(),
	}, "session_id": session.ID})
	if err != nil {
		_ = connection.Close()
		return nil, nil, err
	}
	executor := &Executor{
		store: store, owner: owner, client: client, connection: connection,
		scope: &executorpb.ExecutionScope{
			Destination: request.GetExecutor(), ExpectedIncarnationUid: request.GetExpectedIncarnationUid(), SessionId: session.ID,
		},
		lifetime: ctx, cwd: request.GetSpec().GetCwd(), codexPath: settings.ExecutorCodex,
		environment: map[string]string{"AGENTPLANE_DESTINATION": string(env)}, processLocks: map[string]*sync.Mutex{},
		rpcTimeout:        time.Duration(settings.RPCTimeoutSeconds) * time.Second,
		initializeTimeout: settings.ExecutorInitializeTimeout(),
		workspacePrepared: session.WorkspacePrepared,
	}
	state, err := store.ExecutionState(ctx, owner)
	if err != nil {
		_ = connection.Close()
		return nil, nil, err
	}
	conversation, err := store.Read(ctx, owner)
	if err != nil {
		_ = connection.Close()
		return nil, nil, err
	}
	needsRemote := false
	for _, process := range state.Processes {
		if process.Status != protocol.ProcessExited && process.Status != protocol.ProcessExitedUnread && process.Status != protocol.ProcessAbandoned {
			needsRemote = true
		}
	}
	if needsRemote {
		if conversation.ExecutorSession == nil {
			_ = connection.Close()
			return nil, nil, &protocol.UnknownEffectError{Reason: "live execution handles have no retained execution session; session substitution is unsafe"}
		}
		executor.sessionID = *conversation.ExecutorSession
		executor.reconnectRequired = true
	}
	return executor, executor.close, nil
}

// prepareWorkspace acquires execution only for an operation that needs it. The JavaScript host
// and model continuation do not depend on the executor being reachable.
func (e *Executor) prepareWorkspace(ctx context.Context) error {
	e.prepareMu.Lock()
	defer e.prepareMu.Unlock()
	e.rpcMu.Lock()
	err := e.ensureSessionLocked(ctx, executorMethodProcessStart, "")
	e.rpcMu.Unlock()
	if err != nil {
		return err
	}
	if e.workspacePrepared {
		return nil
	}
	processID, sessionID, unlock, err := e.startProcess(ctx, []string{"/bin/sh", "-c", "mkdir -p -- " + shellQuote(e.cwd)}, "/", e.environment, false)
	if err != nil {
		return err
	}
	observation, err := e.readProcess(ctx, processID, 30000)
	unlock()
	if err != nil {
		return err
	}
	if observation.exitCode == nil || *observation.exitCode != 0 {
		return &toolError{fmt.Sprintf("workspace preparation did not complete successfully (process session_id %d): %s; the requested operation was not dispatched", sessionID, lossyUTF8(observation.output.withOmissionMarker()))}
	}
	if err := e.store.PreparedWorkspace(ctx, e.owner, e.scopeSnapshot().GetSessionId()); err != nil {
		return err
	}
	e.workspacePrepared = true
	return nil
}

func (e *Executor) Invoke(ctx context.Context, name string, arguments json.RawMessage) (any, error) {
	var result any
	var err error
	switch name {
	case "exec_command":
		result, err = e.execCommand(ctx, arguments)
	case "write_stdin":
		result, err = e.writeStdin(ctx, arguments)
	case "apply_patch":
		result, err = e.applyPatch(ctx, arguments)
	case "view_image":
		result, err = e.viewImage(ctx, arguments)
	default:
		return nil, fmt.Errorf("unsupported nested tool %q", name)
	}
	if err != nil {
		return nil, e.outcomeToolError(ctx, err)
	}
	return result, nil
}

func (e *Executor) outcomeToolError(ctx context.Context, err error) error {
	if ctx.Err() != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
	}
	// Resume failure wraps a transport error, but settlement belongs to this operation and its process.
	var notDispatched *executorNotDispatchedError
	if errors.As(err, &notDispatched) {
		// ensureSessionLocked rejected this operation before recording or sending its
		// request. Without an associated process there is no prior process effect to
		// reconcile; an existing process keeps its prior unknown/running status.
		outcome := NestedNotDispatched
		var afterSequence *uint64
		if notDispatched.processID != "" {
			process, processErr := e.store.Process(ctx, e.owner, notDispatched.processID)
			if processErr != nil {
				return fmt.Errorf("inspect process %s after failed executor reconnect: %w", notDispatched.processID, processErr)
			}
			outcome = NestedUnknown
			if process.Status == protocol.ProcessStarting {
				outcome = NestedNotDispatched
			}
			afterSequence = notDispatched.afterSequence
			if afterSequence == nil {
				afterSequence = process.AfterSequence
			}
			if outcome == NestedNotDispatched {
				if settleErr := e.store.ProcessResult(ctx, e.owner, notDispatched.processID, afterSequence, protocol.ProcessExited, nil, true); settleErr != nil {
					return fmt.Errorf("settle not-dispatched process %s after failed executor reconnect: %w", notDispatched.processID, settleErr)
				}
			} else if abandonErr := e.store.AbandonProcess(ctx, e.owner, notDispatched.processID, afterSequence, notDispatched.output, outcome, notDispatched.retainedSession); abandonErr != nil {
				return fmt.Errorf("record process %s after failed executor reconnect: %w", notDispatched.processID, abandonErr)
			}
		}
		failure := notDispatched.executionFailureRecord(outcome)
		failure.environmentState = executionEnvironmentStateStringerFromError(notDispatched.cause)
		if notDispatched.processID != "" {
			failure.afterSequence = afterSequence
		}
		message, encodeErr := executionFailureMessage(failure)
		if encodeErr != nil {
			return fmt.Errorf("encode observed process connection failure: %w", encodeErr)
		}
		evidence := failure
		evidence.phase = ""
		return &toolOutcomeError{toolError: &toolError{message}, evidence: evidence.intentEvidence()}
	}
	var failureSource executionFailureSource
	if errors.As(err, &failureSource) {
		failure := failureSource.executionFailureRecord()
		if settleErr := e.settleExecutionFailure(ctx, failure); settleErr != nil {
			return settleErr
		}
		message, encodeErr := executionFailureMessage(failure)
		if encodeErr != nil {
			return fmt.Errorf("encode structured execution failure: %w", encodeErr)
		}
		return &toolOutcomeError{toolError: &toolError{message}, evidence: failure.toolEvidence()}
	}
	var transport *executorTransportError
	if errors.As(err, &transport) {
		if transport.processID != "" {
			if abandonErr := e.store.AbandonProcess(ctx, e.owner, transport.processID, transport.afterSequence, transport.output, NestedUnknown, transport.retainedSession); abandonErr != nil {
				return fmt.Errorf("record unknown process %s outcome after %s transport failure: %w", transport.processID, transport.method, abandonErr)
			}
		}
		message, encodeErr := observedProcessFailureMessage(transport)
		if encodeErr != nil {
			return fmt.Errorf("encode observed process failure: %w", encodeErr)
		}
		return &toolOutcomeError{toolError: &toolError{message}, evidence: transport.evidence(transport.retainedSession)}
	}
	var rejected *executorRejectedError
	if errors.As(err, &rejected) {
		if rejected.method == executorMethodProcessRead {
			if rejected.processID == "" {
				return errors.New("settle rejected process/read without a process ID")
			}
			process, processErr := e.store.Process(ctx, e.owner, rejected.processID)
			if processErr != nil {
				return fmt.Errorf("inspect process %s after rejected process/read: %w", rejected.processID, processErr)
			}
			if process.Status == protocol.ProcessExited || process.Status == protocol.ProcessExitedUnread {
				return &toolError{rejected.Error()}
			}
			if process.Status != protocol.ProcessAbandoned {
				afterSequence := rejected.afterSequence
				if afterSequence == nil {
					afterSequence = process.AfterSequence
				}
				if abandonErr := e.store.AbandonProcess(ctx, e.owner, rejected.processID, afterSequence, rejected.output, NestedUnknown, rejected.executionSessionID); abandonErr != nil {
					return fmt.Errorf("record unknown process %s outcome after rejected process/read: %w", rejected.processID, abandonErr)
				}
			}
			message := rejected.Error() + "; the process handle was abandoned with unknown outcome and was not replayed"
			return &toolOutcomeError{
				toolError: &toolError{message},
				evidence: rejectedProcessReadEvidence{
					Error: message, ExecutorIntentID: rejected.intentID,
					ProcessID: rejected.processID, ExecutionSessionID: rejected.executionSessionID,
					RemoteOutcome: NestedUnknown, AutomaticReplay: false,
				},
			}
		}
		return &toolError{rejected.Error()}
	}
	return err
}

func (e *Executor) settleExecutionFailure(ctx context.Context, failure executionFailureRecord) error {
	if failure.processID == "" {
		return nil
	}
	process, err := e.store.Process(ctx, e.owner, failure.processID)
	if err != nil {
		return fmt.Errorf("inspect process %s after structured execution failure: %w", failure.processID, err)
	}
	if failure.method == executorMethodProcessStart && failure.remoteOutcome == NestedNotDispatched && process.Status == protocol.ProcessStarting {
		if err := e.store.ProcessResult(ctx, e.owner, failure.processID, failure.afterSequence, protocol.ProcessExited, nil, true); err != nil {
			return fmt.Errorf("settle process %s after known not-dispatched start: %w", failure.processID, err)
		}
		return nil
	}
	if failure.remoteOutcome != NestedUnknown || process.Status == protocol.ProcessAbandoned || process.Status == protocol.ProcessExited || process.Status == protocol.ProcessExitedUnread {
		return nil
	}
	if err := e.store.AbandonProcess(ctx, e.owner, failure.processID, failure.afterSequence, failure.output, NestedUnknown, failure.executionSessionID); err != nil {
		return fmt.Errorf("record unknown process %s outcome after structured execution failure: %w", failure.processID, err)
	}
	return nil
}

func executionEnvironmentStateStringerFromError(err error) fmt.Stringer {
	var environment *executorEnvironmentFailureError
	if errors.As(err, &environment) {
		return &environment.state
	}
	return nil
}

// Codex rust-v0.162.1 core/src/tools/handlers/unified_exec/exec_command.rs handle_call and
// core/src/unified_exec/process_manager.rs exec_command_inner.
func (e *Executor) execCommand(ctx context.Context, arguments json.RawMessage) (*unifiedExecResult, error) {
	args, err := parseExecCommand(arguments)
	if err != nil {
		return nil, err
	}
	ctx = withExpectedIncarnationUID(ctx, args.ExpectedIncarnationUID)
	e.rpcMu.Lock()
	shell := e.shell
	e.rpcMu.Unlock()
	if shell != nil {
		if _, err := args.plan(e.cwd, shell); err != nil {
			return nil, err
		}
	}
	if err := e.prepareWorkspace(ctx); err != nil {
		return nil, err
	}
	e.rpcMu.Lock()
	plan, err := planExecCommand(arguments, e.cwd, e.shell)
	e.rpcMu.Unlock()
	if err != nil {
		return nil, err
	}
	environment := maps.Clone(e.environment)
	maps.Copy(environment, unifiedExecEnvironment)
	processID, sessionID, unlock, err := e.startProcess(ctx, plan.argv, plan.cwd, environment, plan.tty)
	if err != nil {
		return nil, err
	}
	defer unlock()
	observation, err := e.readProcess(ctx, processID, plan.yieldMS)
	if err != nil {
		return nil, err
	}
	return observation.unifiedExecResult(&sessionID, plan.maxOutputTokens, newChunkID()), nil
}

// applyPatchResult is what a nested apply_patch call resolves to in JavaScript.
type applyPatchResult struct {
	Output    string `json:"output"`
	ExitCode  *int   `json:"exit_code"`
	SessionID *int   `json:"session_id,omitempty"`
}

func (e *Executor) applyPatch(ctx context.Context, arguments json.RawMessage) (*applyPatchResult, error) {
	var patch string
	if err := json.Unmarshal(arguments, &patch); err != nil || bytes.Equal(bytes.TrimSpace(arguments), []byte("null")) {
		return nil, &toolError{"apply_patch takes a patch string"}
	}
	if err := e.prepareWorkspace(ctx); err != nil {
		return nil, err
	}
	processID, sessionID, unlock, err := e.startProcess(ctx, []string{e.codexPath, "--codex-run-as-apply-patch", patch}, e.cwd, e.environment, false)
	if err != nil {
		return nil, err
	}
	defer unlock()
	observation, err := e.readProcess(ctx, processID, 60000)
	if err != nil {
		return nil, err
	}
	result := &applyPatchResult{Output: lossyUTF8(observation.output.withOmissionMarker()), ExitCode: observation.exitCode}
	if !observation.exited {
		result.SessionID = &sessionID
	}
	return result, nil
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
		return "", 0, nil, err
	}
	if err := e.store.ProcessResult(ctx, e.owner, processID, nil, protocol.ProcessRunning, nil, false); err != nil {
		lock.Unlock()
		return "", 0, nil, err
	}
	return processID, sessionID, lock.Unlock, nil
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
	defer lock.Unlock()
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
			if process.Status == protocol.ProcessRunning {
				if err := e.dispatchInput(ctx, func() error {
					_, err := executorRPC[executorEmpty](ctx, e, executorMethodProcessSignal, executorSignal{ProcessID: process.ID, Signal: "interrupt"})
					return err
				}); err != nil {
					return nil, err
				}
			}
		} else if process.Status == protocol.ProcessRunning {
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
	if process.Status == protocol.ProcessExitedUnread {
		if err := e.store.ProcessResult(ctx, e.owner, processID, after, protocol.ProcessExited, process.ExitCode, true); err != nil {
			return nil, err
		}
		return &processObservation{output: output, exitCode: process.ExitCode, exited: true}, nil
	}
	start := time.Now()
	deadline := start.Add(time.Duration(yieldMS) * time.Millisecond)
	for {
		waitMS := min(max(int(time.Until(deadline)/time.Millisecond), 0), 1000)
		pageCtx, cancel := context.WithTimeout(ctx, time.Duration(waitMS+eRPCSlackMS)*time.Millisecond)
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
		if !result.Exited && !time.Now().Before(deadline) {
			if err := e.store.ProcessResult(ctx, e.owner, processID, after, protocol.ProcessRunning, nil, true); err != nil {
				return nil, err
			}
			return &processObservation{output: output, wallTime: time.Since(start).Seconds()}, nil
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
		pageCtx, cancel := context.WithTimeout(ctx, timeout)
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
		if len(chunks) > 0 || result.Exited {
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

func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\\''") + "'"
}

func fileURI(path string) string {
	return (&url.URL{Scheme: "file", Path: path}).String()
}

func numberInt64(value any) (int64, bool) {
	switch number := value.(type) {
	case float64:
		return int64(number), number == float64(int64(number))
	case int:
		return int64(number), true
	case int64:
		return number, true
	case json.Number:
		parsed, err := number.Int64()
		return parsed, err == nil
	case uint64:
		return int64(number), number <= uint64(^uint64(0)>>1)
	default:
		return 0, false
	}
}
