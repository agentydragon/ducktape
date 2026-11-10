package session

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/journal"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type admissionCutoff struct {
	// generation is the revision that first closed a live owner's admission.
	generation uint64
	// settled records undispatched command settlement after the cutoff.
	settled bool
	// reopenedUID identifies a durable reopen awaiting acknowledgement.
	reopenedUID string
	// live requires a newer exact Running revision and a reconcile signal.
	// A newly loaded sealed journal uses the startup boundary instead.
	live bool
}

// closeAdmission is the per-Session final cutoff. Admission and sealing take
// the same lock, so a racing command either commits before this boundary or
// returns without creating a journal entry.
func (s *Session) closeAdmission(generation uint64) {
	s.admitMu.Lock()
	if s.cutoff == nil {
		s.cutoff = &admissionCutoff{generation: generation, live: true}
	}
	s.admitMu.Unlock()
}

func (s *Session) admissionClosed() bool {
	s.admitMu.Lock()
	defer s.admitMu.Unlock()
	return (s.cutoff != nil && s.cutoff.settled) || s.journal.SealState() != nil
}

func (s *Session) Admit(ctx context.Context, command *commandpb.Command, lameduck bool) (*eventlogpb.EventEntry, error) {
	s.admitMu.Lock()
	defer s.admitMu.Unlock()
	old, err := s.journal.GetCommand(ctx, command.CommandId)
	if err != nil {
		return nil, err
	}
	closed := s.executionClosed()
	if old == nil && s.journal.SealState() != nil && command.GetInterruptTurn() != nil {
		return nil, journal.ErrSealed
	}
	if old == nil && (s.cutoff != nil || s.journal.SealState() != nil) {
		return nil, status.Error(codes.Aborted, "this Session no longer admits new commands")
	}
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
		if closed || s.cutoff != nil {
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
	s.deps.changed()
	return entry, nil
}
