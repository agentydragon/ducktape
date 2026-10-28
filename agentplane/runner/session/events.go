package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	harness "github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/journal"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

type pendingReconciliation struct {
	turnID  string
	adapter harness.Adapter
	sources []uint64
	ready   bool
}

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
	entry, err := s.journal.Append(ctx, e, journal.AppendOptions{Sources: sources, TerminalCommandIDs: ids})
	if err != nil {
		return nil, err
	}
	s.apply(entry, ids)
	s.notifyBlockers()
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
	if err := s.deps.store.Write(s.id, record); err != nil {
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
	if err := s.deps.store.Write(s.id, record); err != nil {
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
			return fmt.Errorf("%w %q", journal.ErrUnknownCommand, id)
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
	configured := s.deps.debugCheckpoint
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
	configured := s.deps.debugCheckpoint
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
