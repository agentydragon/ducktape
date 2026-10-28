package session

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"path/filepath"
	"sort"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/agentydragon/ducktape/agentplane/runner/workspace"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/state"
)

// Manager owns the in-memory registry and reconstruction of durable
// sessions. Runner's admission mutex remains the sole quiesce barrier.
type Manager struct {
	ctx           context.Context
	state         state.State
	store         *retained.Store
	workspaceRoot string
	dependencies  sessionDependencies
	destination   func(string) string
	publishSeal   func(string, *journal.SealState) error
	publishAck    func(string, retained.Acknowledgement) error
	changed       func()
	fail          func(error)
	workers       int
	publishMu     sync.Mutex

	mu       sync.Mutex
	loadMu   sync.Mutex
	sessions map[string]*Session
	// loadMu guards freshReservations; those records have not completed their first publication.
	freshReservations map[string]struct{}
}

func NewManager(config Config) (*Manager, error) {
	if config.Context == nil || config.State == nil || config.Store == nil || config.Destination == nil || config.Scripts == nil || config.BootstrapHost == nil || config.EnsureWorkspace == nil || config.LaunchProcess == nil || config.ResolveModelConfig == nil || config.PublishSeal == nil || config.PublishAck == nil || config.OnChange == nil || config.OnError == nil || config.ReconcileConcurrency <= 0 {
		return nil, fmt.Errorf("invalid Session manager dependencies")
	}
	if config.Claude != nil {
		copied := *config.Claude
		config.Claude = &copied
	}
	if config.DebugCheckpoint != nil {
		copied := *config.DebugCheckpoint
		config.DebugCheckpoint = &copied
	}
	m := &Manager{
		ctx: config.Context, state: config.State, store: config.Store,
		workspaceRoot: config.WorkspaceRoot, destination: config.Destination,
		publishSeal: config.PublishSeal, publishAck: config.PublishAck,
		changed: config.OnChange, fail: config.OnError,
		workers: config.ReconcileConcurrency, sessions: map[string]*Session{}, freshReservations: map[string]struct{}{},
	}
	m.dependencies = sessionDependencies{
		ctx: config.Context, state: config.State, store: config.Store,
		claude: config.Claude, harnessEnvironment: maps.Clone(config.HarnessEnvironment),
		debugCheckpoint: config.DebugCheckpoint, scripts: config.Scripts,
		codexAppServer: config.CodexAppServer, bootstrapHost: config.BootstrapHost,
		ensureWorkspace: config.EnsureWorkspace, launchProcess: config.LaunchProcess,
		resolveModelConfig: config.ResolveModelConfig,
		publish:            m.publishSession, publishOperation: m.publishOperation,
		changed: config.OnChange, fail: config.OnError,
	}
	return m, nil
}

func (m *Manager) Controller() controller.QController { return &sessionLifecycleController{manager: m} }

func (m *Manager) Get(id string) *Session {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.sessions[id]
}

func (m *Manager) Inventory() []*Session {
	m.mu.Lock()
	defer m.mu.Unlock()
	sessions := make([]*Session, 0, len(m.sessions))
	for _, s := range m.sessions {
		sessions = append(sessions, s)
	}
	sort.Slice(sessions, func(i, j int) bool { return sessions[i].id < sessions[j].id })
	return sessions
}

func (m *Manager) Startup(ctx context.Context) error {
	ids, err := m.store.SessionIDs()
	if err != nil {
		return err
	}
	for _, id := range ids {
		s, err := m.Load(ctx, id)
		if err != nil {
			return err
		}
		if err := m.ResumeRecovered(s); err != nil {
			return err
		}
	}
	return nil
}

// reserveFresh serializes the durable write with first ownership of its
// journal. The admission callback may hold Runner.mu for the write but must
// release it before returning, so publication never runs under that barrier.
func (m *Manager) ReserveFresh(ctx context.Context, id string, reserve func() error) (*Session, error) {
	m.loadMu.Lock()
	defer m.loadMu.Unlock()
	if err := reserve(); err != nil {
		return nil, err
	}
	m.freshReservations[id] = struct{}{}
	return m.loadLocked(ctx, id, false)
}

// Retained records are reconciled before publishing any schedulable Session input.
func (m *Manager) Load(ctx context.Context, id string) (*Session, error) {
	m.loadMu.Lock()
	defer m.loadMu.Unlock()
	_, freshReservation := m.freshReservations[id]
	return m.loadLocked(ctx, id, !freshReservation)
}

func (m *Manager) loadLocked(ctx context.Context, id string, recoverRetained bool) (*Session, error) {
	if s := m.Get(id); s != nil {
		return s, nil
	}
	record, err := m.store.Read(id)
	if err != nil {
		return nil, err
	}
	if _, err := workspace.Resolve(record.CWD, m.workspaceRoot); err != nil {
		return nil, err
	}
	directory, err := m.store.Directory(id)
	if err != nil {
		return nil, err
	}
	journal, err := journal.Open(ctx, filepath.Join(directory, "journal.sqlite"), record.EventSourceID)
	if err != nil {
		return nil, err
	}
	initialized := false
	defer func() {
		if !initialized {
			_ = journal.Close()
		}
	}()
	deps := m.dependencies
	deps.destination = m.destination(id)
	s := newSession(deps, id, &record, journal)
	s.loading = true
	if err := s.load(ctx); err != nil {
		return nil, err
	}
	if recoverRetained {
		if err := s.recover(ctx); err != nil {
			return nil, err
		}
	}
	s.mu.Lock()
	s.loading = false
	s.mu.Unlock()
	if err := m.Reopen(ctx, s); err != nil {
		return nil, err
	}
	// The Session controller resolves each published Input through Get.
	m.mu.Lock()
	m.sessions[id] = s
	m.mu.Unlock()
	s.notifyBlockers()
	m.publishSession(s, false)
	delete(m.freshReservations, id)
	m.changed()
	initialized = true
	return s, nil
}

func (m *Manager) ResumeRecovered(s *Session) error {
	if m.store.Lameduck() || s.journal.SealState() != nil {
		return nil
	}
	commands, err := s.journal.PendingCommands(m.ctx)
	if err != nil {
		return err
	}
	turns, err := s.journal.UnreconciledProcessLostTurns(m.ctx)
	if err != nil {
		return err
	}
	if len(commands) > 0 || len(turns) > 0 {
		s.requestExecution()
	}
	return nil
}

func (m *Manager) Reopen(ctx context.Context, s *Session) error {
	uid, known := m.store.IncarnationUID()
	seal := s.journal.SealState()
	mayReopen := known && !m.store.Lameduck()
	reopening := mayReopen && seal != nil && seal.IncarnationUID != uid
	if reopening {
		if err := s.journal.Reopen(ctx, uid); err != nil {
			return err
		}
	}
	ack, err := m.store.ReadAcknowledgement(s.id)
	if err != nil {
		return err
	}
	writeAck := reopening && ack == nil || mayReopen && ack != nil && ack.Sealed && s.journal.SealState() == nil
	if writeAck && ack == nil {
		ack = &retained.Acknowledgement{}
	}
	if writeAck {
		ack.Sealed = false
		if err := m.store.WriteAcknowledgement(s.id, *ack); err != nil {
			return err
		}
	}
	if err := m.publishSeal(s.id, s.journal.SealState()); err != nil {
		return err
	}
	if ack != nil {
		if err := m.publishAck(s.id, *ack); err != nil {
			return err
		}
	}
	if reopening || writeAck {
		m.changed()
	}
	return nil
}

func (m *Manager) ResetDemand() error {
	m.publishMu.Lock()
	defer m.publishMu.Unlock()
	for _, s := range m.Inventory() {
		input, _, err := componentRead[Input](m.ctx, m.state, s.id)
		if err != nil {
			return err
		}
		input.Demand = 0
		if err := publish(m.ctx, m.state, s.id, input); err != nil {
			return err
		}
	}
	return nil
}

func (m *Manager) Close() error {
	var errs []error
	for _, s := range m.Inventory() {
		s.work.Wait()
		errs = append(errs, s.journal.Close())
	}
	return errors.Join(errs...)
}
