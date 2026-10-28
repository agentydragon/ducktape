package session

import (
	"context"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (s *Session) Admit(ctx context.Context, command *commandpb.Command, lameduck bool) (*eventlogpb.EventEntry, error) {
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
	s.deps.changed()
	return entry, nil
}
