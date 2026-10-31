package session

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"

	"github.com/agentydragon/ducktape/agentplane/observability"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/claude"
	"github.com/agentydragon/ducktape/agentplane/runner/codex"
	"github.com/agentydragon/ducktape/agentplane/runner/codexappserver"
	harness "github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"
)

type interruptTargetKey struct{}

// Run read-only reconciliation off the stdout reader, which must deliver its RPC replies.
func (s *Session) startHarness(ctx context.Context) (resultErr error) {
	s.mu.Lock()
	s.processState = runnerpb.ProcessState_PROCESS_STATE_STARTING
	s.nativeStop = stopNone
	harnessKind := s.record.Harness
	s.mu.Unlock()
	ctx, span := observability.Start(ctx, "agentplane.runner.harness.start", trace.WithAttributes(
		attribute.String("session.id", s.id), attribute.String("harness", harnessKind.String()),
	))
	defer func() { observability.End(span, resultErr) }()
	switch {
	case harnessKind == runnerpb.Harness_HARNESS_CLAUDE && s.deps.claude != nil:
		return s.startProcess(ctx, claude.New(s, *s.deps.claude))
	case harnessKind == runnerpb.Harness_HARNESS_CODEX && s.deps.codexAppServer != nil:
		return s.startOnCodexServer(ctx)
	default:
		return errors.New("this runner is not configured for the selected harness")
	}
}

func (s *Session) recoverAdapter(ctx context.Context, adapter harness.Adapter) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if recovery, ok := adapter.(harness.RecoveryAdapter); ok {
		// Finish translating retained replies before honoring preparation cancellation.
		if err := recovery.Recover(s.deps.ctx); err != nil {
			return fmt.Errorf("recover pending native requests: %w", err)
		}
	}
	return ctx.Err()
}

// startProcess runs this Session's own native harness process.
func (s *Session) startProcess(ctx context.Context, adapter harness.ProcessAdapter) error {
	if err := s.recoverAdapter(ctx, adapter); err != nil {
		return err
	}
	command, err := adapter.Command()
	if err != nil {
		return err
	}
	harnessEnv, err := adapter.Environment()
	if err != nil {
		return err
	}
	env := maps.Clone(s.deps.harnessEnvironment)
	if env == nil {
		env = map[string]string{}
	}
	maps.Copy(env, s.environment())
	maps.Copy(env, harnessEnv)
	cwd, err := s.deps.ensureWorkspace(ctx, s.Session().Cwd)
	if err != nil {
		return err
	}
	process, err := s.deps.launchProcess(ctx, ControllerName+"/"+s.id, supervision.ProcessConfig{Command: command, Dir: cwd, Env: env})
	if err != nil {
		return err
	}
	s.mu.Lock()
	s.process = process
	s.adapter = adapter
	s.processID = process.ID()
	s.nativeStop = stopNone
	s.mu.Unlock()
	s.connection.startStdout(process, func(ctx context.Context, line []byte) error { return s.receive(ctx, line, adapter) })
	s.connection.startStderr(process)
	if err := s.prepare(ctx, adapter, process.PID()); err != nil {
		s.mu.Lock()
		s.nativeStop = stopStartupFailure
		s.processState = runnerpb.ProcessState_PROCESS_STATE_STOPPING
		s.mu.Unlock()
		stopErr := process.Stop(context.WithoutCancel(ctx))
		if stopErr == nil {
			s.connection.join()
		}
		return errors.Join(fmt.Errorf("harness startup failed: %w; %s", err, process.ExitDescription()), stopErr)
	}
	return nil
}

// startOnCodexServer hosts this Session's thread on the shared Codex app-server.
func (s *Session) startOnCodexServer(ctx context.Context) error {
	if _, err := s.deps.ensureWorkspace(ctx, s.Session().Cwd); err != nil {
		return err
	}
	server := s.deps.codexAppServer
	adapter := codex.New(s)
	if err := s.recoverAdapter(ctx, adapter); err != nil {
		return err
	}
	// Frames for every hosted Session, and the exit of a start it makes, can reach this one as soon as
	// it is hosted.
	s.mu.Lock()
	s.adapter = adapter
	s.nativeStop = stopNone
	exitEpoch := s.codexExitEpoch
	s.mu.Unlock()
	attachment := s.appServerAttachment()
	p, err := server.Host(ctx, attachment)
	if err != nil {
		return err
	}
	if err := s.installCodexProcess(p, exitEpoch); err != nil {
		p.Release(attachment)
		return err
	}
	err = server.WithThreadHandshake(func() error { return s.prepare(ctx, adapter, p.PID()) })
	if err != nil {
		p.Release(attachment)
		s.mu.Lock()
		if s.codexAppServer == p {
			s.process, s.codexAppServer, s.processID = nil, nil, ""
		}
		s.mu.Unlock()
		return fmt.Errorf("harness startup failed: %w", err)
	}
	return nil
}

// An End callback may finish after Host has selected a process but before it returns to
// this Session. Do not attach that already-exited incarnation after the callback cleared it.
func (s *Session) installCodexProcess(p *codexappserver.Process, exitEpoch uint64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.codexExitEpoch != exitEpoch {
		return errors.New("Codex app-server exited before the Session could attach")
	}
	s.process = p.Process()
	s.codexAppServer = p
	s.processID = p.ID()
	return nil
}

func (s *Session) appServerAttachment() *codexappserver.Attachment {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.codexAttachment == nil {
		s.codexAttachment = &codexappserver.Attachment{
			ID: s.id,
			Frames: func(ctx context.Context, lines [][]byte) error {
				adapter := s.hostedAdapter()
				return s.connection.receiveBatch(ctx, lines, func(ctx context.Context, line []byte) error {
					return s.receive(ctx, line, adapter)
				})
			},
			Stderr: func(ctx context.Context, text string) error {
				return s.Emit(ctx, observation(&eventpb.HarnessStderr{Text: text}), nil)
			},
			End: s.harnessEnded,
		}
	}
	return s.codexAttachment
}

// prepare starts or resumes the native conversation on a started harness and reconciles the turns a
// lost harness left.
func (s *Session) prepare(ctx context.Context, adapter harness.Adapter, pid int) (resultErr error) {
	ctx, span := observability.Start(ctx, "agentplane.runner.harness.handshake", trace.WithAttributes(attribute.String("session.id", s.id)))
	defer func() { observability.End(span, resultErr) }()
	native, err := adapter.Handshake(ctx)
	if err != nil {
		return err
	}
	s.mu.Lock()
	s.record.NativeSessionID = &native
	record := *s.record
	s.mu.Unlock()
	resuming := s.runtimeState().Resuming
	if err := s.deps.store.Write(s.id, record); err != nil {
		return err
	}
	// The native handshake succeeded; persist its observation even if preparation was canceled.
	if err := s.Emit(s.deps.ctx, observation(&eventpb.HarnessStarted{Resumed: resuming, Pid: int32(pid)}), nil); err != nil {
		return err
	}
	if !resuming {
		return nil
	}
	turns, err := s.journal.RecoveryTurns(ctx)
	if err != nil {
		return err
	}
	for _, turn := range turns {
		report, err := adapter.Reconcile(ctx, turn, true)
		if err != nil {
			return err
		}
		if err := s.Emit(s.deps.ctx, observation(report), nil); err != nil {
			return err
		}
	}
	return nil
}

// dispatchControl starts each pending exact-turn interrupt independently from ordinary dispatch.
// An ordinary native RPC can block the dispatch operation while the interrupt must still reach the
// currently active turn.
func (s *Session) dispatchControl(ctx context.Context) error {
	phase := s.runtimeState().Phase
	if phase != Ready && phase != Stopping {
		return nil
	}
	commands, err := s.journal.PendingCommands(ctx)
	if err != nil {
		return err
	}
	for _, command := range commands {
		if command.GetInterruptTurn() == nil {
			continue
		}
		s.mu.Lock()
		if s.runtimeState().Phase == Halted || s.halting.Finished() || s.dispatched[command.CommandId] || s.controlDispatches[command.CommandId] != nil {
			s.mu.Unlock()
			continue
		}
		done := make(chan struct{})
		s.controlDispatches[command.CommandId] = done
		s.work.Add(1)
		s.mu.Unlock()
		go func(command *commandpb.Command, done chan struct{}) {
			defer s.work.Done()
			// The reconciliation context may end as soon as this controller pass
			// returns. Native control RPCs belong to the Session host lifetime.
			err := s.dispatch(s.deps.ctx, command)
			s.mu.Lock()
			delete(s.controlDispatches, command.CommandId)
			close(done)
			s.mu.Unlock()
			if err != nil {
				s.deps.fail(err)
			}
			s.wake()
			s.deps.changed()
		}(command, done)
	}
	return nil
}

func (s *Session) dispatch(ctx context.Context, c *commandpb.Command) error {
	s.mu.Lock()
	interrupt := c.GetInterruptTurn() != nil
	if s.dispatched[c.CommandId] || s.adapter == nil || !s.readyLocked() || (!interrupt && (s.inspection != nil || len(s.reconciliations) != 0 || s.executionClosed())) {
		s.mu.Unlock()
		return nil
	}
	s.dispatched[c.CommandId] = true
	adapter := s.adapter
	s.mu.Unlock()
	stored, err := s.journal.GetCommand(ctx, c.CommandId)
	if err != nil {
		return err
	}
	if stored == nil || stored.TerminalCursor != nil {
		return nil
	}
	if stored.DispatchPlanned {
		// This marker is committed before native I/O. Its presence cannot prove that retry is safe.
		return nil
	}
	if input := c.GetSubmitInput(); input != nil && input.Text == "" {
		return s.FailCommand(ctx, c.CommandId, "submit_input.text is required", nil)
	}
	if model := c.GetChangeModel(); model != nil {
		if model.Model == "" {
			return s.FailCommand(ctx, c.CommandId, "change_model.model is required", nil)
		}
		snapshot := s.Session()
		if model.Model == snapshot.Model {
			return s.NoopCommand(ctx, c.CommandId, "the requested model is already active", nil)
		}
		budget, e := s.deps.resolveModelConfig(ctx, s.record.Harness, model.Model)
		if e != nil {
			return s.FailCommand(ctx, c.CommandId, e.Error(), nil)
		}
		if !equalBudget(snapshot.TotalContextBudgetTokens, budget) {
			return s.FailCommand(ctx, c.CommandId, "This model switch changes the configured context budget, which Agentplane currently sets only when the harness starts. Start a new session with the requested model as a workaround.", nil)
		}
	}
	if effort := c.GetChangeReasoningEffort(); effort != nil {
		if effort.Effort == "" {
			return s.FailCommand(ctx, c.CommandId, "change_reasoning_effort.effort is required", nil)
		}
		if effort.Effort == s.Session().ReasoningEffort {
			return s.NoopCommand(ctx, c.CommandId, "the requested reasoning effort is already active", nil)
		}
	}
	if interrupt := c.GetInterruptTurn(); interrupt != nil {
		if s.Session().ActiveTurnID != interrupt.TurnId {
			return s.NoopCommand(ctx, c.CommandId, "the target turn is no longer active", nil)
		}
	}
	s.admitMu.Lock()
	if s.executionClosed() && c.GetInterruptTurn() == nil {
		s.admitMu.Unlock()
		return nil
	}
	planned, err := s.journal.DispatchPlanned(ctx, c.CommandId)
	s.admitMu.Unlock()
	if err != nil || !planned {
		return err
	}
	switch {
	case c.GetSubmitInput() != nil:
		if err = s.checkpoint(ctx, "after-dispatch-planned", c.CommandId); err != nil {
			return err
		}
		err = adapter.Submit(ctx, c.CommandId, c.GetSubmitInput().Text)
	case c.GetChangeModel() != nil:
		err = adapter.ChangeModel(ctx, c.CommandId, c.GetChangeModel().Model)
	case c.GetChangeReasoningEffort() != nil:
		err = adapter.ChangeReasoningEffort(ctx, c.CommandId, c.GetChangeReasoningEffort().Effort)
	case c.GetInterruptTurn() != nil:
		target := c.GetInterruptTurn().TurnId
		s.mu.Lock()
		active := s.activeTurn
		if active == target {
			s.interrupts[target] = c.CommandId
		}
		s.mu.Unlock()
		if active != target {
			return s.NoopCommand(ctx, c.CommandId, "the turn ended before interruption was sent", nil)
		}
		err = adapter.Interrupt(context.WithValue(ctx, interruptTargetKey{}, target), target)
	default:
		err = errors.New("unrecognized command operation")
	}
	if err != nil {
		if errors.Is(err, harness.ErrNativeResponsePending) {
			return nil
		}
		if errors.Is(err, errStaleInterrupt) {
			return s.NoopCommand(ctx, c.CommandId, err.Error(), nil)
		}
		return s.FailCommand(ctx, c.CommandId, err.Error(), nil)
	}
	return nil
}
func equalBudget(a, b *int) bool { return a == nil && b == nil || a != nil && b != nil && *a == *b }

var errStaleInterrupt = errors.New("the target turn ended before interruption was sent")

func (s *Session) SendNative(ctx context.Context, frame json.RawMessage) error {
	return s.sendNative(ctx, ctx, frame)
}

func (s *Session) sendNative(ctx, journalCtx context.Context, frame json.RawMessage) error {
	if err := s.Emit(journalCtx, observation(&eventpb.Native{Direction: eventpb.Direction_DIRECTION_TO_HARNESS, Line: string(frame)}), nil); err != nil {
		return err
	}
	if err := s.journal.CommitBatch(journalCtx); err != nil {
		return err
	}
	s.connection.deliverReplies()
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	s.mu.Lock()
	process := s.process
	shared := s.codexAppServer
	attachment := s.codexAttachment
	target, _ := ctx.Value(interruptTargetKey{}).(string)
	active := s.activeTurn
	s.mu.Unlock()
	if target != "" && target != active {
		return errStaleInterrupt
	}
	if process == nil || !process.Running() {
		return errors.New("the harness is not running")
	}
	if shared != nil {
		if err := shared.RegisterSent(attachment, frame); err != nil {
			return err
		}
	}
	if err := process.WriteLine(ctx, string(frame)); err != nil {
		return &harness.NativeResponsePendingError{Cause: err}
	}
	return nil
}

// RequestNativeHandshake sequences native startup and read-only history exchanges. It must not
// run on the stdout reader that delivers the reply. Runtime commands use send-and-observe
// transitions instead, so their scheduler never waits here for a native response.
func (s *Session) RequestNativeHandshake(ctx context.Context, frame json.RawMessage, match func(json.RawMessage) bool) (harness.NativeReceipt, error) {
	return s.connection.request(ctx, frame, match)
}

func (s *Session) receive(ctx context.Context, line []byte, adapter harness.Adapter) error {
	if err := s.checkpointNativeResponse(ctx, line); err != nil {
		return err
	}
	entry, err := s.recordEvent(ctx, observation(&eventpb.Native{Direction: eventpb.Direction_DIRECTION_FROM_HARNESS, Line: string(line)}), nil, nil)
	if err != nil {
		return err
	}
	if !json.Valid(line) || len(bytes.TrimSpace(line)) == 0 || bytes.TrimSpace(line)[0] != '{' {
		zap.L().Warn("non-object frame on harness stdout", zap.String("session", s.id), zap.Uint64("sequence", entry.Origin.Sequence))
		return nil
	}
	frame := append(json.RawMessage(nil), line...)
	if err := adapter.OnFrame(ctx, frame, entry.Origin.Sequence); err != nil {
		// Native protocols may acquire fields independently of the runner. Preserve the
		// original frame and report the translation failure; storage failures stop us.
		var storageErr *journal.StorageError
		if errors.Is(err, context.Canceled) || errors.As(err, &storageErr) {
			return err
		}
		zap.L().Error("native frame translation failed", zap.String("session", s.id), zap.Uint64("sequence", entry.Origin.Sequence), zap.Error(err))
	}
	s.connection.match(harness.NativeReceipt{Frame: frame, Sequence: entry.Origin.Sequence})
	return nil
}

func (s *Session) harnessEnded(ctx context.Context, code int) error {
	s.mu.Lock()
	s.codexExitEpoch++
	stoppedByRunner := s.nativeStop != stopNone
	s.codexAppServer = nil
	s.processID = ""
	s.processState = runnerpb.ProcessState_PROCESS_STATE_UNSPECIFIED
	s.mu.Unlock()
	s.connection.end(code)
	// History may end its stream at HarnessExited. Publish the lost turn and its
	// recovery evidence before exposing that terminal boundary.
	if turn := s.Session().ActiveTurnID; turn != "" {
		if err := s.completeTurn(ctx, &eventpb.TurnCompleted{TurnId: turn, Status: eventpb.TurnStatus_TURN_STATUS_PROCESS_LOST, Error: fmt.Sprintf("the harness exited with exit_code=%d during the turn", code)}, nil); err != nil {
			return err
		}
	}
	if err := s.Emit(ctx, observation(&eventpb.HarnessExited{ExitCode: int32(code), StoppedByRunner: stoppedByRunner}), nil); err != nil {
		return err
	}
	s.wake()
	return nil
}
