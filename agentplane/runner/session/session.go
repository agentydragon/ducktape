package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/runner/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	"github.com/agentydragon/ducktape/agentplane/runner/operation"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/agentydragon/ducktape/agentplane/runner/scripts"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/state"
	"go.uber.org/zap"
)

const (
	autosuspendActiveTurn     = "ActiveTurn"
	autosuspendPendingCommand = "PendingCommand"
	autosuspendActivation     = "Activation"
)

type Session struct {
	deps                                                   sessionDependencies
	id                                                     string
	record                                                 *retained.Record
	journal                                                *journal.Journal
	mu                                                     sync.Mutex
	loading                                                bool
	notifiedBlockers                                       blockerNotification
	admitMu                                                sync.Mutex
	writeMu                                                sync.Mutex
	process                                                *supervision.ManagedProcess
	adapter                                                harness.Adapter
	processID                                              string
	processState                                           runnerpb.ProcessState
	nativeStop                                             stopReason
	preparation, dispatching, inspection, halting, sealing *operation.Operation
	controlDispatches                                      map[string]chan struct{}
	reconciliations                                        []*pendingReconciliation
	activeTurn                                             string
	setup                                                  runnerpb.SetupState
	launch                                                 runnerpb.LaunchOutcome
	failure                                                *uint64
	setupScript                                            *string
	setupJob, stopping                                     *operation.Operation
	connection                                             *nativeConnection
	work                                                   sync.WaitGroup
	operationStartFailed                                   bool
	pending                                                map[string]bool
	dispatched                                             map[string]bool
	interrupts                                             map[string]string

	// codexAppServer is the shared app-server incarnation hosting this Codex Session's thread.
	codexAppServer  *codexappserver.Process
	codexAttachment *codexappserver.Attachment
	codexExitEpoch  uint64
}

// Session dependencies name only the capabilities that one session needs. The Runner
// admission barrier and registry are deliberately absent.
type sessionDependencies struct {
	ctx                context.Context
	state              state.State
	store              *retained.Store
	destination        string
	claude             *harness.LaunchConfig
	harnessEnvironment map[string]string
	debugCheckpoint    *DebugCheckpointConfig
	scripts            *scripts.Runner
	codexAppServer     *codexappserver.Host
	bootstrapHost      *bootstrap.Host
	ensureWorkspace    func(context.Context, string) (string, error)
	launchProcess      func(context.Context, string, supervision.ProcessConfig) (*supervision.ManagedProcess, error)
	resolveModelConfig func(context.Context, runnerpb.Harness, string) (*int, error)
	publish            func(*Session, bool)
	publishOperation   func(string, string, bool) error
	changed            func()
	fail               func(error)
}

func newSession(deps sessionDependencies, id string, record *retained.Record, journal *journal.Journal) *Session {
	s := &Session{deps: deps, id: id, record: record, journal: journal, pending: map[string]bool{}, dispatched: map[string]bool{}, controlDispatches: map[string]chan struct{}{}, interrupts: map[string]string{}, launch: runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING, setup: runnerpb.SetupState_SETUP_STATE_NOT_REQUIRED}
	if record.SetupScriptSHA256 != nil {
		s.setup = runnerpb.SetupState_SETUP_STATE_RUNNING
	}
	s.connection = &nativeConnection{deps: nativeConnectionDependencies{
		ctx: deps.ctx, journal: journal,
		send:       func(ctx context.Context, frame json.RawMessage) error { return s.sendNative(ctx, deps.ctx, frame) },
		afterBatch: s.wake,
		stderr: func(ctx context.Context, text string) error {
			return s.Emit(ctx, observation(&eventpb.HarnessStderr{Text: text}), nil)
		},
		ended: s.harnessEnded, fail: deps.fail,
	}}
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
	return map[string]string{"AGENTPLANE_DESTINATION": s.deps.destination}
}

func (s *Session) hostedAdapter() harness.Adapter {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.adapter
}
func (s *Session) Journal() harness.AdapterJournal { return s.journal }
func (s *Session) ensureReady(ctx context.Context) error {
	if err := s.waitSetup(ctx); err != nil {
		return err
	}
	s.mu.Lock()
	setup := s.setup
	s.mu.Unlock()
	if s.executionClosed() {
		return errors.New("the session is sealed")
	}
	if setup == runnerpb.SetupState_SETUP_STATE_FAILED || setup == runnerpb.SetupState_SETUP_STATE_INTERRUPTED || setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
		return errors.New("session setup failed or was interrupted; create a new session")
	}
	if err := s.deps.bootstrapHost.Wait(ctx); err != nil {
		return err
	}
	s.mu.Lock()
	previous, shared := s.process, s.codexAppServer != nil
	s.mu.Unlock()
	if previous != nil && !shared && !previous.Running() {
		if _, err := previous.Wait(ctx); err != nil {
			return fmt.Errorf("previous harness cleanup failed: %w", err)
		}
		// The exited process may still be recording frames and clearing its
		// attachment. Join those callbacks before installing a successor.
		s.connection.join()
	}
	return s.startHarness(ctx)
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
	setupJob := s.setupJob
	s.mu.Unlock()
	s.admitMu.Unlock()
	if preparation != nil {
		preparation.Cancel()
		<-preparation.Done()
	}
	if dispatching != nil {
		<-dispatching.Done()
	}
	if setupJob != nil {
		<-setupJob.Done()
	}
	s.mu.Lock()
	controls := make([]chan struct{}, 0, len(s.controlDispatches))
	for _, control := range s.controlDispatches {
		controls = append(controls, control)
	}
	s.mu.Unlock()
	for _, control := range controls {
		<-control
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
	shared := s.codexAppServer != nil
	s.mu.Unlock()
	if !shared && process != nil {
		if err := process.Stop(ctx); err != nil {
			return err
		}
	}
	s.connection.join()
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
	uid, err := s.deps.store.RequireIncarnationUID()
	if err != nil {
		return err
	}
	_, err = s.journal.Seal(ctx, uid)
	return err
}

func (s *Session) directory() string {
	directory, err := s.deps.store.Directory(s.id)
	if err != nil {
		panic(err)
	}
	return directory
}

func (s *Session) ID() string                     { return s.id }
func (s *Session) EventJournal() *journal.Journal { return s.journal }
func (s *Session) SetupScriptSHA256() *string     { return s.record.SetupScriptSHA256 }
func (s *Session) SetSetupScript(script string) {
	s.mu.Lock()
	s.setupScript = &script
	s.mu.Unlock()
	s.wake()
}
