package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"slices"
	"sync"
	"time"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"google.golang.org/protobuf/proto"
)

type Runtime struct {
	store        *Store
	settings     Settings
	claimLease   func(context.Context, string, time.Duration) (Owner, error)
	releaseLease func(context.Context, Owner) error

	mu       sync.Mutex
	runCtx   context.Context
	cancel   context.CancelFunc
	started  bool
	stopping bool
	tasks    map[string]struct{}
	changed  chan struct{}
	drivers  sync.WaitGroup
	codeHost *CodeModeHost
}

func NewRuntime(store *Store, settings Settings) *Runtime {
	return &Runtime{
		store: store, settings: settings, claimLease: store.Claim, releaseLease: store.Release,
		tasks: map[string]struct{}{}, changed: make(chan struct{}, 1),
	}
}

func (r *Runtime) Wake(sessionID string) {
	r.mu.Lock()
	if r.runCtx != nil && !r.stopping {
		r.launchLocked(sessionID)
	}
	r.mu.Unlock()
	r.signal()
}

func (r *Runtime) Run(ctx context.Context) error {
	r.mu.Lock()
	if r.started {
		r.mu.Unlock()
		return errors.New("central sampler runtime already started")
	}
	r.started = true
	r.runCtx, r.cancel = context.WithCancel(ctx)
	r.codeHost = NewCodeModeHost(context.Background(), r.settings, r.store)
	codeHost := r.codeHost
	r.mu.Unlock()

	recoveryDone := make(chan struct{})
	go func() {
		defer close(recoveryDone)
		r.recover(r.runCtx)
	}()
	<-r.runCtx.Done()
	r.mu.Lock()
	r.stopping = true
	codeHost.StopAccepting()
	r.cancel()
	r.mu.Unlock()
	<-recoveryDone
	// Drivers deliberately use independent bounded contexts for Claim and Release. Waiting here
	// closes the committed-but-not-returned Claim and blocked Release shutdown races.
	r.drivers.Wait()
	closeCtx, closeCancel := context.WithTimeout(context.Background(), 5*time.Second)
	if err := codeHost.Close(closeCtx); err != nil {
		slog.Error("central sampler code-mode host close failed", "error", err)
	}
	closeCancel()
	return nil
}

func (r *Runtime) recover(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		r.mu.Lock()
		capacity := r.settings.MaxLiveSessions - len(r.tasks)
		excluded := make([]string, 0, len(r.tasks))
		for sessionID := range r.tasks {
			excluded = append(excluded, sessionID)
		}
		r.mu.Unlock()
		if capacity > 0 {
			sessions, err := r.store.RecoverableSessions(ctx, excluded, capacity)
			if err != nil && ctx.Err() == nil {
				slog.Error("central sampler recovery scan failed", "error", err)
			} else {
				for _, sessionID := range sessions {
					r.Wake(sessionID)
				}
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-r.changed:
		case <-ticker.C:
		}
	}
}

func (r *Runtime) launchLocked(sessionID string) {
	if sessionID == "" || r.runCtx == nil || r.stopping || len(r.tasks) >= r.settings.MaxLiveSessions {
		return
	}
	if _, exists := r.tasks[sessionID]; exists {
		return
	}
	r.tasks[sessionID] = struct{}{}
	r.drivers.Add(1)
	go func() {
		defer r.drivers.Done()
		r.drive(r.runCtx, sessionID)
		r.mu.Lock()
		delete(r.tasks, sessionID)
		r.mu.Unlock()
		r.signal()
	}()
}

func (r *Runtime) signal() {
	select {
	case r.changed <- struct{}{}:
	default:
	}
}

func (r *Runtime) drive(runtimeCtx context.Context, sessionID string) {
	loadCtx, cancelLoad := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
	session, err := r.store.GetSession(loadCtx, sessionID, "")
	cancelLoad()
	if err != nil {
		if !errors.Is(err, protocol.ErrMissing) {
			slog.Error("central sampler could not load Session", "session_id", sessionID, "error", err)
		}
		return
	}
	claimCtx, claimCancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
	owner, err := r.claimLease(claimCtx, session.ConversationID, time.Duration(r.settings.LeaseSeconds)*time.Second)
	claimCancel()
	if err != nil {
		var lost *LostOwnershipError
		if !errors.As(err, &lost) && !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded) {
			slog.Error("central sampler lease claim failed", "session_id", sessionID, "error", err)
		}
		return
	}
	if runtimeCtx.Err() != nil || r.isStopping() {
		r.release(owner)
		return
	}
	executionCtx, cancelExecution := context.WithCancel(runtimeCtx)
	heartbeatDone := make(chan error, 1)
	go func() {
		err := r.heartbeat(executionCtx, owner)
		if err != nil {
			cancelExecution()
		}
		heartbeatDone <- err
	}()
	activeCommand, executionErr := r.execute(executionCtx, sessionID, owner)
	cancelExecution()
	heartbeatErr := <-heartbeatDone
	if executionErr != nil && runtimeCtx.Err() == nil && heartbeatErr == nil {
		var known *protocol.KnownFailureError
		if errors.As(executionErr, &known) {
			slog.Warn("central sampler rejected execution", "session_id", sessionID, "reason", known.Error())
		} else {
			slog.Error("central sampler execution stopped", "session_id", sessionID, "error", executionErr)
		}
		failCtx, cancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
		if err := r.store.FailExecution(failCtx, owner, sessionID, activeCommand, executionErr); err != nil && !errors.Is(err, context.Canceled) {
			slog.Error("central sampler could not record execution failure", "session_id", sessionID, "error", err)
		}
		cancel()
	}
	r.release(owner)
}

func (r *Runtime) heartbeat(ctx context.Context, owner Owner) error {
	period := time.Duration(r.settings.LeaseSeconds) * time.Second / 3
	ticker := time.NewTicker(period)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			renewCtx, cancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
			err := r.store.Renew(renewCtx, owner, time.Duration(r.settings.LeaseSeconds)*time.Second)
			cancel()
			if err != nil {
				if ctx.Err() == nil {
					slog.Error("central sampler lease renewal failed", "conversation_id", owner.ConversationID, "error", err)
					return err
				}
				return nil
			}
		}
	}
}

func (r *Runtime) release(owner Owner) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
	defer cancel()
	if err := r.releaseLease(ctx, owner); err != nil {
		var lost *LostOwnershipError
		if !errors.As(err, &lost) {
			slog.Error("central sampler lease release failed", "conversation_id", owner.ConversationID, "error", err)
		}
	}
}

func (r *Runtime) isStopping() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.stopping
}

func (r *Runtime) execute(ctx context.Context, sessionID string, owner Owner) (*string, error) {
	setupOK, err := r.setup(ctx, sessionID, owner)
	if err != nil || !setupOK {
		return nil, err
	}
	var live *liveHost
	defer func() {
		if live != nil {
			closeCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			if err := live.Close(closeCtx); err != nil {
				slog.Error("central sampler host close failed", "session_id", sessionID, "error", err)
			}
			cancel()
		}
	}()
	var activeCommand *string
	client := &http.Client{}
	for {
		if ctx.Err() != nil {
			return activeCommand, ctx.Err()
		}
		command, err := r.store.NextCommand(ctx, owner, sessionID)
		if errors.Is(err, protocol.ErrPrecondition) {
			return nil, nil
		}
		if err != nil {
			return activeCommand, err
		}
		if command == nil {
			state, err := r.store.ExecutionState(ctx, owner)
			if err != nil {
				return activeCommand, err
			}
			if err := r.store.CheckLiveCells(ctx, owner, live != nil); err != nil {
				return activeCommand, err
			}
			runningProcess := false
			for _, process := range state.Processes {
				if process.Status != "exited_unread" && process.Status != "exited" {
					runningProcess = true
					break
				}
			}
			if len(state.Cells) == 0 && !runningProcess {
				return nil, nil
			}
			if live == nil {
				live, err = r.connectHost(ctx, sessionID, owner)
				if err != nil {
					return activeCommand, err
				}
			}
			progressed, err := live.executor.ReconcileProcesses(ctx, time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
			if err != nil {
				return activeCommand, err
			}
			if progressed {
				continue
			}
			timer := time.NewTimer(250 * time.Millisecond)
			select {
			case <-ctx.Done():
				timer.Stop()
				return activeCommand, ctx.Err()
			case <-liveDone(live):
				timer.Stop()
				if err := live.code.Err(); err != nil {
					return activeCommand, err
				}
				return activeCommand, errors.New("code-mode session detached while remote effects were live")
			case <-r.changed:
				timer.Stop()
			case <-timer.C:
			}
			continue
		}
		if command.Outcome != samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED {
			continue
		}
		currentCommand := command.ID
		activeCommand = &currentCommand
		if err := r.store.CheckRecovery(ctx, owner); err != nil {
			return activeCommand, err
		}
		session, err := r.store.GetSession(ctx, sessionID, "")
		if err != nil {
			return activeCommand, err
		}
		payload := &commandpb.Command{}
		if err := proto.Unmarshal(command.Payload, payload); err != nil {
			return activeCommand, err
		}
		turnCtx, cancel := context.WithTimeout(ctx, time.Duration(r.settings.TurnTimeoutSeconds)*time.Second)
		result := ""
		if payload.GetCompact() != nil {
			err = r.compact(turnCtx, owner, session.Model)
			result = "compact"
		} else {
			for {
				conversation, readErr := r.store.Read(turnCtx, owner)
				if readErr != nil {
					err = readErr
					break
				}
				if live == nil && conversation.Pending == nil {
					state, stateErr := r.store.ExecutionState(turnCtx, owner)
					if stateErr != nil {
						err = stateErr
						break
					}
					calls, callErr := conversation.Archive.PendingCalls()
					if callErr != nil {
						err = callErr
						break
					}
					needsHost := len(state.Cells) > 0 || len(calls) > 0 && calls[0].IsModelTool(r.settings.Models[session.Model].ExecEncoding)
					if needsHost {
						interrupts, interruptErr := r.store.PendingInterrupts(turnCtx, owner)
						if interruptErr != nil {
							err = interruptErr
							break
						}
						if len(interrupts) == 0 || len(state.Cells) > 0 {
							live, err = r.connectHost(ctx, sessionID, owner)
							if err != nil {
								break
							}
						}
					}
				}
				result, err = r.controlledAdvance(turnCtx, sessionID, owner, live, client, session)
				if err != nil || result == "done" || result == "interrupted" || result == "effects_unknown" {
					break
				}
				if result == "cell" || result == "sample" || result == "compact" || result == "notification" || result == "steer" {
					continue
				}
			}
		}
		cancel()
		if err != nil {
			return activeCommand, err
		}
		if result == "" {
			result = "done"
		}
		if err := r.store.FinishCommand(ctx, owner, sessionID, command.ID, result); err != nil {
			return activeCommand, err
		}
		activeCommand = nil
		if result == "effects_unknown" {
			return nil, nil
		}
		if result == "interrupted" && live != nil {
			closeCtx, closeCancel := context.WithTimeout(context.Background(), 5*time.Second)
			_ = live.Close(closeCtx)
			closeCancel()
			live = nil
		}
	}
}

func (r *Runtime) setup(ctx context.Context, sessionID string, owner Owner) (bool, error) {
	session, err := r.store.GetSession(ctx, sessionID, "")
	if err != nil {
		return false, err
	}
	switch session.SetupState {
	case runnerpb.SetupState_SETUP_STATE_NOT_REQUIRED, runnerpb.SetupState_SETUP_STATE_SUCCEEDED:
		return true, nil
	case runnerpb.SetupState_SETUP_STATE_FAILED, runnerpb.SetupState_SETUP_STATE_INTERRUPTED:
		return false, nil
	case runnerpb.SetupState_SETUP_STATE_RUNNING:
	default:
		return false, fmt.Errorf("unknown setup state %s", session.SetupState)
	}
	started, err := r.store.StartSetup(ctx, owner, sessionID)
	if err != nil || !started {
		return false, err
	}
	configuration := &samplerpb.OpenSessionRequest{}
	if err := proto.Unmarshal(session.Configuration, configuration); err != nil {
		return false, err
	}
	setupCtx, cancel := context.WithTimeout(ctx, time.Duration(r.settings.TurnTimeoutSeconds)*time.Second)
	defer cancel()
	executor, closeTransport, err := ConnectExecutor(setupCtx, r.store, owner, session, r.settings)
	if err != nil {
		if ctx.Err() == nil {
			finishCtx, finishCancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
			finishErr := r.store.FinishSetup(finishCtx, owner, sessionID, nil)
			finishCancel()
			return false, errors.Join(err, finishErr)
		}
		return false, err
	}
	defer closeTransport()
	exitCode, err := executor.Setup(setupCtx, configuration.GetSetupScript(), func(stream string, data []byte) error {
		return r.store.SetupOutput(setupCtx, owner, sessionID, stream, data)
	})
	if err != nil {
		if ctx.Err() != nil {
			return false, err
		}
		finishCtx, finishCancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
		finishErr := r.store.FinishSetup(finishCtx, owner, sessionID, nil)
		finishCancel()
		return false, errors.Join(err, finishErr)
	}
	finishCtx, finishCancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
	finishErr := r.store.FinishSetup(finishCtx, owner, sessionID, &exitCode)
	finishCancel()
	if finishErr != nil {
		return false, finishErr
	}
	return exitCode == 0, nil
}

type liveHost struct {
	code           *CodeMode
	executor       *Executor
	closeTransport func() error
	closeOnce      sync.Once
	closeErr       error
}

func (h *liveHost) Close(ctx context.Context) error {
	var errs []error
	if h.code != nil {
		errs = append(errs, h.code.Detach(ctx))
	}
	errs = append(errs, h.CloseTransport())
	return errors.Join(errs...)
}

func (h *liveHost) CloseTransport() error {
	h.closeOnce.Do(func() {
		if h.closeTransport != nil {
			h.closeErr = h.closeTransport()
		}
	})
	return h.closeErr
}

func (r *Runtime) connectHost(ctx context.Context, sessionID string, owner Owner) (*liveHost, error) {
	session, err := r.store.GetSession(ctx, sessionID, "")
	if err != nil {
		return nil, err
	}
	executor, closeTransport, err := ConnectExecutor(ctx, r.store, owner, session, r.settings)
	if err != nil {
		return nil, err
	}
	code, err := r.codeHost.OpenSession(ctx, owner.ConversationID, owner, executor)
	if err != nil {
		_ = closeTransport()
		return nil, err
	}
	return &liveHost{code: code, executor: executor, closeTransport: closeTransport}, nil
}

func liveDone(live *liveHost) <-chan struct{} {
	if live == nil || live.code == nil {
		return nil
	}
	return live.code.Done()
}

func (r *Runtime) controlledAdvance(ctx context.Context, sessionID string, owner Owner, live *liveHost, client *http.Client, session *Session) (string, error) {
	interrupts, err := r.store.PendingInterrupts(ctx, owner)
	if err != nil {
		return "", err
	}
	if len(interrupts) > 0 {
		return r.applyInterrupt(ctx, sessionID, owner, live, interrupts)
	}
	advanceCtx, cancel := context.WithCancel(ctx)
	type advanceResult struct {
		result string
		err    error
	}
	completed := make(chan advanceResult, 1)
	go func() {
		result, err := r.advance(advanceCtx, owner, live, client, session)
		completed <- advanceResult{result: result, err: err}
	}()
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
	joined := false
	defer func() {
		cancel()
		if !joined {
			<-completed
		}
	}()
	for {
		select {
		case result := <-completed:
			joined = true
			return result.result, result.err
		case <-ctx.Done():
			cancel()
			<-completed
			joined = true
			return "", ctx.Err()
		case <-liveDone(live):
			cancel()
			<-completed
			joined = true
			if err := live.code.Err(); err != nil {
				return "", err
			}
			return "", errors.New("code-mode session detached during advance")
		case <-ticker.C:
			interrupts, err := r.store.PendingInterrupts(ctx, owner)
			if err != nil {
				return "", err
			}
			if len(interrupts) == 0 && live != nil && live.code.HasForeground() {
				steering, err := r.store.HasSteeringInput(ctx, owner, sessionID)
				if err != nil {
					return "", err
				}
				if steering {
					if _, err := live.code.YieldForeground(ctx); err != nil {
						return "", err
					}
				}
			}
			if len(interrupts) > 0 {
				conversation, readErr := r.store.Read(ctx, owner)
				if readErr != nil {
					return "", readErr
				}
				cancel()
				<-completed
				joined = true
				return r.applyInterrupt(ctx, sessionID, owner, live, interrupts, conversation.Pending)
			}
		}
	}
}

func (r *Runtime) applyInterrupt(ctx context.Context, sessionID string, owner Owner, live *liveHost, interruptIDs []string, pending ...*PendingIntent) (string, error) {
	var currentPending *PendingIntent
	if len(pending) > 0 {
		currentPending = pending[0]
	} else {
		conversation, err := r.store.Read(ctx, owner)
		if err != nil {
			return "", err
		}
		currentPending = conversation.Pending
	}
	cleanupCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cells, processes := map[string]any{}, map[string]any{}
	var cleanupErr error
	delegatesPending := false
	if live != nil {
		delegatesPending = live.code.hasDelegates()
		live.executor.stopDispatch()
		cells, cleanupErr = live.code.Interrupt(cleanupCtx)
		var processErr error
		processes, processErr = live.executor.InterruptAll(cleanupCtx)
		cleanupErr = errors.Join(cleanupErr, processErr)
		if detachErr := live.code.Detach(cleanupCtx); detachErr != nil {
			cleanupErr = errors.Join(cleanupErr, detachErr)
		}
	}
	state, stateErr := r.store.ExecutionState(cleanupCtx, owner)
	cleanupErr = errors.Join(cleanupErr, stateErr)
	complete := cleanupErr == nil && stateErr == nil && len(state.Cells) == 0
	if delegatesPending {
		complete = false
	}
	if stateErr == nil {
		for _, process := range state.Processes {
			if process.Status != "exited" && process.Status != "exited_unread" {
				complete = false
			}
		}
	}
	if live != nil {
		if closeErr := live.CloseTransport(); closeErr != nil {
			cleanupErr = errors.Join(cleanupErr, closeErr)
			complete = false
		}
	}
	providerPending := currentPending != nil && (currentPending.Kind == PendingIntentSampling || currentPending.Kind == PendingIntentCompaction)
	result := map[string]any{
		"status": "effects_unknown", "provider_cancelled": providerPending,
		"provider_completion_unknown": providerPending, "cells": cells, "processes": processes,
		"cleanup_complete": complete, "partial_effects_possible": true,
	}
	if cleanupErr != nil {
		result["cleanup_error"] = cleanupErr.Error()
	}
	status := "effects_unknown"
	if complete {
		status = "interrupted"
	}
	result["status"] = status
	if err := r.store.AcknowledgeInterrupt(cleanupCtx, owner, interruptIDs, result); err != nil {
		return "", err
	}
	return status, nil
}

func (r *Runtime) advance(ctx context.Context, owner Owner, live *liveHost, client *http.Client, session *Session) (string, error) {
	conversation, err := r.store.Read(ctx, owner)
	if err != nil {
		return "", err
	}
	config, configured := r.settings.Models[session.Model]
	if !configured || conversation.API != config.API {
		return "", protocol.ErrPrecondition
	}

	if conversation.Pending != nil {
		return "", &protocol.UnknownEffectError{Reason: "unresolved provider or executor intent blocks continuation; automatic replay is unsafe"}
	}
	if conversation.Stopped {
		return "interrupted", nil
	}
	if err := r.store.CheckLiveCells(ctx, owner, live != nil); err != nil {
		return "", err
	}
	state, err := r.store.ExecutionState(ctx, owner)
	if err != nil {
		return "", err
	}
	if live != nil {
		if err := live.code.Err(); err != nil {
			return "", err
		}
	}
	calls, err := conversation.Archive.PendingCalls()
	if err != nil {
		return "", err
	}
	if len(calls) > 0 {
		call := &calls[0]
		if !call.IsModelTool(r.settings.Models[session.Model].ExecEncoding) {
			if !call.HasValidShape() {
				return "", &protocol.KnownFailureError{Evidence: map[string]any{"reason": "invalid model tool call shape", "call": call}}
			}
			if call.IsConfiguredToolName(r.settings.Models[session.Model].ExecEncoding) {
				return "", &protocol.KnownFailureError{Evidence: map[string]any{"reason": "unsupported model tool encoding", "call": call}}
			}
			content := []protocol.ContentPart{{Type: "input_text", Text: unsupportedToolCallMessage(*call)}}
			intentID, err := r.store.Begin(ctx, owner, map[string]any{"kind": "cell", "call": call, "dispatched": false})
			if err != nil {
				return "", err
			}
			result := map[string]any{
				"call_id": call.CallID, "output": content,
				"success": false, "dispatched": false,
			}
			if err := r.store.FinishTool(ctx, owner, intentID, result, *call, content); err != nil {
				return "", err
			}
			return "cell", nil
		}
		if live == nil {
			return "", &protocol.KnownFailureError{Evidence: map[string]any{"reason": "pending code cell has no live host", "call": call}}
		}
		intentID, err := r.store.Begin(ctx, owner, map[string]any{"kind": "cell", "call": call})
		if err != nil {
			return "", err
		}
		content, err := live.code.Execute(ctx, *call)
		if err != nil {
			var hostError *codeModeHostResponseError
			if errors.As(err, &hostError) && hostError.phase == codeOperationResponsePhase {
				content = []protocol.ContentPart{{Type: "input_text", Text: hostError.message}}
				result := map[string]any{
					"call_id": call.CallID, "output": content, "success": false,
					"code_mode_host_response": hostError.evidence(),
				}
				if err := r.store.FinishTool(ctx, owner, intentID, result, *call, content); err != nil {
					return "", err
				}
				return "cell", nil
			}
			var known *protocol.KnownFailureError
			if errors.As(err, &known) {
				if rejectErr := r.store.Reject(ctx, owner, intentID, known); rejectErr != nil {
					return "", rejectErr
				}
			}
			return "", err
		}
		result := map[string]any{"call_id": call.CallID, "output": content}
		if err := r.store.FinishTool(ctx, owner, intentID, result, *call, content); err != nil {
			return "", err
		}
		return "cell", nil
	}
	// TODO: Codex rust-v0.162.1 keeps notify() outputs and user input in one arrival-ordered pending
	// queue; this records a step's notify() outputs before its steering input (SPEC.md deviation).
	drained, err := r.store.DrainNotifications(ctx, owner)
	if err != nil {
		return "", err
	}
	if drained {
		return "notification", nil
	}
	// TODO: Codex's InstantInterrupt also cuts an in-flight provider stream for new input. Steering
	// input waits here for the sampling request to finish, because a cut stream leaves its intent
	// uncertain and blocks the Session (SPEC.md deviation); cutting needs a way to settle that intent.
	steered, err := r.store.DrainSteeringInput(ctx, owner, session.ID)
	if err != nil {
		return "", err
	}
	if steered {
		return "steer", nil
	}
	if conversation.Archive.TurnComplete() {
		return "done", nil
	}
	adapter, err := adapterFor(config)
	if err != nil {
		return "", err
	}
	if conversation.API != config.API {
		return "", protocol.ErrPrecondition
	}
	threshold := adapter.AutoCompactTokens(r.settings.AutoCompactTokens)

	if threshold != nil && conversation.ContextTokens != nil && *conversation.ContextTokens >= *threshold && len(state.Cells) == 0 {
		if err := r.compact(ctx, owner, session.Model); err != nil {
			return "", err
		}
		return "compact", nil
	}
	conversation, err = r.store.PrepareSamplingContext(ctx, owner, state)
	if err != nil {
		return "", err
	}

	metadata, err := r.store.ProviderMetadata(ctx, owner)
	if err != nil {
		return "", err
	}
	connection, err := r.settings.Connection(session.Model)
	if err != nil {
		return "", err
	}
	body, err := adapter.Prepare(conversation, session, state, metadata)
	if err != nil {
		return "", err
	}
	wire, err := json.Marshal(body)
	if err != nil {
		return "", err
	}
	intentID, err := r.store.Begin(ctx, owner, providerRequestAudit("sampling", connection, config, body, wire))
	if err != nil {
		return "", err
	}
	evidence := &ResponseEvidence{Store: r.store, Owner: owner, IntentID: intentID}
	headers := map[string]string{"x-agentplane-session-id": metadata.SessionID}
	response, err := adapter.Sample(ctx, client, connection, body, wire, evidence, headers, false)
	if err != nil {
		var known *protocol.KnownFailureError
		if errors.As(err, &known) {
			if rejectErr := r.store.Reject(ctx, owner, intentID, known); rejectErr != nil {
				return "", rejectErr
			}
		}
		return "", err
	}
	candidate, err := cloneNativeContext(conversation.Archive)
	if err == nil {
		err = candidate.AppendRecords(response.Records)
	}
	if err == nil {
		var pending []protocol.ToolInvocation
		pending, err = candidate.PendingCalls()
		for _, call := range pending {
			if !call.HasValidShape() || (call.IsConfiguredToolName(config.ExecEncoding) && !call.IsModelTool(config.ExecEncoding)) {
				err = &protocol.KnownFailureError{Evidence: map[string]any{"reason": "invalid model tool encoding", "call": call}}
				break
			}
		}
	}
	if err != nil {
		failure := &protocol.KnownFailureError{Evidence: map[string]any{"reason": "invalid native conversation continuation", "response": response.Audit, "error": err.Error()}}
		if rejectErr := r.store.Reject(ctx, owner, intentID, failure); rejectErr != nil {
			return "", rejectErr
		}
		return "", failure
	}
	if err := r.store.Finish(ctx, owner, intentID, response.Audit, response.Records, response.ContextTokens); err != nil {
		return "", err
	}

	return "sample", nil
}

func (r *Runtime) compact(ctx context.Context, owner Owner, model string) error {
	state, err := r.store.ExecutionState(ctx, owner)
	if err != nil {
		return err
	}
	if len(state.Cells) != 0 {
		return &protocol.UnknownEffectError{Reason: "cannot compact over live JavaScript cells"}
	}
	conversation, err := r.store.Read(ctx, owner)
	if err != nil {
		return err
	}
	if err := conversation.Archive.CheckSettled(); err != nil {
		return err
	}
	if err := conversation.Context().CheckSettled(); err != nil {
		return err
	}
	metadata, err := r.store.ProviderMetadata(ctx, owner)
	if err != nil {
		return err
	}
	adapter, err := adapterFor(r.settings.Models[model])
	if err != nil {
		return err
	}
	if adapter.API() != conversation.API {
		return protocol.ErrPrecondition
	}
	session, err := r.store.GetSessionForConversation(ctx, owner)
	if err != nil {
		return err
	}
	environment, err := r.store.environmentContext(conversation, session)
	if err != nil {
		return err
	}
	plan, err := adapter.PrepareCompaction(conversation, session, state, metadata, environmentContextUpdate(nil, environment))
	if err != nil {
		return err
	}
	wire, err := json.Marshal(plan.Body)
	if err != nil {
		return err
	}
	connection, err := r.settings.Connection(model)
	if err != nil {
		return err
	}
	original := conversation.Context()
	intentID, err := r.store.BeginCompaction(ctx, owner, providerRequestAudit("compaction", connection, r.settings.Models[model], plan.Body, wire), original)
	if err != nil {
		return err
	}
	headers := map[string]string{"x-agentplane-session-id": metadata.SessionID}
	result, err := adapter.Sample(ctx, &http.Client{}, connection, plan.Body, wire, &ResponseEvidence{Store: r.store, Owner: owner, IntentID: intentID}, headers, true)
	if err != nil {
		return r.rejectKnownCompactionFailure(ctx, owner, intentID, err)
	}
	replacement, err := plan.Apply(result)
	if err != nil {
		return r.rejectKnownCompactionFailure(ctx, owner, intentID, err)
	}
	return r.store.FinishCompaction(ctx, owner, intentID, result.Audit, original, replacement)
}

func sortedKeys(values map[string]struct{}) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	return keys
}

func (e *Executor) stopDispatch() {
	e.dispatchMu.Lock()
	e.stopping = true
	e.dispatchMu.Unlock()
}

func (r *Runtime) rejectKnownCompactionFailure(ctx context.Context, owner Owner, intentID string, err error) error {
	var known *protocol.KnownFailureError
	if errors.As(err, &known) {
		if rejectErr := r.store.Reject(ctx, owner, intentID, known); rejectErr != nil {
			return rejectErr
		}
	}
	return err
}
