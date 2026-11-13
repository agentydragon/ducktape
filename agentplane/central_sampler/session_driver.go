package centralsampler

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"google.golang.org/protobuf/proto"
)

func (r *Runtime) drive(runtimeCtx context.Context, sessionID string, task *residentSession) {
	loadCtx, cancelLoad := context.WithTimeout(context.Background(), time.Duration(r.settings.RPCTimeoutSeconds)*time.Second)
	session, err := r.store.GetSession(loadCtx, sessionID, "")
	cancelLoad()
	if err != nil {
		if !errors.Is(err, protocol.ErrMissing) {
			slog.Error("central sampler could not load Session", "session_id", sessionID, "error", err)
		}
		return
	}
	if r.isStopping() {
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
	slog.Info("central sampler lease claimed", "session_id", sessionID, "conversation_id", owner.ConversationID,
		"generation", owner.Generation, "owner_token", owner.Token, "pod_name", os.Getenv("HOSTNAME"))
	if runtimeCtx.Err() != nil || r.isStopping() {
		r.release(owner, sessionID, releaseStopping)
		return
	}
	r.mu.Lock()
	task.owner = &owner
	r.mu.Unlock()
	executionCtx, cancelExecution := context.WithCancel(runtimeCtx)
	heartbeatDone := make(chan error, 1)
	go func() {
		err := r.heartbeat(executionCtx, owner)
		if err != nil {
			cancelExecution()
		}
		heartbeatDone <- err
	}()
	activeCommand, executionErr := r.execute(executionCtx, sessionID, owner, task)
	cancelExecution()
	heartbeatErr := <-heartbeatDone
	reason := releaseSettled
	switch {
	case heartbeatErr != nil:
		reason = releaseOwnershipLost
	case runtimeCtx.Err() != nil:
		reason = releaseShutdownDeadline
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
func (r *Runtime) logDrainExit(sessionID string, owner Owner, activeCommand *string) {
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
func (r *Runtime) cleanupTimeout() time.Duration {
	timeout := time.Duration(r.settings.RPCTimeoutSeconds) * time.Second
	r.mu.Lock()
	shutdownDeadline := r.shutdownDeadline
	r.mu.Unlock()
	if !shutdownDeadline.IsZero() {
		timeout = min(timeout, 5*time.Second, time.Until(shutdownDeadline))
	}
	return timeout
}

func (r *Runtime) release(owner Owner, sessionID string, reason releaseReason) {
	timeout := r.cleanupTimeout()
	if timeout <= 0 {
		slog.Warn("central sampler cleanup budget expired before lease release", "session_id", sessionID, "conversation_id", owner.ConversationID,
			"generation", owner.Generation, "reason", string(reason))
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	if err := r.releaseLease(ctx, owner); err != nil {
		var lost *LostOwnershipError
		if !errors.As(err, &lost) {
			slog.Error("central sampler lease release failed", "session_id", sessionID, "conversation_id", owner.ConversationID,
				"generation", owner.Generation, "reason", string(reason), "error", err)
		}
		return
	}
	slog.Info("central sampler lease released", "session_id", sessionID, "conversation_id", owner.ConversationID,
		"generation", owner.Generation, "reason", string(reason), "pod_name", os.Getenv("HOSTNAME"))
}

func (r *Runtime) isStopping() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.stopping
}

func (r *Runtime) isDraining() bool {
	select {
	case <-r.draining:
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
func (r *Runtime) drainReleases(ctx context.Context, sessionID string, owner Owner, activeCommand *string, continued *bool) (bool, error) {
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

func (r *Runtime) execute(ctx context.Context, sessionID string, owner Owner, task *residentSession) (*string, error) {
	if !r.acquireExecution(ctx, task) {
		return nil, nil
	}
	held := true
	defer func() {
		if held {
			r.releaseExecution(task)
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
	var reconciled <-chan struct{}
	drainContinued := false
	client := &http.Client{}
	for {
		if ctx.Err() != nil {
			return activeCommand, ctx.Err()
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
				if process.Status != protocol.ProcessExitedUnread && process.Status != protocol.ProcessExited {
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
				r.releaseExecution(task)
				held = false
				select {
				case <-ctx.Done():
					return nil, ctx.Err()
				case <-r.draining:
					return nil, nil
				case <-task.wake:
				}
				if !r.acquireExecution(ctx, task) {
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
			changes := live.executor.processChanges()
			if changes != reconciled {
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
			case <-task.wake:
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
		turnCtx, cancel := context.WithTimeout(ctx, time.Duration(r.settings.TurnTimeoutSeconds)*time.Second)
		var result TurnResult
		if payload.GetCompact() != nil {
			err = r.compact(turnCtx, owner, session.Model)
			result = TurnCompact
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
				if err != nil || result.endsTurn() {
					break
				}
			}
		}
		cancel()
		if err != nil {
			return activeCommand, err
		}
		if result == "" {
			result = TurnDone
		}
		if err := r.store.FinishCommand(ctx, owner, sessionID, command.ID, result); err != nil {
			return activeCommand, err
		}
		activeCommand = nil
		if result == TurnEffectsUnknown {
			return nil, nil
		}
		if result == TurnInterrupted && live != nil {
			closeCtx, closeCancel := context.WithTimeout(context.Background(), 5*time.Second)
			_ = live.Close(closeCtx)
			closeCancel()
			live = nil
		}
	}
}
