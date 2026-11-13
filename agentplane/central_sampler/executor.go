package centralsampler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/tooloutput"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/settings"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
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
	processChangesMu     sync.Mutex
	processChanged       chan struct{}
	rpcTimeout           time.Duration
	initializeTimeout    time.Duration
	reconnectTimeout     time.Duration
	lifetime             context.Context
	sessionStream        executorSessionStream
	sessionCancel        context.CancelFunc
	sessionDone          chan struct{}
	sessionID            string
	sessionGeneration    uint64
	sessionError         error
	closing              bool
}

// NewExecutor restores local executor state without contacting the execution environment.
func NewExecutor(ctx context.Context, store *Store, owner Owner, session *Session, settings samplersettings.Settings) (*Executor, func() error, error) {
	request := &samplerpb.CreateSessionRequest{}
	if err := proto.Unmarshal(session.Configuration, request); err != nil {
		return nil, nil, fmt.Errorf("decode stored sampler Session: %w", err)
	}
	var (
		connection *grpc.ClientConn
		err        error
	)
	if settings.GatewayTarget != "" {
		address, err := settings.GatewayAddress(request.GetExecutor().GetTarget().GetNamespace(), request.GetExecutor().GetTarget().GetName())
		if err != nil {
			return nil, nil, err
		}
		connection, err = bearer.Dial(address, settings.GatewayTokenFile, time.Duration(settings.RPCTimeoutSeconds)*time.Second, settings.GatewayTLS)
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
	executor := &Executor{
		store: store, owner: owner, client: client, connection: connection,
		scope: &executorpb.ExecutionScope{
			Destination: request.GetExecutor(), ExpectedIncarnationUid: request.GetExpectedIncarnationUid(), SessionId: session.ID,
		},
		lifetime: ctx, cwd: request.GetSpec().GetCwd(), codexPath: settings.ExecutorCodex,
		environment: map[string]string{"AGENTPLANE_SESSION_ID": session.ID}, processLocks: map[string]*sync.Mutex{},
		rpcTimeout:        time.Duration(settings.RPCTimeoutSeconds) * time.Second,
		initializeTimeout: settings.ExecutorInitializeTimeout(),
		reconnectTimeout:  settings.ExecutorReconnectTimeout(),
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
		return &toolError{fmt.Sprintf("workspace preparation did not complete successfully (process session_id %d): %s; the requested operation was not dispatched", sessionID, tooloutput.LossyUTF8(observation.output.WithOmissionMarker()))}
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
	result := &applyPatchResult{Output: tooloutput.LossyUTF8(observation.output.WithOmissionMarker()), ExitCode: observation.exitCode}
	if !observation.exited {
		result.SessionID = &sessionID
	}
	return result, nil
}
