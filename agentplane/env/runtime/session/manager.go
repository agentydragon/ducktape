package session

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"path/filepath"
	"sort"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/journal"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/workspace"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
)

// Manager owns the in-memory registry and reconstruction of durable
// sessions. Runner's admission mutex remains the sole quiesce barrier.
type Manager struct {
	ctx           context.Context
	state         state.State
	store         *retained.Store
	environment   func() Environment
	workspaceRoot string
	dependencies  sessionDependencies
	changed       func()
	fail          func(error)
	workers       int
	publishMu     sync.Mutex

	mu                   sync.Mutex
	loadMu               sync.Mutex
	sessions             map[string]*Session
	inventoryChanges     chan struct{}
	ownerErrors          map[string]error
	ownerFailuresChanged chan struct{}
	// loadMu guards freshReservations; those records have not completed their first publication.
	freshReservations map[string]struct{}
}

func NewManager(config Config) (*Manager, error) {
	if config.Admission == nil || config.Context == nil || config.State == nil || config.Store == nil || config.Scripts == nil || config.WaitBootstrap == nil || config.Environment == nil || config.EnsureWorkspace == nil || config.LaunchProcess == nil || config.ResolveModelConfig == nil || config.OnChange == nil || config.OnError == nil || config.ReconcileConcurrency <= 0 {
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
		workspaceRoot: config.WorkspaceRoot, environment: config.Environment,
		changed: config.OnChange, fail: config.OnError,
		workers: config.ReconcileConcurrency, sessions: map[string]*Session{}, inventoryChanges: make(chan struct{}), freshReservations: map[string]struct{}{},
		ownerErrors: map[string]error{}, ownerFailuresChanged: make(chan struct{}),
	}
	m.dependencies = sessionDependencies{
		admission: config.Admission,
		ctx:       config.Context, state: config.State, store: config.Store,
		claude: config.Claude, harnessEnvironment: maps.Clone(config.HarnessEnvironment),
		debugCheckpoint: config.DebugCheckpoint, scripts: config.Scripts,
		codexAppServer: config.CodexAppServer, waitBootstrap: config.WaitBootstrap, environmentState: config.Environment,
		ensureWorkspace: config.EnsureWorkspace, launchProcess: config.LaunchProcess,
		resolveModelConfig: config.ResolveModelConfig,
		publish:            m.publishSession, signalOperation: m.signalOperation,
		changed: config.OnChange, fail: config.OnError,
	}
	return m, nil
}

func (m *Manager) Controller() controller.QController { return &sessionLifecycleController{manager: m} }

func (m *Manager) OwnerController() controller.QController {
	return &sessionOwnerController{manager: m}
}

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

// InventoryChangesSince returns the signal for Session inventory membership or
// ingress eligibility changes. Callers subscribe before taking an inventory snapshot.
func (m *Manager) InventoryChangesSince() <-chan struct{} {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.inventoryChanges
}

func (m *Manager) inventoryChangedLocked() {
	close(m.inventoryChanges)
	m.inventoryChanges = make(chan struct{})
}

func (m *Manager) inventoryChanged() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.inventoryChangedLocked()
}

func (m *Manager) Startup(ctx context.Context) error {
	ids, err := m.store.SessionIDs()
	if err != nil {
		return err
	}
	for _, id := range ids {
		if _, err := m.Wait(ctx, id); err != nil {
			return err
		}
	}
	return nil
}

// reserveFresh serializes the durable write with first ownership of its
// journal. The admission callback may hold the Runtime admission lock for the write but must
// release it before returning, so publication never runs under that barrier.
func (m *Manager) ReserveFresh(ctx context.Context, id string, reserve func() error) (*Session, error) {
	m.loadMu.Lock()
	if err := reserve(); err != nil {
		m.loadMu.Unlock()
		return nil, err
	}
	m.freshReservations[id] = struct{}{}
	m.loadMu.Unlock()
	return m.Wait(ctx, id)
}

// Wait observes completion of the COSI-owned Session reconstruction.
func (m *Manager) Wait(ctx context.Context, id string) (*Session, error) {
	watchCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	events := make(chan state.Event, 1)
	if err := m.state.Watch(watchCtx, lifecycle.ResourceFor(id, OwnerStatus{}).Metadata(), events); err != nil {
		return nil, err
	}
	for {
		m.mu.Lock()
		if err := m.ownerErrors[id]; err != nil {
			m.mu.Unlock()
			return nil, err
		}
		failed := m.ownerFailuresChanged
		m.mu.Unlock()
		select {
		case event, ok := <-events:
			if !ok {
				return nil, errors.New("Session owner status watch closed")
			}
			if event.Type == state.Errored {
				if event.Error == nil {
					return nil, errors.New("Session owner status watch failed")
				}
				return nil, event.Error
			}
			if event.Type != state.Created && event.Type != state.Updated {
				continue
			}
			owner, ok := event.Resource.(*typed.Resource[OwnerStatus, lifecycle.Extension[OwnerStatus]])
			if !ok {
				return nil, fmt.Errorf("unexpected Session owner status %T", event.Resource)
			}
			if owner.TypedSpec().Ready {
				if s := m.Get(id); s != nil {
					return s, nil
				}
				return nil, fmt.Errorf("Session %s owner reports ready without a live Session", id)
			}
		case <-failed:
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-m.ctx.Done():
			return nil, m.ctx.Err()
		}
	}
}

func (m *Manager) ownerFailed(id string, err error, failRuntime bool) {
	m.mu.Lock()
	if m.ownerErrors[id] == nil {
		m.ownerErrors[id] = err
	}
	close(m.ownerFailuresChanged)
	m.ownerFailuresChanged = make(chan struct{})
	m.mu.Unlock()
	if failRuntime {
		m.fail(err)
	}
}

// RetryFresh requests another owner attempt only for an explicitly retried,
// fresh reservation that failed before a Session became visible.
func (m *Manager) RetryFresh(ctx context.Context, id string) error {
	m.loadMu.Lock()
	defer m.loadMu.Unlock()
	_, fresh := m.freshReservations[id]
	m.mu.Lock()
	failure := m.ownerErrors[id]
	if !fresh || failure == nil || m.sessions[id] != nil {
		m.mu.Unlock()
		return nil
	}
	delete(m.ownerErrors, id)
	m.mu.Unlock()
	if err := safe.StateModify(ctx, m.state, lifecycle.ResourceFor(id, ownerRetry{}), func(value *typed.Resource[ownerRetry, lifecycle.Extension[ownerRetry]]) error {
		value.TypedSpec().Revision++
		return nil
	}, state.WithUpdateOwner(lifecycle.EnvironmentOwner)); err != nil {
		m.mu.Lock()
		m.ownerErrors[id] = failure
		m.mu.Unlock()
		return err
	}
	return nil
}

// Retained records are reconciled before publishing any schedulable Session reconcile snapshot.
func (m *Manager) load(ctx context.Context, id string) (*Session, error) {
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
	if _, err := workspace.Resolve(record.Cwd, m.workspaceRoot); err != nil {
		return nil, err
	}
	directory, err := m.store.Directory(id)
	if err != nil {
		return nil, err
	}
	journal, err := journal.Open(ctx, filepath.Join(directory, "journal.sqlite"), record.EventSourceId)
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
	s := newSession(deps, id, record, journal)
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
	// The Session controller resolves each published ReconcileSnapshot through Get.
	m.mu.Lock()
	m.sessions[id] = s
	m.inventoryChangedLocked()
	m.mu.Unlock()
	// Once visible to controllers, retain ownership of the journal even if
	// publication fails; shutdown must not close it under an active controller.
	initialized = true
	s.notifyBlockers()
	if err := m.publishSessionState(s, false); err != nil {
		return nil, err
	}
	if recoverRetained {
		if err := m.ResumeRecovered(s); err != nil {
			return nil, err
		}
	}
	delete(m.freshReservations, id)
	m.changed()
	return s, nil
}

func (m *Manager) ResumeRecovered(s *Session) error {
	if m.environment().Quiesced || s.journal.SealState() != nil {
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
		return m.publishSessionState(s, true)
	}
	return nil
}

// requireSuccessorRevision is called with admitMu held for a live owner's cutoff.
// Check both before the journal write and before opening admission: either
// boundary can observe a lifecycle change while acknowledgement is published.
func (m *Manager) requireSuccessorRevision(ctx context.Context, s *Session, uid string) error {
	desired, found, err := lifecycle.ReadEnvironmentDesired(ctx, m.state)
	if err != nil {
		return err
	}
	if !found || desired.Mode != lifecycle.Running || desired.IncarnationUID != uid || desired.Generation <= s.cutoff.generation || m.environment().Quiesced {
		return fmt.Errorf("Session %s successor reopen does not match the current environment revision", s.id)
	}
	return nil
}

func (m *Manager) Reopen(ctx context.Context, s *Session) error {
	env := m.environment()
	uid, known := env.IncarnationUID, env.IncarnationUID != ""
	seal := s.journal.SealState()
	mayReopen := known && !env.Quiesced
	reopening := mayReopen && seal != nil && seal.IncarnationUID != uid
	if reopening {
		// Keep the local admission gate closed through the entire successor
		// reopen. Runtime's global gate is closed here, but direct Session
		// admission and lifecycle observation must remain consistent too.
		s.admitMu.Lock()
		if s.cutoff != nil && s.cutoff.live {
			if err := m.requireSuccessorRevision(ctx, s, uid); err != nil {
				s.admitMu.Unlock()
				return err
			}
		}
		if s.cutoff == nil {
			s.cutoff = &admissionCutoff{}
		}
		if err := s.journal.Reopen(ctx, uid); err != nil {
			s.admitMu.Unlock()
			return err
		}
		s.cutoff.reopenedUID = uid
		s.admitMu.Unlock()
	}
	s.admitMu.Lock()
	reopenPending := s.cutoff != nil && s.cutoff.reopenedUID == uid
	s.admitMu.Unlock()
	ack, err := m.store.ReadAcknowledgement(s.id)
	if err != nil {
		return err
	}
	writeAck := (reopenPending && ack == nil) || (ack != nil && ack.Sealed && s.journal.SealState() == nil)
	if writeAck && ack != nil && ack.Cursor > s.journal.LastCursor() {
		return fmt.Errorf("sealed acknowledgement cursor %d exceeds unsealed journal cursor %d", ack.Cursor, s.journal.LastCursor())
	}
	if writeAck && ack == nil {
		ack = &retained.Acknowledgement{}
	}
	if writeAck {
		ack.Sealed = false
		if err := m.store.WriteAcknowledgement(s.id, *ack); err != nil {
			return err
		}
	}
	// A failed acknowledgement publication leaves the gate closed.
	// A retry may see an already-unsealed journal; the recorded reopen UID
	// identifies the successor that is allowed to complete that retry.
	s.admitMu.Lock()
	if s.cutoff != nil {
		if s.cutoff.reopenedUID != uid || s.journal.SealState() != nil {
			s.admitMu.Unlock()
			return fmt.Errorf("Session %s final admission cutoff has no completed successor reopen for %q", s.id, uid)
		}
		if s.cutoff.live {
			if err := m.requireSuccessorRevision(ctx, s, uid); err != nil {
				s.admitMu.Unlock()
				return err
			}
		}
		// A live Session needs a new reconcile after its gate reopens. A newly
		// loaded Session has not published its first snapshot yet.
		if s.cutoff.live {
			if err := s.deps.signalOperation(s.id); err != nil {
				s.admitMu.Unlock()
				return err
			}
		}
		s.cutoff = nil
	}
	s.admitMu.Unlock()
	if reopening || writeAck {
		if m.Get(s.id) == s {
			m.inventoryChanged()
		}
		m.changed()
	}
	return nil
}

func (m *Manager) ResetDemand() error {
	m.publishMu.Lock()
	defer m.publishMu.Unlock()
	for _, s := range m.Inventory() {
		if err := safe.StateModify(m.ctx, m.state, lifecycle.ResourceFor(s.id, ReconcileSnapshot{}), func(current *typed.Resource[ReconcileSnapshot, lifecycle.Extension[ReconcileSnapshot]]) error {
			current.TypedSpec().Demand = 0
			return nil
		}, state.WithUpdateOwner(lifecycle.EnvironmentOwner)); err != nil {
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
