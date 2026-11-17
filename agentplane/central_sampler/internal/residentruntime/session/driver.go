package session

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/executionstate"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/executor"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"google.golang.org/protobuf/proto"
)

type Driver struct {
	store           *sessionstore.Store
	settings        samplersettings.Settings
	claimLease      func(context.Context, string, time.Duration) (sessionstore.Owner, error)
	releaseLease    func(context.Context, sessionstore.Owner) error
	newExecutor     func(context.Context, *sessionstore.Store, sessionstore.Owner, *sessionstore.Session, samplersettings.Settings) (*executor.Executor, func() error, error)
	openCodeSession func(context.Context, string, sessionstore.Owner, *executor.Executor) (*CodeMode, error)
	onTurnExpiry    func(TurnExpiryEvidence)

	codeHost *CodeModeHost
	slot     Slot
}

// TurnExpiryEvidence records the state used to decide whether an expired command had a settled
// provider result. It is exposed to runtime tests so an intermittent unknown outcome leaves a
// useful undeclared artifact without changing execution behavior.
type TurnExpiryEvidence struct {
	CommandID                 string `json:"command_id"`
	TurnContextError          string `json:"turn_context_error,omitempty"`
	ExecutionContextError     string `json:"execution_context_error,omitempty"`
	StepResult                string `json:"step_result,omitempty"`
	StepError                 string `json:"step_error,omitempty"`
	Expired                   bool   `json:"expired"`
	SettlementStage           string `json:"settlement_stage"`
	CurrentAttemptCommandID   string `json:"current_attempt_command_id,omitempty"`
	RecoveryFrontierCommandID string `json:"recovery_frontier_command_id,omitempty"`
	CompletionRejection       string `json:"completion_rejection,omitempty"`
	ConversationPending       bool   `json:"conversation_pending"`
	ConversationStopped       bool   `json:"conversation_stopped"`
	TranscriptComplete        bool   `json:"transcript_complete"`
	TranscriptCheckError      string `json:"transcript_check_error,omitempty"`
	SessionConversationMatch  bool   `json:"session_conversation_match"`
	SessionBlocked            bool   `json:"session_blocked"`
	SessionActiveCommand      string `json:"session_active_command,omitempty"`
	SessionActiveTurn         string `json:"session_active_turn,omitempty"`
	Settled                   bool   `json:"settled"`
	SettlementError           string `json:"settlement_error,omitempty"`
}

type Dependencies struct {
	Store           *sessionstore.Store
	Settings        samplersettings.Settings
	ClaimLease      func(context.Context, string, time.Duration) (sessionstore.Owner, error)
	ReleaseLease    func(context.Context, sessionstore.Owner) error
	NewExecutor     func(context.Context, *sessionstore.Store, sessionstore.Owner, *sessionstore.Session, samplersettings.Settings) (*executor.Executor, func() error, error)
	OpenCodeSession func(context.Context, string, sessionstore.Owner, *executor.Executor) (*CodeMode, error)
	CodeHost        *CodeModeHost
	OnTurnExpiry    func(TurnExpiryEvidence)
}

func New(deps Dependencies, slot Slot) *Driver {
	return &Driver{
		store: deps.Store, settings: deps.Settings,
		claimLease: deps.ClaimLease, releaseLease: deps.ReleaseLease,
		newExecutor: deps.NewExecutor, openCodeSession: deps.OpenCodeSession,
		codeHost: deps.CodeHost, slot: slot, onTurnExpiry: deps.OnTurnExpiry,
	}
}

func diagnosticError(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func (r *Driver) reportTurnExpiry(evidence *TurnExpiryEvidence) {
	if evidence != nil && r.onTurnExpiry != nil {
		r.onTurnExpiry(*evidence)
	}
}

// A Slot coordinates one driver's residency with replica scheduling.
// Leases and their fencing remain the driver's responsibility.
type Slot interface {
	Wake() <-chan struct{}
	Drain() <-chan struct{}
	IsStopping() bool
	ShutdownDeadline() time.Time
	SetOwner(sessionstore.Owner)
	SetWaitingProvider(bool)
	AcquireExecution(context.Context) bool
	ReleaseExecution()
}

func (r *Driver) Drive(runtimeCtx context.Context, sessionID string) {
	loadCtx, cancelLoad := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
	session, err := r.store.GetSession(loadCtx, sessionID, "")
	cancelLoad()
	if err != nil {
		if !errors.Is(err, protocol.ErrMissing) {
			slog.Error("central sampler could not load Session", "session_id", sessionID, "error", err)
		}
		return
	}
	if r.slot.IsStopping() {
		return
	}
	claimCtx, claimCancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
	owner, err := r.claimLease(claimCtx, session.ConversationID, time.Duration(r.settings.LeaseSeconds)*time.Second)
	claimCancel()
	if err != nil {
		var lost *sessionstore.LostOwnershipError
		if !errors.As(err, &lost) && !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded) {
			slog.Error("central sampler lease claim failed", "session_id", sessionID, "error", err)
		}
		return
	}
	slog.Info("central sampler lease claimed", "session_id", sessionID, "conversation_id", owner.ConversationID,
		"generation", owner.Generation, "owner_token", owner.Token, "pod_name", os.Getenv("HOSTNAME"))
	if runtimeCtx.Err() != nil || r.slot.IsStopping() {
		r.release(owner, sessionID, releaseStopping)
		return
	}
	r.slot.SetOwner(owner)
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
	reason := releaseSettled
	switch {
	case heartbeatErr != nil:
		reason = releaseOwnershipLost
	case runtimeCtx.Err() != nil:
		reason = releaseShutdownDeadline
	case errors.As(executionErr, new(*deferredProviderError)):
		reason = releaseFailed
		slog.Warn("central sampler retained provider recovery state", "session_id", sessionID, "error", executionErr)
	case executionErr != nil:
		var known *protocol.KnownFailureError
		if errors.As(executionErr, &known) {
			reason = releaseRejected
			slog.Warn("central sampler rejected execution", "session_id", sessionID, "reason", known.Error())
		} else {
			reason = releaseFailed
			slog.Error("central sampler execution stopped", "session_id", sessionID, "error", executionErr)
		}
		failCtx, cancel := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
		if err := r.store.FailExecution(failCtx, owner, sessionID, activeCommand, executionErr); err != nil && !errors.Is(err, context.Canceled) {
			slog.Error("central sampler could not record execution failure", "session_id", sessionID, "error", err)
		}
		cancel()
	case r.isDraining():
		reason = releaseDrain
	}
	if r.isDraining() {
		r.logDrainExit(sessionID, owner, activeCommand)
	}
	r.release(owner, sessionID, reason)
}

// logDrainExit records the Session state a driver leaves behind during shutdown drain, so a
// successor that blocks on a lost host can be traced to the owner that released it.
func (r *Driver) logDrainExit(sessionID string, owner sessionstore.Owner, activeCommand *string) {
	timeout := r.cleanupTimeout()
	if timeout <= 0 {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	command := ""
	if activeCommand != nil {
		command = *activeCommand
	}
	work, err := r.store.LiveWork(ctx, owner)
	if err != nil {
		slog.Error("central sampler could not read live work at drain exit", "session_id", sessionID, "conversation_id", owner.ConversationID,
			"generation", owner.Generation, "active_command", command, "error", err)
		return
	}
	slog.Info("central sampler drain releasing Session", "session_id", sessionID, "conversation_id", owner.ConversationID,
		"generation", owner.Generation, "active_command", command, "live_cells", work.LiveCells, "live_processes", work.LiveProcesses,
		"unresolved_intents", work.UnresolvedIntents, "pending", work.Pending, "pod_name", os.Getenv("HOSTNAME"))
}

func (r *Driver) heartbeat(ctx context.Context, owner sessionstore.Owner) error {
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

// releaseReason names why a driver gave up its Session lease.
type releaseReason string

const (
	// releaseStopping: the claim committed after shutdown began; the driver never served.
	releaseStopping releaseReason = "stopping"
	// releaseDrain: shutdown drain released an idle Session or one whose live work settled.
	releaseDrain releaseReason = "drain"
	// releaseShutdownDeadline: the drain deadline cancelled active work; unresolved intents remain.
	releaseShutdownDeadline releaseReason = "shutdown_deadline"
	releaseOwnershipLost    releaseReason = "ownership_lost"
	releaseRejected         releaseReason = "rejected"
	releaseFailed           releaseReason = "failed"
	// releaseSettled: the driver stopped outside drain without an error (unserviceable Session,
	// failed setup, or interrupt with unknown effects).
	releaseSettled releaseReason = "settled"
)

// cleanupTimeout bounds a store call made while the driver winds down. Zero or less means the
// shutdown budget is already spent.
func (r *Driver) cleanupTimeout() time.Duration {
	timeout := time.Duration(r.settings.RPCTimeoutSeconds) * time.Second
	shutdownDeadline := r.slot.ShutdownDeadline()
	if !shutdownDeadline.IsZero() {
		timeout = min(timeout, 5*time.Second, time.Until(shutdownDeadline))
	}
	return timeout
}

func (r *Driver) release(owner sessionstore.Owner, sessionID string, reason releaseReason) {
	timeout := r.cleanupTimeout()
	if timeout <= 0 {
		slog.Warn("central sampler cleanup budget expired before lease release", "session_id", sessionID, "conversation_id", owner.ConversationID,
			"generation", owner.Generation, "reason", string(reason))
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	if err := r.releaseLease(ctx, owner); err != nil {
		var lost *sessionstore.LostOwnershipError
		if !errors.As(err, &lost) {
			slog.Error("central sampler lease release failed", "session_id", sessionID, "conversation_id", owner.ConversationID,
				"generation", owner.Generation, "reason", string(reason), "error", err)
		}
		return
	}
	slog.Info("central sampler lease released", "session_id", sessionID, "conversation_id", owner.ConversationID,
		"generation", owner.Generation, "reason", string(reason), "pod_name", os.Getenv("HOSTNAME"))
}

func (r *Driver) isDraining() bool {
	select {
	case <-r.slot.Drain():
		return true
	default:
		return false
	}
}

// drainReleases reports whether a draining driver releases its Session now. An effect in flight
// (unresolved intent, pending provider step) always keeps the owner: detaching the host would
// cancel it mid-way. A live cell or process handle keeps the owner only while a command is active,
// because only the turn in progress can settle it (the model's `wait`); an idle Session with a
// background cell releases immediately, as the successor sees the same live handle either way.
// Serving continues until that work settles or the drain deadline cancels it. The continuation
// is logged once per driver.
func (r *Driver) drainReleases(ctx context.Context, sessionID string, owner sessionstore.Owner, activeCommand *string, continued *bool) (bool, error) {
	if !r.isDraining() {
		return false, nil
	}
	work, err := r.store.LiveWork(ctx, owner)
	if err != nil {
		return false, err
	}
	if !work.KeepsOwner(activeCommand != nil) {
		return true, nil
	}
	if !*continued {
		*continued = true
		slog.Info("central sampler drain keeps serving Session with live work", "session_id", sessionID, "conversation_id", owner.ConversationID,
			"generation", owner.Generation, "live_cells", work.LiveCells, "live_processes", work.LiveProcesses,
			"unresolved_intents", work.UnresolvedIntents, "pending", work.Pending, "pod_name", os.Getenv("HOSTNAME"))
	}
	return false, nil
}

func (r *Driver) execute(ctx context.Context, sessionID string, owner sessionstore.Owner) (*string, error) {
	if !r.slot.AcquireExecution(ctx) {
		return nil, nil
	}
	held := true
	defer func() {
		if held {
			r.slot.ReleaseExecution()
		}
	}()
	if r.isDraining() {
		return nil, nil
	}
	setupOK, err := r.setup(ctx, sessionID, owner)
	if err != nil || !setupOK {
		return nil, err
	}
	sessionForHost, err := r.store.GetSession(ctx, sessionID, "")
	if err != nil {
		return nil, err
	}
	encoding := r.settings.Models[sessionForHost.Model].ExecEncoding
	if r.isDraining() {
		return nil, nil
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
	var timedCommand string
	var turnDeadline time.Time
	var reconciled <-chan struct{}
	drainContinued := false
	client := &http.Client{}
	for {
		if ctx.Err() != nil {
			return activeCommand, ctx.Err()
		}
		if !held {
			if !r.slot.AcquireExecution(ctx) {
				return activeCommand, ctx.Err()
			}
			held = true
		}
		if live != nil && live.code.Err() != nil {
			closeCtx, closeCancel := context.WithTimeout(context.Background(), 5*time.Second)
			_ = live.Close(closeCtx)
			closeCancel()
			live = nil
			reconciled = nil
		}
		changed, err := r.store.ReconcileCodeHostLoss(ctx, owner, r.codeHost.InstanceID(), encoding)
		if err != nil {
			return activeCommand, err
		}
		if changed {
			continue
		}
		if release, err := r.drainReleases(ctx, sessionID, owner, activeCommand, &drainContinued); err != nil || release {
			return activeCommand, err
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
				if process.Status != executionstate.ProcessExitedUnread && process.Status != executionstate.ProcessExited {
					runningProcess = true
					break
				}
			}
			if len(state.Cells) == 0 && !runningProcess {
				if live != nil {
					closeCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
					closeErr := live.Close(closeCtx)
					cancel()
					live = nil
					reconciled = nil
					if closeErr != nil {
						return nil, closeErr
					}
				}
				r.slot.ReleaseExecution()
				held = false
				select {
				case <-ctx.Done():
					return nil, ctx.Err()
				case <-r.slot.Drain():
					return nil, nil
				case <-r.slot.Wake():
				}
				if !r.slot.AcquireExecution(ctx) {
					return nil, nil
				}
				held = true
				continue
			}
			if live == nil {
				live, err = r.connectHost(ctx, sessionID, owner)
				if errors.Is(err, codemode.ErrHostInstanceChanged) {
					continue
				}
				if err != nil {
					return activeCommand, err
				}
			}
			var changes <-chan struct{}
			if live.executor != nil {
				changes = live.executor.ProcessChanges()
			}
			if live.executor != nil && changes != reconciled {
				progressed, err := live.executor.ReconcileProcesses(ctx, time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
				if err != nil {
					return activeCommand, err
				}
				if progressed {
					continue
				}
				reconciled = changes
			}
			// Live cells still require the host's existing periodic check. Process
			// reads are driven by executor notifications, not this timer or work scans.
			var timer *time.Timer
			var tick <-chan time.Time
			if len(state.Cells) != 0 {
				timer = time.NewTimer(250 * time.Millisecond)
				tick = timer.C
			}
			select {
			case <-ctx.Done():
				if timer != nil {
					timer.Stop()
				}
				return activeCommand, ctx.Err()
			case <-liveDone(live):
				if timer != nil {
					timer.Stop()
				}
				if err := live.code.Err(); err != nil {
					return activeCommand, err
				}
				return activeCommand, errors.New("code-mode session detached while remote effects were live")
			case <-r.slot.Wake():
				if timer != nil {
					timer.Stop()
				}
			case <-reconciled:
				if timer != nil {
					timer.Stop()
				}
			case <-tick:
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
		if release, err := r.drainReleases(ctx, sessionID, owner, activeCommand, &drainContinued); err != nil || release {
			return activeCommand, err
		}
		var turnCtx context.Context
		var cancel context.CancelFunc
		if seconds := r.settings.TurnTimeoutSeconds; seconds != nil {
			if timedCommand != command.ID {
				timedCommand = command.ID
				turnDeadline = time.Now().Add(time.Duration(*seconds) * time.Second)
			}
			turnCtx, cancel = context.WithDeadline(ctx, turnDeadline)
		} else {
			turnCtx, cancel = context.WithCancel(ctx)
		}
		var result sessionstore.TurnResult
		if payload.GetCompact() != nil {
			conversation, readErr := r.store.Read(turnCtx, owner)
			if readErr != nil {
				err = readErr
			} else if conversation.Stopped && conversation.Pending == nil {
				result = sessionstore.TurnInterrupted
			} else {
				err = r.compact(turnCtx, owner, session.Model)
				result = sessionstore.TurnCompact
			}
		} else {
			for {
				if live != nil && live.code.Err() != nil {
					closeCtx, closeCancel := context.WithTimeout(context.Background(), 5*time.Second)
					_ = live.Close(closeCtx)
					closeCancel()
					live = nil
				}
				changed, reconcileErr := r.store.ReconcileCodeHostLoss(turnCtx, owner, r.codeHost.InstanceID(), encoding)
				if reconcileErr != nil {
					err = reconcileErr
					break
				}
				if changed {
					continue
				}
				if release, releaseErr := r.drainReleases(turnCtx, sessionID, owner, activeCommand, &drainContinued); releaseErr != nil || release {
					cancel()
					return activeCommand, releaseErr
				}
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
					calls, callErr := conversation.Transcript.PendingCalls()
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
							if errors.Is(err, codemode.ErrHostInstanceChanged) {
								continue
							}
							if err != nil {
								break
							}
						}
					}
				}
				result, err = r.controlledAdvance(turnCtx, sessionID, owner, live, client, session)
				if err != nil || result.EndsTurn() {
					break
				}
			}
		}
		if errors.Is(err, errProviderCommandFailed) {
			cancel()
			activeCommand = nil
			continue
		}
		if errors.Is(err, errProviderRecovery) {
			r.slot.SetWaitingProvider(true)
			r.slot.ReleaseExecution()
			held = false
			err = r.waitProviderRecovery(turnCtx, owner, sessionID, live)
			r.slot.SetWaitingProvider(false)
			if err == nil {
				cancel()
				continue
			}
		}
		expired := errors.Is(turnCtx.Err(), context.DeadlineExceeded) && ctx.Err() == nil
		var expiryEvidence *TurnExpiryEvidence
		if err != nil || turnCtx.Err() != nil || ctx.Err() != nil {
			expiryEvidence = &TurnExpiryEvidence{
				CommandID: command.ID, TurnContextError: diagnosticError(turnCtx.Err()),
				ExecutionContextError: diagnosticError(ctx.Err()), StepResult: string(result),
				StepError: diagnosticError(err), Expired: expired, SettlementStage: "not_attempted",
			}
		}
		cancel()
		if expired && err != nil {
			settled, settleErr := r.settleExpiredProviderCommandWithEvidence(ctx, owner, sessionID, command.ID, expiryEvidence)
			if expiryEvidence != nil {
				expiryEvidence.Settled = settled
				expiryEvidence.SettlementError = diagnosticError(settleErr)
			}
			r.reportTurnExpiry(expiryEvidence)
			if settleErr != nil {
				return activeCommand, &deferredProviderError{cause: settleErr}
			}
			if settled {
				activeCommand = nil
				continue
			}
			return activeCommand, context.DeadlineExceeded
		}
		if err != nil {
			r.reportTurnExpiry(expiryEvidence)
			return activeCommand, err
		}
		if result == "" {
			result = sessionstore.TurnDone
		}
		if err := r.store.FinishCommand(ctx, owner, sessionID, command.ID, result); err != nil {
			return activeCommand, err
		}
		activeCommand = nil
		if result == sessionstore.TurnEffectsUnknown {
			return nil, nil
		}
		if result == sessionstore.TurnInterrupted && live != nil {
			closeCtx, closeCancel := context.WithTimeout(context.Background(), 5*time.Second)
			_ = live.Close(closeCtx)
			closeCancel()
			live = nil
		}
	}
}

// A configured turn limit stops provider work after the call has joined. A response whose
// persistence won the cancellation race can already prove the turn complete. Otherwise settle
// provider-only failure; native work still follows FailExecution's uncertainty checks.
func (r *Driver) settleExpiredProviderCommand(ctx context.Context, owner sessionstore.Owner, sessionID, commandID string) (bool, error) {
	return r.settleExpiredProviderCommandWithEvidence(ctx, owner, sessionID, commandID, nil)
}

func (r *Driver) settleExpiredProviderCommandWithEvidence(ctx context.Context, owner sessionstore.Owner, sessionID, commandID string,
	evidence *TurnExpiryEvidence,
) (bool, error) {
	ctx, cancel := context.WithTimeout(ctx, time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
	defer cancel()
	if evidence != nil {
		evidence.SettlementStage = "current_provider_attempt"
	}
	attempt, err := r.store.CurrentProviderAttempt(ctx, owner, sessionID)
	if err != nil {
		return false, err
	}
	const reason = "configured turn time limit exceeded"
	if attempt != nil {
		if evidence != nil {
			evidence.CurrentAttemptCommandID = attempt.CommandID
		}
		if attempt.CommandID != commandID {
			if evidence != nil {
				evidence.CompletionRejection = "current_provider_attempt_belongs_to_another_command"
			}
			return false, protocol.ErrPrecondition
		}
		if evidence != nil {
			evidence.SettlementStage = "abandon_current_provider_attempt"
		}
		if err := r.store.AbandonProviderAttempt(ctx, owner, attempt.IntentID, sessionstore.ProviderAttemptFailure{
			Class: sessionstore.ProviderAttemptPermanent, Reason: reason,
		}); err != nil {
			return false, err
		}
	} else {
		if evidence != nil {
			evidence.SettlementStage = "provider_recovery_frontier"
		}
		attempt, err = r.store.ProviderRecoveryFrontier(ctx, owner, sessionID)
		if err != nil {
			return false, err
		}
		if attempt == nil {
			if evidence != nil {
				evidence.SettlementStage = "completed_input_turn"
			}
			return r.finishCompletedInputTurnWithEvidence(ctx, owner, sessionID, commandID, evidence)
		}
		if evidence != nil {
			evidence.RecoveryFrontierCommandID = attempt.CommandID
		}
		if attempt.CommandID != commandID {
			if evidence != nil {
				evidence.CompletionRejection = "provider_recovery_frontier_belongs_to_another_command"
			}
			return false, protocol.ErrPrecondition
		}
	}
	if evidence != nil {
		evidence.SettlementStage = "fail_provider_command"
	}
	return true, r.store.FailProviderCommand(ctx, owner, sessionID, commandID, attempt.IntentID, reason)
}

// This only records an already-completed input turn; it cannot dispatch another provider or
// native step. Read and FinishCommand both retain the original lease generation fence.
func (r *Driver) finishCompletedInputTurn(ctx context.Context, owner sessionstore.Owner, sessionID, commandID string) (bool, error) {
	return r.finishCompletedInputTurnWithEvidence(ctx, owner, sessionID, commandID, nil)
}

func (r *Driver) finishCompletedInputTurnWithEvidence(ctx context.Context, owner sessionstore.Owner, sessionID, commandID string,
	evidence *TurnExpiryEvidence,
) (bool, error) {
	conversation, err := r.store.Read(ctx, owner)
	if err != nil {
		return false, err
	}
	if evidence != nil {
		evidence.ConversationPending = conversation.Pending != nil
		evidence.ConversationStopped = conversation.Stopped
		evidence.TranscriptComplete = conversation.Transcript.TurnComplete()
		if settledErr := conversation.Transcript.CheckSettled(); settledErr != nil {
			evidence.TranscriptCheckError = settledErr.Error()
		}
	}
	if conversation.Pending != nil {
		if evidence != nil {
			evidence.CompletionRejection = "conversation_has_pending_intent"
		}
		return false, nil
	}
	if conversation.Stopped {
		if evidence != nil {
			evidence.CompletionRejection = "conversation_stopped"
		}
		return false, nil
	}
	if !conversation.Transcript.TurnComplete() {
		if evidence != nil {
			evidence.CompletionRejection = "transcript_not_turn_complete"
		}
		return false, nil
	}
	if settledErr := conversation.Transcript.CheckSettled(); settledErr != nil {
		if evidence != nil {
			evidence.CompletionRejection = "transcript_not_settled"
			evidence.TranscriptCheckError = settledErr.Error()
		}
		return false, nil
	}
	session, err := r.store.GetSession(ctx, sessionID, "")
	if err != nil {
		return false, err
	}
	if evidence != nil {
		evidence.SessionConversationMatch = session.ConversationID == owner.ConversationID
		evidence.SessionBlocked = session.Blocked
		evidence.SessionActiveCommand = stringPointerValue(session.ActiveCommand)
		evidence.SessionActiveTurn = stringPointerValue(session.ActiveTurn)
	}
	if session.ConversationID != owner.ConversationID {
		if evidence != nil {
			evidence.CompletionRejection = "session_conversation_mismatch"
		}
		return false, nil
	}
	if session.Blocked {
		if evidence != nil {
			evidence.CompletionRejection = "session_blocked"
		}
		return false, nil
	}
	if session.ActiveCommand == nil || *session.ActiveCommand != commandID {
		if evidence != nil {
			evidence.CompletionRejection = "session_active_command_mismatch"
		}
		return false, nil
	}
	if session.ActiveTurn == nil || *session.ActiveTurn != commandID {
		if evidence != nil {
			evidence.CompletionRejection = "session_active_turn_mismatch"
		}
		return false, nil
	}
	if evidence != nil {
		evidence.SettlementStage = "finish_completed_input_turn"
	}
	return true, r.store.FinishCommand(ctx, owner, sessionID, commandID, sessionstore.TurnDone)
}

func stringPointerValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}
