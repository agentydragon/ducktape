package runner

import (
	"context"
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
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/agentydragon/ducktape/agentplane/runner/scripts"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/cosi-project/runtime/pkg/state"
	"go.uber.org/zap"
)

const (
	autosuspendActiveTurn     = "ActiveTurn"
	autosuspendPendingCommand = "PendingCommand"
	autosuspendSetup          = "Setup"
	autosuspendActivation     = "Activation"
)

type Session struct {
	deps                                                   sessionDependencies
	id                                                     string
	record                                                 *retained.Record
	journal                                                *journal.Journal
	mu                                                     sync.Mutex
	loading                                                bool
	admitMu                                                sync.Mutex
	writeMu                                                sync.Mutex
	process                                                *supervision.ManagedProcess
	adapter                                                harness.Adapter
	processID                                              string
	processState                                           runnerpb.ProcessState
	nativeStop                                             stopReason
	preparation, dispatching, inspection, halting, sealing *operation
	controlDispatches                                      map[string]*operation
	reconciliations                                        []*pendingReconciliation
	activeTurn                                             string
	setup                                                  runnerpb.SetupState
	launch                                                 runnerpb.LaunchOutcome
	failure                                                *uint64
	setupScript                                            *string
	setupJob, stopping                                     *operation
	readers                                                sync.WaitGroup
	work                                                   sync.WaitGroup
	pending                                                map[string]bool
	dispatched                                             map[string]bool
	interrupts                                             map[string]string
	waiters                                                []*nativeWaiter
	replies                                                []matchedReply

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
	publishOperation   func(string, *operation, bool)
	changed            func()
	fail               func(error)
}

func newSession(deps sessionDependencies, id string, record *retained.Record, journal *journal.Journal) *Session {
	s := &Session{deps: deps, id: id, record: record, journal: journal, pending: map[string]bool{}, dispatched: map[string]bool{}, controlDispatches: map[string]*operation{}, interrupts: map[string]string{}, launch: runnerpb.LaunchOutcome_LAUNCH_OUTCOME_PENDING, setup: runnerpb.SetupState_SETUP_STATE_NOT_REQUIRED}
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
		s.readers.Wait()
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
		preparation.cancel()
		<-preparation.done
	}
	if dispatching != nil {
		<-dispatching.done
	}
	if setupJob != nil {
		<-setupJob.done
	}
	s.mu.Lock()
	controls := make([]*operation, 0, len(s.controlDispatches))
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
	shared := s.codexAppServer != nil
	s.mu.Unlock()
	if !shared && process != nil {
		if err := process.Stop(ctx); err != nil {
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
