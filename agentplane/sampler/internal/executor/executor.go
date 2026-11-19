package executor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"sync"
	"sync/atomic"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executionstate"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executor/transport"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/toolcontract"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/tooloutput"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

type Executor struct {
	store             *sessionstore.Store
	owner             sessionstore.Owner
	transport         *transport.Transport
	cwd               string
	codexPath         string
	environment       map[string]string
	prepareMu         sync.Mutex
	workspacePrepared atomic.Bool
	dispatchMu        sync.Mutex
	stopping          bool
	processMu         sync.Mutex
	processLocks      map[string]*sync.Mutex
	processChanges    *processChangeBroadcaster
}

// New restores local executor state without contacting the execution environment.
func New(ctx context.Context, store *sessionstore.Store, owner sessionstore.Owner, session *sessionstore.Session, settings samplersettings.Settings) (*Executor, func() error, error) {
	request := &sessionconfig.Configuration{}
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
	client := executorpb.NewExecutorClient(connection)
	scope := &executorpb.ExecutionScope{
		Destination: request.GetExecutor(), ExpectedIncarnationUid: request.GetExpectedIncarnationUid(), SessionId: session.ID,
	}
	processChanges := &processChangeBroadcaster{}
	executor := &Executor{
		store: store, owner: owner, cwd: request.GetSpec().GetCwd(), codexPath: settings.ExecutorCodex,
		environment: map[string]string{"AGENTPLANE_SESSION_ID": session.ID}, processLocks: map[string]*sync.Mutex{}, processChanges: processChanges,
	}
	executor.workspacePrepared.Store(session.WorkspacePrepared)
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
	initialSessionID := ""
	for _, process := range state.Processes {
		if process.Status != executionstate.ProcessExited && process.Status != executionstate.ProcessExitedUnread && process.Status != executionstate.ProcessAbandoned {
			needsRemote = true
		}
	}
	if needsRemote {
		if conversation.ExecutorSession == nil {
			_ = connection.Close()
			return nil, nil, &protocol.UnknownEffectError{Reason: "live execution handles have no retained execution session; session substitution is unsafe"}
		}
		initialSessionID = *conversation.ExecutorSession
	}
	bindings := &storeBindings{store: store, owner: owner, workspacePrepared: &executor.workspacePrepared}
	executor.transport = transport.New(transport.Config{
		Client: client, Connection: connection, Journal: bindings, Bindings: bindings, ProcessChanges: processChanges,
		Scope: scope, Lifetime: ctx, InitialSessionID: initialSessionID,
		RPCTimeout:        time.Duration(settings.RPCTimeoutSeconds) * time.Second,
		InitializeTimeout: settings.ExecutorInitializeTimeout(), ReconnectTimeout: settings.ExecutorReconnectTimeout(),
	})
	return executor, executor.close, nil
}

// prepareWorkspace acquires execution only for an operation that needs it. The JavaScript host
// and model continuation do not depend on the executor being reachable.
func (e *Executor) prepareWorkspace(ctx context.Context) error {
	e.prepareMu.Lock()
	defer e.prepareMu.Unlock()
	if _, err := operationValue(e.transport.EnsureSession(ctx)); err != nil {
		return err
	}
	if e.workspacePrepared.Load() {
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
		return &toolcontract.Rejection{Message: fmt.Sprintf("workspace preparation did not complete successfully (process session_id %d): %s; the requested operation was not dispatched", sessionID, tooloutput.LossyUTF8(observation.output.WithOmissionMarker()))}
	}
	if err := e.store.PreparedWorkspace(ctx, e.owner, e.transport.ScopeSnapshot().GetSessionId()); err != nil {
		return err
	}
	e.workspacePrepared.Store(true)
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
	if notDispatched := transport.FindFailure(err, transport.FailureNotDispatched); notDispatched != nil {
		// ensureSessionLocked rejected this operation before recording or sending its
		// request. Without an associated process there is no prior process effect to
		// reconcile; an existing process keeps its prior unknown/running status.
		outcome := sessionstore.NestedNotDispatched
		var afterSequence *uint64
		if notDispatched.ProcessID != "" {
			process, processErr := e.store.Process(ctx, e.owner, notDispatched.ProcessID)
			if processErr != nil {
				return fmt.Errorf("inspect process %s after failed executor reconnect: %w", notDispatched.ProcessID, processErr)
			}
			outcome = sessionstore.NestedUnknown
			if process.Status == executionstate.ProcessStarting {
				outcome = sessionstore.NestedNotDispatched
			}
			afterSequence = notDispatched.AfterSequence
			if afterSequence == nil {
				afterSequence = process.AfterSequence
			}
			if outcome == sessionstore.NestedNotDispatched {
				if settleErr := e.store.ProcessResult(ctx, e.owner, notDispatched.ProcessID, afterSequence, executionstate.ProcessExited, nil, true); settleErr != nil {
					return fmt.Errorf("settle not-dispatched process %s after failed executor reconnect: %w", notDispatched.ProcessID, settleErr)
				}
			} else if abandonErr := e.store.AbandonProcess(ctx, e.owner, notDispatched.ProcessID, afterSequence, notDispatched.Output, outcome, notDispatched.ExecutionSessionID); abandonErr != nil {
				return fmt.Errorf("record process %s after failed executor reconnect: %w", notDispatched.ProcessID, abandonErr)
			}
		}
		failure := executionFailureProjection(notDispatched)
		failure.remoteOutcome = outcome
		failure.environmentState = executionEnvironmentStateFromError(notDispatched.Cause)
		if notDispatched.ProcessID != "" {
			failure.afterSequence = afterSequence
		}
		message, encodeErr := executionFailureMessage(failure)
		if encodeErr != nil {
			return fmt.Errorf("encode observed process connection failure: %w", encodeErr)
		}
		evidence := failure
		evidence.phase = ""
		return &ToolOutcomeError{rejection: &toolcontract.Rejection{Message: message}, evidence: evidence.intentEvidence()}
	}
	if source := transport.FindFailure(err, transport.FailureEnvironment, transport.FailureIncarnationFence); source != nil {
		failure := executionFailureProjection(source)
		if settleErr := e.settleExecutionFailure(ctx, failure); settleErr != nil {
			return settleErr
		}
		message, encodeErr := executionFailureMessage(failure)
		if encodeErr != nil {
			return fmt.Errorf("encode structured execution failure: %w", encodeErr)
		}
		return &ToolOutcomeError{rejection: &toolcontract.Rejection{Message: message}, evidence: failure.toolEvidence()}
	}
	if transportFailure := transport.FindFailure(err, transport.FailureTransport); transportFailure != nil {
		if transportFailure.ProcessID != "" {
			if abandonErr := e.store.AbandonProcess(ctx, e.owner, transportFailure.ProcessID, transportFailure.AfterSequence, transportFailure.Output, sessionstore.NestedUnknown, transportFailure.ExecutionSessionID); abandonErr != nil {
				return fmt.Errorf("record unknown process %s outcome after %s transport failure: %w", transportFailure.ProcessID, transportFailure.Method, abandonErr)
			}
		}
		message, encodeErr := executionFailureMessage(executionFailureProjection(transportFailure))
		if encodeErr != nil {
			return fmt.Errorf("encode observed process failure: %w", encodeErr)
		}
		return &ToolOutcomeError{rejection: &toolcontract.Rejection{Message: message}, evidence: executionFailureProjection(transportFailure).intentEvidence()}
	}
	if rejected := transport.FindFailure(err, transport.FailureRejected); rejected != nil {
		if rejected.Method == transport.MethodProcessRead {
			if rejected.ProcessID == "" {
				return errors.New("settle rejected process/read without a process ID")
			}
			process, processErr := e.store.Process(ctx, e.owner, rejected.ProcessID)
			if processErr != nil {
				return fmt.Errorf("inspect process %s after rejected process/read: %w", rejected.ProcessID, processErr)
			}
			if process.Status == executionstate.ProcessExited || process.Status == executionstate.ProcessExitedUnread {
				return &toolcontract.Rejection{Message: rejected.Error()}
			}
			if process.Status != executionstate.ProcessAbandoned {
				afterSequence := rejected.AfterSequence
				if afterSequence == nil {
					afterSequence = process.AfterSequence
				}
				if abandonErr := e.store.AbandonProcess(ctx, e.owner, rejected.ProcessID, afterSequence, rejected.Output, sessionstore.NestedUnknown, rejected.ExecutionSessionID); abandonErr != nil {
					return fmt.Errorf("record unknown process %s outcome after rejected process/read: %w", rejected.ProcessID, abandonErr)
				}
			}
			message := rejected.Error() + "; the process handle was abandoned with unknown outcome and was not replayed"
			return &ToolOutcomeError{
				rejection: &toolcontract.Rejection{Message: message},
				evidence: rejectedProcessReadEvidence{
					Error: message, ExecutorIntentID: rejected.IntentID,
					ProcessID: rejected.ProcessID, ExecutionSessionID: rejected.ExecutionSessionID,
					RemoteOutcome: sessionstore.NestedUnknown, AutomaticReplay: false,
				},
			}
		}
		return &toolcontract.Rejection{Message: rejected.Error()}
	}
	if unavailable := transport.FindFailure(err, transport.FailureScopeUnavailable); unavailable != nil {
		return &toolcontract.Rejection{Message: unavailable.Error()}
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
	if failure.method == transport.MethodProcessStart && failure.remoteOutcome == sessionstore.NestedNotDispatched && process.Status == executionstate.ProcessStarting {
		if err := e.store.ProcessResult(ctx, e.owner, failure.processID, failure.afterSequence, executionstate.ProcessExited, nil, true); err != nil {
			return fmt.Errorf("settle process %s after known not-dispatched start: %w", failure.processID, err)
		}
		return nil
	}
	if failure.remoteOutcome != sessionstore.NestedUnknown || process.Status == executionstate.ProcessAbandoned || process.Status == executionstate.ProcessExited || process.Status == executionstate.ProcessExitedUnread {
		return nil
	}
	if err := e.store.AbandonProcess(ctx, e.owner, failure.processID, failure.afterSequence, failure.output, sessionstore.NestedUnknown, failure.executionSessionID); err != nil {
		return fmt.Errorf("record unknown process %s outcome after structured execution failure: %w", failure.processID, err)
	}
	return nil
}

func executionEnvironmentStateFromError(err error) string {
	if environment := transport.FindFailure(err, transport.FailureEnvironment); environment != nil {
		return environment.EnvironmentState
	}
	return ""
}

// Codex rust-v0.162.1 core/src/tools/handlers/unified_exec/exec_command.rs handle_call and
// core/src/unified_exec/process_manager.rs exec_command_inner.
func (e *Executor) execCommand(ctx context.Context, arguments json.RawMessage) (*unifiedExecResult, error) {
	args, err := parseExecCommand(arguments)
	if err != nil {
		return nil, err
	}
	ctx = transport.WithExpectedIncarnationUID(ctx, args.ExpectedIncarnationUID)
	shell := e.transport.ShellSnapshot()
	if shell != nil {
		if _, err := args.plan(e.cwd, shell); err != nil {
			return nil, err
		}
	}
	if err := e.prepareWorkspace(ctx); err != nil {
		return nil, err
	}
	plan, err := planExecCommand(arguments, e.cwd, e.transport.ShellSnapshot())
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
		return nil, &toolcontract.Rejection{Message: "apply_patch takes a patch string"}
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
