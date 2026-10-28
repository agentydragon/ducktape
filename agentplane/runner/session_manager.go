package runner

import (
	"context"
	"path/filepath"
	"sort"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/agentydragon/ducktape/agentplane/runner/workspace"
)

// sessionManager owns the in-memory registry and reconstruction of durable
// sessions. Runner's admission mutex remains the sole quiesce barrier.
type sessionManager struct {
	ctx           context.Context
	store         *retained.Store
	workspaceRoot string
	dependencies  sessionDependencies
	destination   func(string) string
	publishSeal   func(*Session) error
	publishAck    func(string, retained.Acknowledgement) error
	publish       func(*Session, bool)
	changed       func()

	mu       sync.Mutex
	createMu sync.Mutex
	loadMu   sync.Mutex
	sessions map[string]*Session
}

func (m *sessionManager) get(id string) *Session {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.sessions[id]
}

func (m *sessionManager) inventory() []*Session {
	m.mu.Lock()
	defer m.mu.Unlock()
	sessions := make([]*Session, 0, len(m.sessions))
	for _, s := range m.sessions {
		sessions = append(sessions, s)
	}
	sort.Slice(sessions, func(i, j int) bool { return sessions[i].id < sessions[j].id })
	return sessions
}

func (m *sessionManager) startup(ctx context.Context) error {
	ids, err := m.store.SessionIDs()
	if err != nil {
		return err
	}
	for _, id := range ids {
		s, err := m.load(ctx, id)
		if err != nil {
			return err
		}
		if err := m.resumeRecovered(s); err != nil {
			return err
		}
	}
	return nil
}

// reserveFresh serializes the durable write with first ownership of its
// journal. The admission callback may hold Runner.mu for the write but must
// release it before returning, so publication never runs under that barrier.
func (m *sessionManager) reserveFresh(ctx context.Context, id string, reserve func() error) (*Session, error) {
	m.loadMu.Lock()
	defer m.loadMu.Unlock()
	if err := reserve(); err != nil {
		return nil, err
	}
	return m.loadLocked(ctx, id, false)
}

// Retained records are reconciled before publishing any schedulable Session input.
func (m *sessionManager) load(ctx context.Context, id string) (*Session, error) {
	m.loadMu.Lock()
	defer m.loadMu.Unlock()
	return m.loadLocked(ctx, id, true)
}

func (m *sessionManager) loadLocked(ctx context.Context, id string, recoverRetained bool) (*Session, error) {
	if s := m.get(id); s != nil {
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
	deps := m.dependencies
	deps.destination = m.destination(id)
	s := newSession(deps, id, &record, journal)
	s.loading = true
	if err := s.load(ctx); err != nil {
		_ = journal.Close()
		return nil, err
	}
	if recoverRetained {
		if err := s.recover(ctx); err != nil {
			_ = journal.Close()
			return nil, err
		}
	}
	s.mu.Lock()
	s.loading = false
	s.mu.Unlock()
	m.mu.Lock()
	m.sessions[id] = s
	m.mu.Unlock()
	if err := m.publishSeal(s); err != nil {
		return nil, err
	}
	ack, err := m.store.ReadAcknowledgement(id)
	if err != nil {
		return nil, err
	}
	if ack != nil {
		if err := m.publishAck(id, *ack); err != nil {
			return nil, err
		}
	}
	m.publish(s, false)
	if err := m.reopen(ctx, s); err != nil {
		return nil, err
	}
	m.changed()
	return s, nil
}

func (m *sessionManager) resumeRecovered(s *Session) error {
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

func (m *sessionManager) reopen(ctx context.Context, s *Session) error {
	uid, known := m.store.IncarnationUID()
	seal := s.journal.SealState()
	if !known || m.store.Lameduck() || seal == nil || seal.IncarnationUID == uid {
		return nil
	}
	if err := s.journal.Reopen(ctx, uid); err != nil {
		return err
	}
	ack, err := m.store.ReadAcknowledgement(s.id)
	if err != nil {
		return err
	}
	if ack == nil {
		ack = &retained.Acknowledgement{}
	}
	ack.Sealed = false
	if err := m.store.WriteAcknowledgement(s.id, *ack); err != nil {
		return err
	}
	if err := m.publishAck(s.id, *ack); err != nil {
		return err
	}
	if err := m.publishSeal(s); err != nil {
		return err
	}
	m.changed()
	return nil
}
