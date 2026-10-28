package runner

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/claude"
	"github.com/agentydragon/ducktape/agentplane/runner/codex"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/google/uuid"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

const (
	autosuspendActiveTurn     = "ActiveTurn"
	autosuspendPendingCommand = "PendingCommand"
	autosuspendSetup          = "Setup"
	autosuspendActivation     = "Activation"
)

// observation wraps one generated observation in the Event vocabulary. The generated
// descriptor owns the mapping, avoiding a second list of observation field names.
func observation(value proto.Message) *eventpb.Event {
	e := &eventpb.Event{}
	d := e.ProtoReflect().Descriptor().Fields()
	for i := 0; i < d.Len(); i++ {
		f := d.Get(i)
		if f.ContainingOneof() != nil && f.Message() != nil && f.Message().FullName() == value.ProtoReflect().Descriptor().FullName() {
			e.ProtoReflect().Set(f, protoreflect.ValueOfMessage(value.ProtoReflect()))
			return e
		}
	}
	panic(fmt.Sprintf("%T is not a runner observation", value))
}

type nativeWaiter struct {
	matches func(json.RawMessage) bool
	result  chan nativeResult
}
type nativeResult struct {
	receipt harness.NativeReceipt
	err     error
}
type matchedReply struct {
	waiter  *nativeWaiter
	receipt harness.NativeReceipt
}
type interruptTargetKey struct{}

// Run read-only reconciliation off the stdout reader, which must deliver its RPC replies.
type pendingReconciliation struct {
	turnID  string
	adapter harness.Adapter
	sources []uint64
	ready   bool
}

type Session struct {
	runner                                                 *Runner
	id                                                     string
	record                                                 *SessionRecord
	journal                                                *Journal
	mu                                                     sync.Mutex
	admitMu                                                sync.Mutex
	writeMu                                                sync.Mutex
	process                                                *Process
	adapter                                                harness.Adapter
	processID                                              string
	processState                                           runnerpb.ProcessState
	nativeStop                                             stopReason
	preparation, dispatching, inspection, halting, sealing *sessionOperation
	controlDispatches                                      map[string]*sessionOperation
	reconciliations                                        []*pendingReconciliation
	activeTurn                                             string
	setup                                                  runnerpb.SetupState
	launch                                                 runnerpb.LaunchOutcome
	failure                                                *uint64
	setupDone                                              chan struct{}
	readers                                                sync.WaitGroup
	work                                                   sync.WaitGroup
	pending                                                map[string]bool
	dispatched                                             map[string]bool
	interrupts                                             map[string]string
	waiters                                                []*nativeWaiter
	replies                                                []matchedReply

	// codex is the shared app-server incarnation hosting this Codex Session's thread.
	codex *codexProcess
}

func newSession(r *Runner, id string, record *SessionRecord, journal *Journal) *Session {
	s := &Session{runner: r, id: id, record: record, journal: journal, pending: map[string]bool{}, dispatched: map[string]bool{}, controlDispatches: map[string]*sessionOperation{}, interrupts: map[string]string{}, launch: runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING, setup: runnerpb.SetupState_SETUP_STATE_NOT_REQUIRED}
	if record.SetupScriptSHA256 != nil {
		s.setup = runnerpb.SetupState_SETUP_STATE_RUNNING
	}
	s.activeTurn = journal.RecoveryState().ActiveTurnID
	return s
}

func (s *Session) Session() harness.SessionSnapshot {
	s.mu.Lock()
	native := ""
	if s.record.NativeSessionID != nil {
		native = *s.record.NativeSessionID
	}
	snapshot := harness.SessionSnapshot{Directory: s.directory(), Cwd: s.record.CWD, Model: s.record.Model, ReasoningEffort: s.record.ReasoningEffort, Instructions: s.record.Instructions, NativeSessionID: native, TotalContextBudgetTokens: s.record.TotalContextBudgetTokens, ActiveTurnID: s.activeTurn, Environment: s.environment()}
	s.mu.Unlock()
	snapshot.Resuming = s.runtimeState().Resuming
	return snapshot
}

// environment is this Session's own harness environment: its notification destination, which the
// `agentplane` command reads as AGENTPLANE_DESTINATION.
func (s *Session) environment() map[string]string {
	return map[string]string{"AGENTPLANE_DESTINATION": s.runner.config.Launch.Destination(s.id)}
}

func (s *Session) hostedAdapter() harness.Adapter {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.adapter
}
func (s *Session) Journal() harness.AdapterJournal { return s.journal }
func (s *Session) RecordNativeRequest(ctx context.Context, requestID string, kind harness.NativeRequestKind, commandIDs []string) error {
	return s.journal.RecordNativeRequest(ctx, requestID, kind, commandIDs)
}

func (s *Session) apply(entry *eventlogpb.EventEntry, ids []string) {
	e := entry.Event
	s.mu.Lock()
	defer s.mu.Unlock()
	failed := e.GetHarnessLaunchFailed() != nil || e.GetHarnessExited() != nil || e.GetSetupInterrupted() != nil || (e.GetSetupFinished() != nil && e.GetSetupFinished().ExitCode != 0)
	if s.launch == runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING && (e.GetHarnessStarted() != nil || failed) {
		s.launch = runnerpb.LaunchOutcome_LAUNCH_OUTCOME_FAILED
		if e.GetHarnessStarted() != nil {
			s.launch = runnerpb.LaunchOutcome_LAUNCH_OUTCOME_STARTED
		}
	}
	if s.setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
		if f := e.GetSetupFinished(); f != nil {
			s.setup = runnerpb.SetupState_SETUP_STATE_SUCCEEDED
			if f.ExitCode != 0 {
				s.setup = runnerpb.SetupState_SETUP_STATE_FAILED
			}
		}
		if e.GetSetupInterrupted() != nil {
			s.setup = runnerpb.SetupState_SETUP_STATE_INTERRUPTED
		}
	}
	if e.GetHarnessLaunchFailed() != nil || e.GetSetupInterrupted() != nil || (e.GetSetupFinished() != nil && e.GetSetupFinished().ExitCode != 0) {
		cursor := entry.Cursor
		s.failure = &cursor
	}
	if e.GetHarnessStarted() != nil {
		s.failure = nil
	}
	if e.GetHarnessExited() != nil {
		if !e.GetHarnessExited().StoppedByRunner {
			cursor := entry.Cursor
			s.failure = &cursor
		}
	}
	if turn := e.GetTurnStarted(); turn != nil {
		s.activeTurn = turn.TurnId
	}
	if e.GetTurnCompleted() != nil {
		s.activeTurn = ""
	}
	for _, id := range ids {
		delete(s.pending, id)
		delete(s.dispatched, id)
	}
}

func (s *Session) recordEvent(ctx context.Context, e *eventpb.Event, sources []uint64, ids []string) (*eventlogpb.EventEntry, error) {
	// Turn transitions share the final native-write gate with exact-turn controls.
	// Once a successor is recorded, an interrupt cannot reach its stdin under the
	// predecessor's identity.
	if e.GetTurnStarted() != nil || e.GetTurnCompleted() != nil {
		s.writeMu.Lock()
		defer s.writeMu.Unlock()
	}
	entry, err := s.journal.Append(ctx, e, AppendOptions{Sources: sources, TerminalCommandIDs: ids})
	if err != nil {
		return nil, err
	}
	s.apply(entry, ids)
	s.runner.changed()
	return entry, nil
}

func (s *Session) Emit(ctx context.Context, e *eventpb.Event, sources []uint64) error {
	if turn := e.GetTurnCompleted(); turn != nil {
		return s.completeTurn(ctx, turn, sources)
	}
	_, err := s.recordEvent(ctx, e, sources, nil)
	return err
}

func (s *Session) completeTurn(ctx context.Context, turn *eventpb.TurnCompleted, sources []uint64) error {
	if err := s.journal.CommitBatch(ctx); err != nil {
		return err
	}
	var reconciliation *pendingReconciliation
	s.mu.Lock()
	interrupt := s.interrupts[turn.TurnId]
	delete(s.interrupts, turn.TurnId)
	adapter := s.adapter
	teardown := s.nativeStop == stopQuiesce
	if turn.Status == eventpb.TurnStatus_TURN_STATUS_INTERRUPTED && adapter != nil {
		reconciliation = &pendingReconciliation{turnID: turn.TurnId, adapter: adapter, sources: append([]uint64(nil), sources...)}
		s.reconciliations = append(s.reconciliations, reconciliation)
	}
	s.mu.Unlock()
	ids := []string{}
	if turn.Status == eventpb.TurnStatus_TURN_STATUS_INTERRUPTED {
		turn.InterruptedByCommandId = interrupt
		turn.EndedByTeardown = teardown && interrupt == ""
		if interrupt != "" {
			ids = append(ids, interrupt)
		}
	}
	if _, err := s.recordEvent(ctx, observation(turn), sources, ids); err != nil {
		return err
	}
	if reconciliation != nil {
		s.mu.Lock()
		reconciliation.ready = true
		s.mu.Unlock()
	}
	if turn.Status == eventpb.TurnStatus_TURN_STATUS_PROCESS_LOST {
		items, err := s.journal.ObservedItems(ctx, turn.TurnId)
		if err != nil {
			return err
		}
		if err := s.Emit(ctx, observation(harness.UnknownReport(turn.TurnId, items, "the harness exited before continuation could be inspected")), nil); err != nil {
			return err
		}
	}
	if interrupt != "" && turn.Status != eventpb.TurnStatus_TURN_STATUS_INTERRUPTED {
		if err := s.NoopCommand(ctx, interrupt, "the turn completed before interruption took effect", sources); err != nil {
			return err
		}
	}
	s.wake()
	return nil
}

func (s *Session) FailCommand(ctx context.Context, id, reason string, sources []uint64) error {
	_, err := s.recordEvent(ctx, observation(&eventpb.CommandFailed{CommandId: id, Reason: reason}), sources, []string{id})
	return err
}

func (s *Session) NoopCommand(ctx context.Context, id, reason string, sources []uint64) error {
	_, err := s.recordEvent(ctx, observation(&eventpb.CommandNoop{CommandId: id, Reason: reason}), sources, []string{id})
	return err
}

func (s *Session) ModelChanged(ctx context.Context, id, model string, sources []uint64) error {
	command, err := s.journal.GetCommand(ctx, id)
	if err != nil {
		return err
	}
	if command != nil && command.TerminalCursor != nil {
		return nil
	}
	s.mu.Lock()
	previous := s.record.Model
	s.record.Model = model
	record := *s.record
	s.mu.Unlock()
	if err := s.runner.store.Write(s.id, record); err != nil {
		return err
	}
	_, err = s.recordEvent(ctx, observation(&eventpb.ModelChanged{CommandId: id, PreviousModel: previous, Model: model}), sources, []string{id})
	return err
}

func (s *Session) ReasoningEffortChanged(ctx context.Context, id, effort string, sources []uint64) error {
	command, err := s.journal.GetCommand(ctx, id)
	if err != nil {
		return err
	}
	if command != nil && command.TerminalCursor != nil {
		return nil
	}
	s.mu.Lock()
	previous := s.record.ReasoningEffort
	s.record.ReasoningEffort = effort
	record := *s.record
	s.mu.Unlock()
	if err := s.runner.store.Write(s.id, record); err != nil {
		return err
	}
	_, err = s.recordEvent(ctx, observation(&eventpb.ReasoningEffortChanged{CommandId: id, PreviousEffort: previous, Effort: effort}), sources, []string{id})
	return err
}

func (s *Session) ConfirmUserMessage(ctx context.Context, c harness.UserMessageConfirmation, sources []uint64) error {
	if len(c.OriginCommandIDs) == 0 {
		return errors.New("native confirmation has no originating command")
	}
	pending := false
	for _, id := range c.OriginCommandIDs {
		record, err := s.journal.GetCommand(ctx, id)
		if err != nil {
			return err
		}
		if record == nil {
			return fmt.Errorf("%w %q", ErrUnknownCommand, id)
		}
		pending = pending || record.TerminalCursor == nil
	}
	if !pending {
		return nil
	}
	if err := s.checkpoint(ctx, "before-user-message-confirmed", c.OriginCommandIDs[0]); err != nil {
		return err
	}
	_, err := s.recordEvent(ctx, observation(&eventpb.HarnessUserMessageConfirmed{HarnessMessageId: c.HarnessMessageID, Text: c.Text, OriginCommandIds: c.OriginCommandIDs, TurnId: c.TurnID}), sources, c.OriginCommandIDs)
	if err != nil {
		return err
	}
	return s.checkpoint(ctx, "after-terminal-outcome", c.OriginCommandIDs[0])
}

func (s *Session) checkpoint(ctx context.Context, name, id string) error {
	configured := s.runner.config.DebugCheckpoint
	if configured == nil || configured.Name != name || configured.CommandID != id {
		return nil
	}
	reached, err := s.journal.ReachedCheckpoint(ctx, name, id)
	if err != nil || reached {
		return err
	}
	if err := s.Emit(ctx, observation(&eventpb.DebugCheckpoint{Name: name, CommandId: id}), nil); err != nil {
		return err
	}
	if err := s.journal.CommitBatch(ctx); err != nil {
		return err
	}
	<-ctx.Done()
	return ctx.Err()
}

func (s *Session) checkpointNativeResponse(ctx context.Context, line []byte) error {
	configured := s.runner.config.DebugCheckpoint
	if configured == nil || configured.Name != "before-native-response-recorded" {
		return nil
	}
	var response struct {
		ID     string `json:"id"`
		Method string `json:"method"`
	}
	if json.Unmarshal(line, &response) != nil || response.ID == "" || response.Method != "" {
		return nil
	}
	requests, err := s.journal.PendingNativeRequests(ctx)
	if err != nil {
		return err
	}
	for _, request := range requests {
		if request.Kind != harness.CodexTurnStart || request.ID != response.ID {
			continue
		}
		for _, record := range request.Commands {
			if record.Command.GetCommandId() == configured.CommandID && record.Command.GetSubmitInput() != nil {
				return s.checkpoint(ctx, configured.Name, configured.CommandID)
			}
		}
	}
	return nil
}

func (s *Session) admit(ctx context.Context, command *commandpb.Command, lameduck bool) (*eventlogpb.EventEntry, error) {
	s.admitMu.Lock()
	defer s.admitMu.Unlock()
	old, err := s.journal.GetCommand(ctx, command.CommandId)
	if err != nil {
		return nil, err
	}
	closed := s.executionClosed()
	if old == nil && (lameduck || closed) && command.GetInterruptTurn() == nil {
		return nil, status.Error(codes.Aborted, "this Sandbox is quiesced")
	}
	expected := ""
	if v := command.GetInterruptTurn(); v != nil {
		expected = v.TurnId
	}
	entry, _, err := s.journal.Admit(ctx, command, expected)
	if err != nil {
		return nil, err
	}
	if old != nil {
		if closed {
			return entry, nil
		}
	}
	if old != nil && old.TerminalCursor != nil {
		return entry, nil
	}
	s.mu.Lock()
	s.pending[command.CommandId] = true
	ready := s.readyLocked()
	s.mu.Unlock()
	if command.GetInterruptTurn() != nil {
		if ready {
			s.requestExecution()
		} else {
			if err := s.NoopCommand(ctx, command.CommandId, "the harness is not ready to receive this interruption", nil); err != nil {
				return nil, err
			}
		}
	} else if !lameduck && !closed {
		s.requestExecution()
	}
	s.runner.changed()
	return entry, nil
}

func (s *Session) ensureReady(ctx context.Context) error {
	s.mu.Lock()
	setupDone := s.setupDone
	setup := s.setup
	s.mu.Unlock()
	if setupDone != nil {
		select {
		case <-setupDone:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	s.mu.Lock()
	setup = s.setup
	s.mu.Unlock()
	if s.executionClosed() {
		return errors.New("the session is sealed")
	}
	if setup == runnerpb.SetupState_SETUP_STATE_FAILED || setup == runnerpb.SetupState_SETUP_STATE_INTERRUPTED || setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
		return errors.New("session setup failed or was interrupted; create a new session")
	}
	if err := s.runner.bootstrap(ctx); err != nil {
		return err
	}
	return s.startHarness(ctx)
}

func (s *Session) startHarness(ctx context.Context) error {
	s.mu.Lock()
	s.processState = runnerpb.ProcessState_PROCESS_STATE_STARTING
	s.nativeStop = stopNone
	harnessKind := s.record.Harness
	s.mu.Unlock()
	switch {
	case harnessKind == runnerpb.Harness_HARNESS_CLAUDE && s.runner.config.Claude != nil:
		return s.startProcess(ctx, claude.New(s, *s.runner.config.Claude))
	case harnessKind == runnerpb.Harness_HARNESS_CODEX && s.runner.codex != nil:
		return s.startOnCodexServer(ctx)
	default:
		return errors.New("this runner is not configured for the selected harness")
	}
}

func recoverAdapter(ctx context.Context, adapter harness.Adapter) error {
	if recovery, ok := adapter.(harness.RecoveryAdapter); ok {
		if err := recovery.Recover(ctx); err != nil {
			return fmt.Errorf("recover pending native requests: %w", err)
		}
	}
	return nil
}

// startProcess runs this Session's own native harness process.
func (s *Session) startProcess(ctx context.Context, adapter harness.ProcessAdapter) error {
	if err := recoverAdapter(ctx, adapter); err != nil {
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
	env := maps.Clone(s.runner.config.HarnessEnvironment)
	if env == nil {
		env = map[string]string{}
	}
	maps.Copy(env, s.environment())
	maps.Copy(env, harnessEnv)
	cwd, err := s.runner.ensureWorkspace(ctx, s.Session().Cwd)
	if err != nil {
		return err
	}
	process, err := NewProcess(ProcessConfig{Command: command, Dir: cwd, Env: env, StateOwnerFD: s.runner.owner.File(), ProcessIsolation: s.runner.config.ProcessIsolation})
	if err != nil {
		return err
	}
	if err := process.Start(ctx); err != nil {
		return err
	}
	s.mu.Lock()
	s.process = process
	s.adapter = adapter
	s.processID = uuid.NewString()
	s.nativeStop = stopNone
	s.mu.Unlock()
	s.readers.Add(2)
	go s.readStdout(process, adapter)
	go s.readStderr(process)
	if err := s.prepare(ctx, adapter, process.PID()); err != nil {
		s.mu.Lock()
		s.nativeStop = stopStartupFailure
		s.processState = runnerpb.ProcessState_PROCESS_STATE_STOPPING
		s.mu.Unlock()
		stopErr := process.Stop(context.WithoutCancel(ctx), 5*time.Second)
		s.readers.Wait()
		return errors.Join(fmt.Errorf("harness startup failed: %w; %s", err, process.ExitDescription()), stopErr)
	}
	return nil
}

// startOnCodexServer hosts this Session's thread on the Runner's shared Codex app-server.
func (s *Session) startOnCodexServer(ctx context.Context) error {
	if _, err := s.runner.ensureWorkspace(ctx, s.Session().Cwd); err != nil {
		return err
	}
	server := s.runner.codex
	adapter := codex.New(s)
	if err := recoverAdapter(ctx, adapter); err != nil {
		return err
	}
	// Frames for every hosted Session, and the exit of a start it makes, can reach this one as soon as
	// it is hosted.
	s.mu.Lock()
	s.adapter = adapter
	s.nativeStop = stopNone
	s.mu.Unlock()
	p, err := server.host(ctx, s)
	if err != nil {
		return err
	}
	s.mu.Lock()
	s.process = p.process
	s.codex = p
	s.processID = p.id
	s.mu.Unlock()
	server.handshakeMu.Lock()
	err = s.prepare(ctx, adapter, p.process.PID())
	server.handshakeMu.Unlock()
	if err != nil {
		p.router.Release(s)
		s.mu.Lock()
		if s.codex == p {
			s.process, s.codex, s.processID = nil, nil, ""
		}
		s.mu.Unlock()
		return fmt.Errorf("harness startup failed: %w", err)
	}
	return nil
}

// prepare starts or resumes the native conversation on a started harness and reconciles the turns a
// lost harness left.
func (s *Session) prepare(ctx context.Context, adapter harness.Adapter, pid int) error {
	native, err := adapter.Handshake(ctx)
	if err != nil {
		return err
	}
	s.mu.Lock()
	s.record.NativeSessionID = &native
	record := *s.record
	s.mu.Unlock()
	resuming := s.runtimeState().Resuming
	if err := s.runner.store.Write(s.id, record); err != nil {
		return err
	}
	if err := s.Emit(ctx, observation(&eventpb.HarnessStarted{Resumed: resuming, Pid: int32(pid)}), nil); err != nil {
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
		if err := s.Emit(ctx, observation(report), nil); err != nil {
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
	if phase != executionReady && phase != executionStopping {
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
		if s.runtimeState().Phase == executionHalted || operationDone(s.halting) || s.dispatched[command.CommandId] || s.controlDispatches[command.CommandId] != nil {
			s.mu.Unlock()
			continue
		}
		op := &sessionOperation{id: command.CommandId, done: make(chan struct{})}
		s.controlDispatches[command.CommandId] = op
		s.work.Add(1)
		s.mu.Unlock()
		go func(command *commandpb.Command, op *sessionOperation) {
			defer s.work.Done()
			// The reconciliation context may end as soon as this controller pass
			// returns. Native control RPCs belong to the Session host lifetime.
			err := s.dispatch(s.runner.ctx, command)
			s.mu.Lock()
			op.err = err
			delete(s.controlDispatches, command.CommandId)
			close(op.done)
			s.mu.Unlock()
			if err != nil {
				s.runner.fail(err)
			}
			s.wake()
			s.runner.changed()
		}(command, op)
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
		budget, e := s.runner.config.ResolveModelConfig(ctx, s.record.Harness, model.Model)
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
	if err := s.Emit(ctx, observation(&eventpb.Native{Direction: eventpb.Direction_DIRECTION_TO_HARNESS, Line: string(frame)}), nil); err != nil {
		return err
	}
	if err := s.journal.CommitBatch(ctx); err != nil {
		return err
	}
	s.deliverReplies()
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	s.mu.Lock()
	process := s.process
	shared := s.codex
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
		if err := shared.router.Sent(s, frame); err != nil {
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
	waiter := &nativeWaiter{matches: match, result: make(chan nativeResult, 1)}
	s.mu.Lock()
	s.waiters = append(s.waiters, waiter)
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		defer s.mu.Unlock()
		for i, w := range s.waiters {
			if w == waiter {
				s.waiters = append(s.waiters[:i], s.waiters[i+1:]...)
				break
			}
		}
	}()
	if err := s.SendNative(ctx, frame); err != nil {
		return harness.NativeReceipt{}, err
	}
	select {
	case reply := <-waiter.result:
		return reply.receipt, reply.err
	case <-ctx.Done():
		return harness.NativeReceipt{}, &harness.NativeResponsePendingError{Cause: ctx.Err()}
	}
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
		var storageErr *JournalStorageError
		if errors.Is(err, context.Canceled) || errors.As(err, &storageErr) {
			return err
		}
		zap.L().Error("native frame translation failed", zap.String("session", s.id), zap.Uint64("sequence", entry.Origin.Sequence), zap.Error(err))
	}
	s.mu.Lock()
	waiting := s.waiters[:0]
	for _, w := range s.waiters {
		if w.matches(frame) {
			s.replies = append(s.replies, matchedReply{w, harness.NativeReceipt{Frame: frame, Sequence: entry.Origin.Sequence}})
		} else {
			waiting = append(waiting, w)
		}
	}
	s.waiters = waiting
	s.mu.Unlock()
	return nil
}

func (s *Session) deliverReplies() {
	s.mu.Lock()
	replies := s.replies
	s.replies = nil
	s.mu.Unlock()
	for _, reply := range replies {
		reply.waiter.result <- nativeResult{receipt: reply.receipt}
	}
}

func (s *Session) readStdout(process *Process, adapter harness.Adapter) {
	defer s.readers.Done()
	buffer := make([]byte, 64*1024)
	var pending []byte
	var readErr error
	for {
		n, err := process.Stdout().Read(buffer)
		if n > 0 {
			pending = append(pending, buffer[:n]...)
			if e := s.journal.WithBatch(s.runner.ctx, func(ctx context.Context) error {
				for {
					i := bytes.IndexByte(pending, '\n')
					if i < 0 {
						break
					}
					if i > harness.MaxFrameLineBytes {
						return errors.New("native frame exceeded the 64 MiB limit")
					}
					line := append([]byte(nil), bytes.TrimRight(pending[:i], "\r")...)
					pending = pending[i+1:]
					if e := s.receive(ctx, line, adapter); e != nil {
						return e
					}
				}
				return nil
			}); e != nil {
				readErr = e
				break
			}
			s.wake()
			s.deliverReplies()
			if len(pending) > harness.MaxFrameLineBytes {
				readErr = errors.New("native frame exceeded the 64 MiB limit")
				break
			}
		}
		if err != nil {
			if err == io.EOF && len(pending) > 0 {
				if e := s.journal.WithBatch(s.runner.ctx, func(ctx context.Context) error { return s.receive(ctx, bytes.TrimRight(pending, "\r"), adapter) }); e != nil {
					readErr = e
				} else {
					s.wake()
					s.deliverReplies()
				}
			}
			if err != io.EOF {
				readErr = err
			}
			break
		}
	}
	if readErr != nil {
		s.runner.fail(readErr)
		if err := process.Stop(context.Background(), 5*time.Second); err != nil {
			s.runner.fail(err)
		}
	}
	code, err := process.Wait(context.Background())
	if err != nil {
		s.runner.fail(err)
	}
	if err := s.harnessEnded(s.runner.ctx, code); err != nil {
		s.runner.fail(err)
	}
}

func (s *Session) readStderr(process *Process) {
	defer s.readers.Done()
	buffer := make([]byte, 4096)
	for {
		n, err := process.Stderr().Read(buffer)
		if n > 0 {
			if e := s.Emit(s.runner.ctx, observation(&eventpb.HarnessStderr{Text: string(bytes.ToValidUTF8(buffer[:n], []byte("�")))}), nil); e != nil {
				s.runner.fail(e)
				return
			}
		}
		if err != nil {
			if err != io.EOF {
				s.runner.fail(err)
			}
			return
		}
	}
}

func (s *Session) harnessEnded(ctx context.Context, code int) error {
	s.mu.Lock()
	waiting := s.waiters
	s.waiters = nil
	for _, reply := range s.replies {
		waiting = append(waiting, reply.waiter)
	}
	s.replies = nil
	stoppedByRunner := s.nativeStop != stopNone
	s.codex = nil
	s.processID = ""
	s.processState = runnerpb.ProcessState_PROCESS_STATE_UNSPECIFIED
	s.mu.Unlock()
	for _, w := range waiting {
		w.result <- nativeResult{err: fmt.Errorf("the harness exited with exit_code=%d", code)}
	}
	if err := s.Emit(ctx, observation(&eventpb.HarnessExited{ExitCode: int32(code), StoppedByRunner: stoppedByRunner}), nil); err != nil {
		return err
	}
	s.wake()
	if turn := s.Session().ActiveTurnID; turn != "" {
		return s.completeTurn(ctx, &eventpb.TurnCompleted{TurnId: turn, Status: eventpb.TurnStatus_TURN_STATUS_PROCESS_LOST, Error: fmt.Sprintf("the harness exited with exit_code=%d during the turn", code)}, nil)
	}
	return nil
}

func (s *Session) load(ctx context.Context) error {
	for cursor := uint64(0); ; {
		s.mu.Lock()
		needsPrefix := s.setup == runnerpb.SetupState_SETUP_STATE_RUNNING || s.launch == runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING
		s.mu.Unlock()
		if !needsPrefix {
			break
		}
		entries, err := s.journal.Since(ctx, cursor, 128)
		if err != nil {
			return err
		}
		if len(entries) == 0 {
			break
		}
		for _, entry := range entries {
			s.apply(entry, nil)
			cursor = entry.Cursor
			s.mu.Lock()
			resolved := s.setup != runnerpb.SetupState_SETUP_STATE_RUNNING && s.launch != runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING
			s.mu.Unlock()
			if resolved {
				break
			}
		}
	}
	// Prefix scanning recovers one-shot launch/setup outcomes only. Current turn state comes
	// from the durable checkpoint and must not be replaced by an early historical prefix.
	s.mu.Lock()
	s.activeTurn = s.journal.RecoveryState().ActiveTurnID
	s.mu.Unlock()
	commands, err := s.journal.PendingCommands(ctx)
	if err != nil {
		return err
	}
	for _, c := range commands {
		s.pending[c.CommandId] = true
	}
	return nil
}

func (s *Session) recover(ctx context.Context) error {
	if s.journal.SealState() != nil {
		return nil
	}
	s.mu.Lock()
	setup := s.setup
	s.mu.Unlock()
	if setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
		if err := s.Emit(ctx, observation(&eventpb.SetupInterrupted{}), nil); err != nil {
			return err
		}
	}
	if s.journal.RecoveryState().HarnessRunning {
		if err := s.Emit(ctx, observation(&eventpb.HarnessLost{}), nil); err != nil {
			return err
		}
	}
	if turn := s.Session().ActiveTurnID; turn != "" {
		if err := s.completeTurn(ctx, &eventpb.TurnCompleted{TurnId: turn, Status: eventpb.TurnStatus_TURN_STATUS_PROCESS_LOST, Error: "the runner restarted while the turn was active"}, nil); err != nil {
			return err
		}
	}
	commands, err := s.journal.PendingCommands(ctx)
	if err != nil {
		return err
	}
	for _, c := range commands {
		if c.GetInterruptTurn() != nil {
			stored, err := s.journal.GetCommand(ctx, c.CommandId)
			if err != nil {
				return err
			}
			if stored == nil || stored.DispatchPlanned {
				continue
			}
			if err := s.NoopCommand(ctx, c.CommandId, "the target turn is no longer active after the runner restarted", nil); err != nil {
				return err
			}
		}
	}
	return nil
}

func (s *Session) settleUndispatched(ctx context.Context, reason string) error {
	commands, err := s.journal.PendingCommands(ctx)
	if err != nil {
		return err
	}
	for _, command := range commands {
		stored, err := s.journal.GetCommand(ctx, command.CommandId)
		if err != nil {
			return err
		}
		if stored != nil && !stored.DispatchPlanned {
			if err := s.FailCommand(ctx, command.CommandId, reason, nil); err != nil {
				return err
			}
		}
	}
	return nil
}

// halt ends this Session's work on its harness: it cancels preparation, waits for setup and dispatch,
// then interrupts the active turn and waits for it to end. The harness stops afterwards, in
// stopProcess or with the shared Codex app-server.
func (s *Session) halt(ctx context.Context, reason stopReason) {
	s.admitMu.Lock()
	s.mu.Lock()
	preparation := s.preparation
	dispatching := s.dispatching
	setupDone := s.setupDone
	s.mu.Unlock()
	s.admitMu.Unlock()
	if preparation != nil {
		preparation.cancel()
		<-preparation.done
	}
	if dispatching != nil {
		<-dispatching.done
	}
	if setupDone != nil {
		<-setupDone
	}
	s.mu.Lock()
	controls := make([]*sessionOperation, 0, len(s.controlDispatches))
	for _, control := range s.controlDispatches {
		controls = append(controls, control)
	}
	s.mu.Unlock()
	for _, control := range controls {
		<-control.done
	}
	s.admitMu.Lock()
	s.mu.Lock()
	s.nativeStop = reason
	s.processState = runnerpb.ProcessState_PROCESS_STATE_STOPPING
	process := s.process
	adapter := s.adapter
	turn := s.activeTurn
	s.mu.Unlock()
	s.admitMu.Unlock()
	if process != nil && process.Running() && adapter != nil && turn != "" {
		interruptCtx := context.WithValue(ctx, interruptTargetKey{}, turn)
		if err := adapter.Interrupt(interruptCtx, turn); err != nil && !errors.Is(err, errStaleInterrupt) {
			zap.L().Warn("teardown interrupt failed", zap.String("session", s.id), zap.Error(err))
		}
		waitCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
		for s.Session().ActiveTurnID == turn && process.Running() {
			if err := s.journal.WaitBeyond(waitCtx, s.journal.RecoveryState().ThroughCursor); err != nil {
				break
			}
		}
		cancel()
	}
}

// stopProcess stops the harness process this Session owns, if any, and waits for its exit to be recorded.
func (s *Session) stopProcess(ctx context.Context) error {
	s.mu.Lock()
	process := s.process
	shared := s.codex != nil
	s.mu.Unlock()
	if !shared && process != nil && process.Running() {
		if err := process.Stop(ctx, 5*time.Second); err != nil {
			return err
		}
	}
	s.readers.Wait()
	return nil
}

// sealJournal fails the commands never dispatched and seals the journal, once the harness has stopped.
func (s *Session) sealJournal(ctx context.Context) error {
	// Serialize sealing with command admission. An interrupt admitted while a
	// lameduck halt is in progress must either be dispatched before halt completes
	// or be durably settled before the journal is sealed.
	s.admitMu.Lock()
	defer s.admitMu.Unlock()
	if err := s.settleUndispatched(ctx, "the Sandbox was quiesced before this command was dispatched"); err != nil {
		return err
	}
	uid, err := s.runner.store.RequireIncarnationUID()
	if err != nil {
		return err
	}
	_, err = s.journal.Seal(ctx, uid)
	return err
}

func (s *Session) attached() *runnerpb.Attached {
	state := s.journal.RecoveryState()
	s.mu.Lock()
	defer s.mu.Unlock()
	harness := runnerpb.HarnessState_HARNESS_STATE_STOPPED
	if s.process != nil && s.process.Running() {
		harness = runnerpb.HarnessState_HARNESS_STATE_RUNNING
	}
	uid, known := s.runner.store.IncarnationUID()
	var incarnation *string
	if known {
		incarnation = &uid
	}
	return &runnerpb.Attached{SessionId: s.id, Spec: s.record.Spec(), LastCursor: state.ThroughCursor, HarnessState: harness, ActiveTurnId: state.ActiveTurnID, SetupState: s.setup, LaunchOutcome: s.launch, IncarnationUid: incarnation}
}

func (s *Session) autosuspendBlockers() []*sandboxpb.AutosuspendBlocker {
	preparing := s.runtimeState().Phase == executionPreparing
	s.mu.Lock()
	var blockers []*sandboxpb.AutosuspendBlocker
	add := func(reason string) {
		sessionID := s.id
		blockers = append(blockers, &sandboxpb.AutosuspendBlocker{Reason: reason, SessionId: &sessionID})
	}
	if s.activeTurn != "" {
		add(autosuspendActiveTurn)
	}
	if len(s.pending) > 0 {
		add(autosuspendPendingCommand)
	}
	if preparing {
		add(autosuspendActivation)
	}
	if s.setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
		add(autosuspendSetup)
	}
	s.mu.Unlock()
	return blockers
}

func (s *Session) startSetup(script string) {
	s.mu.Lock()
	s.setupDone = make(chan struct{})
	done := s.setupDone
	s.mu.Unlock()
	go func() {
		defer func() {
			close(done)
			s.wake()
			s.runner.changed()
		}()
		err := s.runner.bootstrap(s.runner.ctx)
		if err == nil {
			err = s.Emit(s.runner.ctx, observation(&eventpb.SetupStarted{}), nil)
		}
		if err == nil {
			cwd, e := s.runner.ensureWorkspace(s.runner.ctx, s.Session().Cwd)
			err = e
			if err == nil {
				var code int
				code, err = s.runner.runScript(s.runner.ctx, script, cwd, func(stdout bool, data []byte) error {
					out := &eventpb.SetupOutput{}
					if stdout {
						out.Stream = &eventpb.SetupOutput_Stdout{Stdout: data}
					} else {
						out.Stream = &eventpb.SetupOutput_Stderr{Stderr: data}
					}
					return s.Emit(s.runner.ctx, observation(out), nil)
				})
				if err == nil {
					err = s.Emit(s.runner.ctx, observation(&eventpb.SetupFinished{ExitCode: int32(code)}), nil)
				}
			}
		}
		if err != nil {
			zap.L().Error("session setup interrupted", zap.String("session", s.id), zap.Error(err))
			if e := s.Emit(s.runner.ctx, observation(&eventpb.SetupInterrupted{}), nil); e != nil {
				s.runner.fail(e)
			}
		}
	}()
}

func (s *Session) directory() string {
	directory, err := s.runner.store.Directory(s.id)
	if err != nil {
		panic(err)
	}
	return directory
}
