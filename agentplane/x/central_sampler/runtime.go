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
		if !errors.Is(err, ErrMissing) {
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
		var known *KnownFailureError
		if errors.As(executionErr, &known) {
			slog.Warn("central sampler rejected execution", "session_id", sessionID, "reason", known.Error())
		} else {
			slog.Error("central sampler execution stopped", "session_id", sessionID, "error", executionErr)
		}
		failCtx, cancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
		if err := r.store.FailExecution(failCtx, owner, sessionID, activeCommand, known); err != nil && !errors.Is(err, context.Canceled) {
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
		if errors.Is(err, ErrPrecondition) {
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
		configuration := &samplerpb.OpenSessionRequest{}
		if err := proto.Unmarshal(session.Configuration, configuration); err != nil {
			return activeCommand, err
		}
		provider, ok := r.settings.Providers[configuration.GetSpec().GetCentralSampler().GetProvider()]
		if !ok {
			return activeCommand, fmt.Errorf("configured provider %q is unavailable", configuration.GetSpec().GetCentralSampler().GetProvider())
		}
		payload := &commandpb.Command{}
		if err := proto.Unmarshal(command.Payload, payload); err != nil {
			return activeCommand, err
		}
		turnCtx, cancel := context.WithTimeout(ctx, time.Duration(r.settings.TurnTimeoutSeconds)*time.Second)
		result := ""
		if payload.GetCompact() != nil {
			err = r.compact(turnCtx, owner, provider, session.Model)
			result = "compact"
		} else {
			for {
				conversation, readErr := r.store.Read(turnCtx, owner)
				if readErr != nil {
					err = readErr
					break
				}
				if live == nil && conversation.Pending == nil && pendingCall(conversation.Items) != nil {
					interrupts, interruptErr := r.store.PendingInterrupts(turnCtx, owner)
					if interruptErr != nil {
						err = interruptErr
						break
					}
					if len(interrupts) == 0 {
						live, err = r.connectHost(ctx, sessionID, owner)
						if err != nil {
							break
						}
					}
				}
				result, err = r.controlledAdvance(turnCtx, sessionID, owner, live, client, provider, session)
				if err != nil || result == "done" || result == "interrupted" || result == "effects_unknown" {
					break
				}
				if result == "cell" || result == "sample" || result == "compact" {
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

func (r *Runtime) controlledAdvance(ctx context.Context, sessionID string, owner Owner, live *liveHost, client *http.Client, provider Provider, session *Session) (string, error) {
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
		result, err := r.advance(advanceCtx, owner, live, client, provider, session)
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

func (r *Runtime) advance(ctx context.Context, owner Owner, live *liveHost, client *http.Client, provider Provider, session *Session) (string, error) {
	conversation, err := r.store.Read(ctx, owner)
	if err != nil {
		return "", err
	}
	if conversation.Pending != nil {
		return "", &UnknownEffectError{Reason: "unresolved provider or executor intent blocks continuation; automatic replay is unsafe"}
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
	if call := pendingCall(conversation.Items); call != nil {
		if call.Name != "exec" && call.Name != "wait" {
			return "", &KnownFailureError{Evidence: map[string]any{"reason": "unsupported model tool", "call": call}}
		}
		if live == nil {
			return "", &KnownFailureError{Evidence: map[string]any{"reason": "pending code cell has no live host", "call": call}}
		}
		intentID, err := r.store.Begin(ctx, owner, map[string]any{"kind": "cell", "call": call})
		if err != nil {
			return "", err
		}
		content, err := live.code.Execute(ctx, *call)
		if err != nil {
			var known *KnownFailureError
			if errors.As(err, &known) {
				if rejectErr := r.store.Reject(ctx, owner, intentID, known); rejectErr != nil {
					return "", rejectErr
				}
			}
			return "", err
		}
		result := map[string]any{"type": string(ResponsesItemCustomToolOutput), "call_id": call.CallID, "output": content}
		item := ResponsesItem{Type: ResponsesItemCustomToolOutput, CallID: call.CallID, Output: content}
		if err := r.store.Finish(ctx, owner, intentID, result, []ResponsesItem{item}, nil); err != nil {
			return "", err
		}
		return "cell", nil
	}
	if len(conversation.Items) > 0 {
		last := conversation.Items[len(conversation.Items)-1]
		if last.Type == ResponsesItemMessage && last.Role == "assistant" {
			return "done", nil
		}
	}
	if r.settings.AutoCompactTokens != nil && conversation.ContextTokens != nil && *conversation.ContextTokens >= *r.settings.AutoCompactTokens && len(state.Cells) == 0 {
		if err := r.compact(ctx, owner, provider, session.Model); err != nil {
			return "", err
		}
		return "compact", nil
	}
	body, err := RequestBody(session.Model, conversation.ID, conversation.Context(), conversation.Instructions, state)
	if err != nil {
		return "", err
	}
	if session.Effort != "" {
		body.Reasoning = &ResponsesReasoningConfig{Effort: session.Effort}
	}
	metadata, err := r.store.ProviderMetadata(ctx, owner)
	if err != nil {
		return "", err
	}
	body.ClientMetadata = ResponsesClientMetadata{SessionID: metadata.SessionID, ThreadID: metadata.ThreadID, TurnID: metadata.TurnID}
	body.PromptCacheKey = metadata.SessionID
	credential, err := provider.ReadCredential()
	if err != nil {
		return "", err
	}
	intentID, err := r.store.Begin(ctx, owner, map[string]any{"kind": "sampling", "body": body})
	if err != nil {
		return "", err
	}
	headers, err := r.store.ProviderHeaders(ctx, owner)
	if err != nil {
		return "", err
	}
	response, err := Sample(ctx, client, provider.Endpoint, credential, body,
		&ResponseEvidence{Store: r.store, Owner: owner, IntentID: intentID}, headers, false)
	if err != nil {
		var known *KnownFailureError
		if errors.As(err, &known) {
			if rejectErr := r.store.Reject(ctx, owner, intentID, known); rejectErr != nil {
				return "", rejectErr
			}
		}
		return "", err
	}
	for _, item := range response.Output {
		if item.Type != ResponsesItemMessage && item.Type != ResponsesItemReasoning && item.Type != ResponsesItemCustomToolCall {
			failure := &KnownFailureError{Evidence: map[string]any{"reason": "unsupported provider output type", "response": response.Audit(), "item": item}}
			if err := r.store.Reject(ctx, owner, intentID, failure); err != nil {
				return "", err
			}
			return "", failure
		}
		if item.Type == ResponsesItemCustomToolCall && item.Name != "exec" && item.Name != "wait" {
			failure := &KnownFailureError{Evidence: map[string]any{"reason": "unsupported model tool", "response": response.Audit(), "item": item}}
			if err := r.store.Reject(ctx, owner, intentID, failure); err != nil {
				return "", err
			}
			return "", failure
		}
	}
	contextTokens := response.Terminal.TotalTokens()
	if err := r.store.Finish(ctx, owner, intentID, response.Audit(), response.Output, contextTokens); err != nil {
		return "", err
	}
	return "sample", nil
}

func (r *Runtime) compact(ctx context.Context, owner Owner, provider Provider, model string) error {
	state, err := r.store.ExecutionState(ctx, owner)
	if err != nil {
		return err
	}
	if len(state.Cells) != 0 {
		return &UnknownEffectError{Reason: "cannot compact over live JavaScript cells"}
	}
	conversation, err := r.store.Read(ctx, owner)
	if err != nil {
		return err
	}
	if err := checkPairs(conversation.Items); err != nil {
		return err
	}
	original := conversation.Context()
	if err := checkPairs(original); err != nil {
		return err
	}
	body, err := RequestBody(model, conversation.ID, original, conversation.Instructions, state)
	if err != nil {
		return err
	}
	metadata, err := r.store.ProviderMetadata(ctx, owner)
	if err != nil {
		return err
	}
	body.ClientMetadata = ResponsesClientMetadata{SessionID: metadata.SessionID, ThreadID: metadata.ThreadID, TurnID: metadata.TurnID}
	body.PromptCacheKey = metadata.SessionID
	body.Input = append(body.Input, ResponsesItem{Type: ResponsesItemCompactionTrigger})
	credential, err := provider.ReadCredential()
	if err != nil {
		return err
	}
	intentID, err := r.store.BeginCompaction(ctx, owner, map[string]any{"kind": "compaction", "body": body}, original)
	if err != nil {
		return err
	}
	client := &http.Client{}
	headers, err := r.store.ProviderHeaders(ctx, owner)
	if err != nil {
		return err
	}
	response, err := Sample(ctx, client, provider.Endpoint, credential, body,
		&ResponseEvidence{Store: r.store, Owner: owner, IntentID: intentID}, headers, true)
	if err != nil {
		var known *KnownFailureError
		if errors.As(err, &known) {
			if rejectErr := r.store.Reject(ctx, owner, intentID, known); rejectErr != nil {
				return rejectErr
			}
		}
		return err
	}
	if len(response.Output) != 1 || response.Output[0].Type != ResponsesItemCompaction {
		failure := &KnownFailureError{Evidence: map[string]any{"reason": "compaction must produce exactly one compaction item and no tools", "response": response.Audit()}}
		if err := r.store.Reject(ctx, owner, intentID, failure); err != nil {
			return err
		}
		return failure
	}
	if response.Output[0].EncryptedContent == "" {
		failure := &KnownFailureError{Evidence: map[string]any{"reason": "compaction must include opaque encrypted_content", "response": response.Audit()}}
		if err := r.store.Reject(ctx, owner, intentID, failure); err != nil {
			return err
		}
		return failure
	}
	if contextBytes(response.Output) >= contextBytes(original) {
		failure := &KnownFailureError{Evidence: map[string]any{"reason": "compaction did not reduce serialized model context", "response": response.Audit()}}
		if err := r.store.Reject(ctx, owner, intentID, failure); err != nil {
			return err
		}
		return failure
	}
	return r.store.FinishCompaction(ctx, owner, intentID, response.Audit(), original, response.Output)
}

func pendingCall(items []ResponsesItem) *ResponsesToolCall {
	completed := map[string]struct{}{}
	for _, item := range items {
		if item.Type == ResponsesItemCustomToolOutput {
			completed[item.CallID] = struct{}{}
		}
	}
	for _, item := range items {
		call, ok := item.ToolCall()
		if !ok {
			continue
		}
		if _, ok := completed[call.CallID]; !ok {
			return &call
		}
	}
	return nil
}

func checkPairs(items []ResponsesItem) error {
	pending, seen := map[string]struct{}{}, map[string]struct{}{}
	for _, item := range items {
		switch item.Type {
		case ResponsesItemCustomToolCall:
			callID := item.CallID
			if _, exists := seen[callID]; exists {
				return &UnknownEffectError{Reason: "duplicate tool call " + callID}
			}
			seen[callID] = struct{}{}
			pending[callID] = struct{}{}
		case ResponsesItemCustomToolOutput:
			callID := item.CallID
			if _, exists := pending[callID]; !exists {
				return &UnknownEffectError{Reason: "orphan tool output " + callID}
			}
			delete(pending, callID)
		}
	}
	if len(pending) != 0 {
		return &UnknownEffectError{Reason: fmt.Sprintf("unresolved tool calls %v", sortedKeys(pending))}
	}
	return nil
}

func contextBytes(items []ResponsesItem) int {
	data, _ := json.Marshal(items)
	return len(data)
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
