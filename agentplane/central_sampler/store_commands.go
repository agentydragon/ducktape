package centralsampler

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func (s *Store) NextCommand(ctx context.Context, owner Owner, sessionID string) (*Command, error) {
	var next *Command
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		if session.Blocked {
			return protocol.ErrPrecondition
		}
		if session.ActiveCommand != nil {
			if err := s.ensureTurnInterrupt(ctx, tx, owner, session); err != nil {
				return err
			}
			next, err = commandFor(ctx, tx, sessionID, *session.ActiveCommand)
			return err
		}
		rows, err := tx.QueryContext(ctx, `SELECT session_id,id,payload,admission,outcome,failure_reason FROM sampler_command
			WHERE session_id=$1 AND outcome=$2 ORDER BY admission LIMIT 1 FOR UPDATE`, sessionID,
			int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
		if err != nil {
			return err
		}
		if rows.Next() {
			next, err = scanCommand(rows)
			rows.Close()
			if err != nil {
				return err
			}
		} else {
			rows.Close()
			return rows.Err()
		}
		payload := &commandpb.Command{}
		if err := proto.Unmarshal(next.Payload, payload); err != nil {
			return err
		}
		switch {
		case payload.GetSubmitInput() != nil:
			if conversation.Pending != nil {
				return &protocol.UnknownEffectError{Reason: "unresolved intent blocks queued input; automatic replay is unsafe"}
			}
			// Codex records environment updates before the turn's user input.
			if err := s.recordEnvironmentContext(conversation, session); err != nil {
				return err
			}
			text := payload.GetSubmitInput().GetText()
			if err := conversation.apply(func(state protocol.Context) error {
				return state.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: text}})
			}); err != nil {
				return err
			}
			conversation.Stopped = false
			session.ActiveTurn = &next.ID
			session.ActiveCommand = &next.ID
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{
				HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{
					HarnessMessageId: next.ID,
					Text:             text, OriginCommandIds: []string{next.ID}, TurnId: next.ID,
				},
			}}); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{
				TurnStarted: &eventpb.TurnStarted{TurnId: next.ID, Model: session.Model},
			}}); err != nil {
				return err
			}
		case payload.GetChangeModel() != nil:
			previous := session.Model
			session.Model = payload.GetChangeModel().GetModel()
			next.Outcome = samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED
			if err := setCommandOutcome(ctx, tx, sessionID, next.ID, next.Outcome, nil); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_ModelChanged{
				ModelChanged: &eventpb.ModelChanged{CommandId: next.ID, PreviousModel: previous, Model: session.Model},
			}}); err != nil {
				return err
			}
		case payload.GetChangeReasoningEffort() != nil:
			previous := session.Effort
			session.Effort = payload.GetChangeReasoningEffort().GetEffort()
			if err := conversation.apply(func(state protocol.Context) error { return state.ChangeEffort(session.Effort) }); err != nil {
				return err
			}
			next.Outcome = samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED
			if err := setCommandOutcome(ctx, tx, sessionID, next.ID, next.Outcome, nil); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_ReasoningEffortChanged{
				ReasoningEffortChanged: &eventpb.ReasoningEffortChanged{CommandId: next.ID, PreviousEffort: previous, Effort: session.Effort},
			}}); err != nil {
				return err
			}
		case payload.GetInterruptTurn() != nil:
			next.Outcome = samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED
			if err := setCommandOutcome(ctx, tx, sessionID, next.ID, next.Outcome, nil); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandNoop{
				CommandNoop: &eventpb.CommandNoop{CommandId: next.ID, Reason: "target turn has ended"},
			}}); err != nil {
				return err
			}
		case payload.GetCompact() != nil:
			session.ActiveCommand = &next.ID
		default:
			return protocol.ErrInvalid
		}
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		return persistSession(ctx, tx, session)
	})
	return next, err
}

// steeringInputs returns the queued SubmitInput commands at the head of the session's queue, in
// admission order, that can join its active input turn. Other queued commands keep their order: input
// admitted after one waits for a later turn. A pending interrupt of the turn takes precedence.
func steeringInputs(ctx context.Context, tx *sql.Tx, conversation *Conversation, session *Session) ([]*Command, []*commandpb.SubmitInput, error) {
	if session.ActiveTurn == nil || session.ActiveCommand == nil || *session.ActiveCommand != *session.ActiveTurn {
		return nil, nil, nil
	}
	var interrupting bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_interrupt WHERE conversation_id=$1 AND generation=$2 AND target_turn=$3 AND result IS NULL)`,
		conversation.ID, conversation.Generation, *session.ActiveTurn).Scan(&interrupting); err != nil {
		return nil, nil, err
	}
	if interrupting {
		return nil, nil, nil
	}
	rows, err := tx.QueryContext(ctx, `SELECT session_id,id,payload,admission,outcome,failure_reason FROM sampler_command
		WHERE session_id=$1 AND outcome=$2 ORDER BY admission FOR UPDATE`, session.ID,
		int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	var commands []*Command
	var inputs []*commandpb.SubmitInput
	for rows.Next() {
		command, err := scanCommand(rows)
		if err != nil {
			return nil, nil, err
		}
		if command.ID == *session.ActiveCommand {
			continue
		}
		payload := &commandpb.Command{}
		if err := proto.Unmarshal(command.Payload, payload); err != nil {
			return nil, nil, err
		}
		if payload.GetSubmitInput() == nil {
			break
		}
		commands = append(commands, command)
		inputs = append(inputs, payload.GetSubmitInput())
	}
	return commands, inputs, rows.Err()
}

// HasSteeringInput reports whether queued user input can join the session's active turn. A running
// cell's unresolved intent does not hide it: that is when a foreground observation should yield.
func (s *Store) HasSteeringInput(ctx context.Context, owner Owner, sessionID string) (bool, error) {
	found := false
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		commands, _, err := steeringInputs(ctx, tx, conversation, session)
		found = len(commands) > 0
		return err
	})
	return found, err
}

// DrainSteeringInput records queued user input into the active turn and applies its commands.
//
// Codex rust-v0.162.1 core/src/session/turn.rs run_turn: input submitted while a turn runs is
// pending input, recorded at the start of the next sampling step; pending input also keeps a turn
// that would otherwise end sampling.
func (s *Store) DrainSteeringInput(ctx context.Context, owner Owner, sessionID string) (bool, error) {
	drained := false
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		// Input joins only at a step boundary, never between an intent and its outcome.
		if conversation.Pending != nil {
			return nil
		}
		commands, inputs, err := steeringInputs(ctx, tx, conversation, session)
		if err != nil || len(commands) == 0 {
			return err
		}
		for index, command := range commands {
			text := inputs[index].GetText()
			if err := conversation.apply(func(state protocol.Context) error {
				return state.AppendUser([]protocol.ContentPart{{Type: "input_text", Text: text}})
			}); err != nil {
				return err
			}
			if err := setCommandOutcome(ctx, tx, sessionID, command.ID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED, nil); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_HarnessUserMessageConfirmed{
				HarnessUserMessageConfirmed: &eventpb.HarnessUserMessageConfirmed{
					HarnessMessageId: command.ID,
					Text:             text, OriginCommandIds: []string{command.ID}, TurnId: *session.ActiveTurn,
				},
			}}); err != nil {
				return err
			}
		}
		drained = true
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		return persistSession(ctx, tx, session)
	})
	return drained, err
}

func scanCommand(row rowScanner) (*Command, error) {
	var command Command
	var outcome int32
	var failure sql.NullString
	if err := row.Scan(&command.SessionID, &command.ID, &command.Payload, &command.Admission, &outcome, &failure); err != nil {
		return nil, err
	}
	command.Outcome = samplerpb.CommandOutcome(outcome)
	if failure.Valid {
		command.FailureReason = &failure.String
	}
	return &command, nil
}

func (s *Store) ensureTurnInterrupt(ctx context.Context, tx *sql.Tx, owner Owner, session *Session) error {
	if session.ActiveTurn == nil {
		return nil
	}
	rows, err := tx.QueryContext(ctx, `SELECT payload FROM sampler_command WHERE session_id=$1 AND outcome=$2 ORDER BY admission`,
		session.ID, int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
	if err != nil {
		return err
	}
	found := false
	for rows.Next() {
		var raw []byte
		if err := rows.Scan(&raw); err != nil {
			rows.Close()
			return err
		}
		candidate := &commandpb.Command{}
		if err := proto.Unmarshal(raw, candidate); err != nil {
			rows.Close()
			return err
		}
		interrupt := candidate.GetInterruptTurn()
		if interrupt == nil || interrupt.GetTurnId() != *session.ActiveTurn {
			continue
		}
		found = true
		break
	}
	rowsErr := rows.Err()
	rows.Close()
	if rowsErr != nil {
		return rowsErr
	}
	if !found {
		return nil
	}
	var pending bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_interrupt WHERE conversation_id=$1 AND generation=$2 AND target_turn=$3 AND result IS NULL)`,
		owner.ConversationID, owner.Generation, *session.ActiveTurn).Scan(&pending); err != nil {
		return err
	}
	if pending {
		return nil
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO sampler_interrupt(id,conversation_id,generation,target_turn,result) VALUES($1,$2,$3,$4,NULL)`,
		uuid.New(), owner.ConversationID, owner.Generation, *session.ActiveTurn)
	return err
}

func (s *Store) FinishCommand(ctx context.Context, owner Owner, sessionID, commandID string, result TurnResult) error {
	if !result.finishesCommand() {
		return fmt.Errorf("%w: turn result %q does not finish a command", protocol.ErrInvalid, result)
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		command, err := commandFor(ctx, tx, sessionID, commandID)
		if err != nil {
			return err
		}
		if command.Outcome != samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED {
			return nil
		}
		session.ActiveCommand = nil
		// Codex records pending turn input when a turn finishes and clears it when the turn aborts.
		if _, err := s.settleNotifications(ctx, tx, owner, conversation, session, result == TurnDone); err != nil {
			return err
		}
		outcome := result.Outcome()
		if err := setCommandOutcome(ctx, tx, sessionID, commandID, outcome, nil); err != nil {
			return err
		}
		session.Blocked = result == TurnEffectsUnknown
		session.BlockedReason = nil
		if session.Blocked {
			reason, err := blockedReason(ctx, tx, conversation, "the command ended with an unknown effect outcome")
			if err != nil {
				return err
			}
			session.BlockedReason = &reason
		}
		if session.ActiveTurn != nil && *session.ActiveTurn == commandID {
			if err := settleTurnInterrupts(ctx, tx, session, result); err != nil {
				return err
			}
			rows, err := tx.QueryContext(ctx, `SELECT session_id,id,payload,admission,outcome,failure_reason FROM sampler_command
				WHERE session_id=$1 AND outcome=$2 ORDER BY admission FOR UPDATE`, sessionID,
				int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
			if err != nil {
				return err
			}
			interruptID := ""
			var interrupts []*Command
			for rows.Next() {
				candidate, err := scanCommand(rows)
				if err != nil {
					rows.Close()
					return err
				}
				payload := &commandpb.Command{}
				if err := proto.Unmarshal(candidate.Payload, payload); err != nil {
					rows.Close()
					return err
				}
				interrupt := payload.GetInterruptTurn()
				if interrupt == nil || interrupt.GetTurnId() != commandID {
					continue
				}
				interrupts = append(interrupts, candidate)
			}
			if err := rows.Err(); err != nil {
				rows.Close()
				return err
			}
			rows.Close()
			for _, candidate := range interrupts {
				if err := setCommandOutcome(ctx, tx, sessionID, candidate.ID, outcome, nil); err != nil {
					return err
				}
				if result == TurnDone {
					if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandNoop{
						CommandNoop: &eventpb.CommandNoop{CommandId: candidate.ID, Reason: "turn completed before interrupt was applied"},
					}}); err != nil {
						return err
					}
				} else {
					interruptID = candidate.ID
				}
			}
			turnStatus := eventpb.TurnStatus_TURN_STATUS_INTERRUPTED
			if result == TurnDone {
				turnStatus = eventpb.TurnStatus_TURN_STATUS_COMPLETED
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{
				TurnCompleted: &eventpb.TurnCompleted{TurnId: commandID, Status: turnStatus, InterruptedByCommandId: interruptID},
			}}); err != nil {
				return err
			}
			session.ActiveTurn = nil
		} else if err := appendProgress(ctx, tx, session, &eventpb.ExecutionProgress{OperationId: commandID, Kind: eventpb.ExecutionProgress_KIND_COMMAND, Phase: eventpb.ExecutionProgress_PHASE_COMPLETED}); err != nil {
			return err
		}
		if session.Blocked {
			if err := s.blockQueue(ctx, tx, session, *session.BlockedReason); err != nil {
				return err
			}
		}
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		return persistSession(ctx, tx, session)
	})
}

func settleTurnInterrupts(ctx context.Context, tx *sql.Tx, session *Session, result TurnResult) error {
	if session.ActiveTurn == nil {
		return nil
	}
	wire, err := marshal(map[string]any{"status": "not_applied", "reason": "target turn ended", "turn_result": string(result)})
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE sampler_interrupt SET result=$3 WHERE conversation_id=$1 AND target_turn=$2 AND result IS NULL`, session.ConversationID, *session.ActiveTurn, wire)
	return err
}

func (s *Store) blockQueue(ctx context.Context, tx *sql.Tx, session *Session, reason string) error {
	rows, err := tx.QueryContext(ctx, `SELECT session_id,id,payload,admission,outcome,failure_reason FROM sampler_command
		WHERE session_id=$1 AND outcome=$2 ORDER BY admission FOR UPDATE`, session.ID,
		int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
	if err != nil {
		return err
	}
	var queued []*Command
	for rows.Next() {
		command, err := scanCommand(rows)
		if err != nil {
			rows.Close()
			return err
		}
		queued = append(queued, command)
	}
	rowsErr := rows.Err()
	rows.Close()
	if rowsErr != nil {
		return rowsErr
	}
	for _, command := range queued {
		payload := &commandpb.Command{}
		if err := proto.Unmarshal(command.Payload, payload); err != nil {
			return err
		}
		outcome := samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED
		if payload.GetInterruptTurn() != nil {
			outcome = samplerpb.CommandOutcome_COMMAND_OUTCOME_UNKNOWN
		}
		if err := setCommandOutcome(ctx, tx, command.SessionID, command.ID, outcome, &reason); err != nil {
			return err
		}
		if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandFailed{
			CommandFailed: &eventpb.CommandFailed{CommandId: command.ID, Reason: reason},
		}}); err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) FailExecution(ctx context.Context, owner Owner, sessionID string, commandID *string, failure error) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		if commandID == nil {
			// execute can fail before NextCommand returns the durable active command (for
			// example, while reconciling a lost code host during successor startup).
			// Settle that command from Session state so an already-confirmed input is not
			// failed again by blockQueue.
			commandID = session.ActiveCommand
		}
		return s.settleFailure(ctx, tx, owner, conversation, session, commandID, failure)
	})
}

func blockedReason(ctx context.Context, tx *sql.Tx, conversation *Conversation, summary string) (string, error) {
	cause := "the execution ended before its external effects could be confirmed"
	if conversation.Pending != nil {
		cause = fmt.Sprintf("%s intent %s has an unresolved outcome", conversation.Pending.Kind, conversation.Pending.ID)
	} else {
		var intentID, kind sql.NullString
		err := tx.QueryRowContext(ctx, `SELECT id::text,request->>'kind' FROM sampler_intent
			WHERE conversation_id=$1 AND result IS NULL ORDER BY id LIMIT 1`, conversation.ID).Scan(&intentID, &kind)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return "", err
		}
		if intentID.Valid {
			cause = fmt.Sprintf("%s intent %s has an unresolved outcome", kind.String, intentID.String)
		} else {
			var cellID string
			err := tx.QueryRowContext(ctx, `SELECT id FROM sampler_cell WHERE conversation_id=$1 AND status='live' ORDER BY id LIMIT 1`, conversation.ID).Scan(&cellID)
			if err != nil && !errors.Is(err, sql.ErrNoRows) {
				return "", err
			}
			if err == nil {
				cause = fmt.Sprintf("JavaScript cell %s has no live host", cellID)
			} else {
				var processID string
				err := tx.QueryRowContext(ctx, `SELECT id FROM sampler_process WHERE conversation_id=$1 AND status NOT IN ('exited','exited_unread','abandoned') ORDER BY id LIMIT 1`, conversation.ID).Scan(&processID)
				if err != nil && !errors.Is(err, sql.ErrNoRows) {
					return "", err
				}
				if err == nil {
					cause = fmt.Sprintf("executor process %s has no confirmed terminal result", processID)
				}
			}
		}
	}
	return fmt.Sprintf("%s; session is blocked because %s. Inspect the linked boundary evidence and reconcile external effects before manual recovery. Automatic effect replay is disabled.", summary, cause), nil
}

func (s *Store) settleFailure(ctx context.Context, tx *sql.Tx, owner Owner, conversation *Conversation, session *Session,
	commandID *string, failure error,
) error {
	var command *Command
	confirmedInput := false
	if commandID != nil {
		var err error
		command, err = commandFor(ctx, tx, session.ID, *commandID)
		if err != nil && !errors.Is(err, protocol.ErrMissing) {
			return err
		}
		if command != nil && command.Outcome != samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED {
			return nil
		}
		if command != nil && session.ActiveTurn != nil && *session.ActiveTurn == command.ID {
			payload := &commandpb.Command{}
			if err := proto.Unmarshal(command.Payload, payload); err != nil {
				return err
			}
			confirmedInput = payload.GetSubmitInput() != nil
		}
	}
	unresolved, err := hasUnresolvedIntent(ctx, tx, conversation.ID, "")
	if err != nil {
		return err
	}
	liveCell, err := hasLiveCell(ctx, tx, conversation.ID)
	if err != nil {
		return err
	}
	liveProcess, err := hasLiveProcess(ctx, tx, conversation.ID)
	if err != nil {
		return err
	}
	var knownFailure *protocol.KnownFailureError
	known := errors.As(failure, &knownFailure) && knownFailure != nil
	// TODO(SAMPLER_INTENT_LEASES): this terminal block (owner alive, provider failure or
	// unconfirmed interrupt cleanup) goes away once sampling attempts are leased and a lapsed
	// lease is reconciled with backoff (DAG section 5). Until then only a lost host or lost owner
	// settles it automatically (ReconcileCodeHostLoss); otherwise RecoverSession.
	session.Blocked = !known || conversation.Pending != nil || unresolved || liveCell || liveProcess
	session.ActiveCommand = nil
	// Deviation: Codex records pending input after a failed turn; a failed turn here may still hold
	// an unresolved intent whose output must precede it, so queued notify() outputs are discarded.
	if _, err := s.settleNotifications(ctx, tx, owner, conversation, session, false); err != nil {
		return err
	}
	reason := "execution stopped; inspect durable boundary evidence"
	if failure != nil {
		reason = protocol.DisplayText(failure.Error())
	}
	session.BlockedReason = nil
	if session.Blocked {
		reason, err = blockedReason(ctx, tx, conversation, reason)
		if err != nil {
			return err
		}
		session.BlockedReason = &reason
	}
	if command != nil {
		outcome := samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED
		if session.Blocked {
			outcome = samplerpb.CommandOutcome_COMMAND_OUTCOME_UNKNOWN
		}
		if err := setCommandOutcome(ctx, tx, command.SessionID, command.ID, outcome, &reason); err != nil {
			return err
		}
		// Confirmation already settles this command in History; retain only the sampler's run outcome.
		if !confirmedInput {
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandFailed{
				CommandFailed: &eventpb.CommandFailed{CommandId: command.ID, Reason: reason},
			}}); err != nil {
				return err
			}
		}
	}
	conversation.Stopped = true
	phase := eventpb.ExecutionProgress_PHASE_FAILED
	if session.Blocked {
		phase = eventpb.ExecutionProgress_PHASE_BLOCKED
	}
	if err := appendProgress(ctx, tx, session, &eventpb.ExecutionProgress{
		OperationId: session.ID, Kind: eventpb.ExecutionProgress_KIND_SESSION, Phase: phase,
		Detail: &eventpb.ExecutionProgress_SessionStopped_{SessionStopped: &eventpb.ExecutionProgress_SessionStopped{Reason: reason}},
	}); err != nil {
		return err
	}
	if session.ActiveTurn != nil {
		if err := settleTurnInterrupts(ctx, tx, session, TurnFailed); err != nil {
			return err
		}
		rows, err := tx.QueryContext(ctx, `SELECT session_id,id,payload,admission,outcome,failure_reason FROM sampler_command WHERE session_id=$1 AND outcome=$2 ORDER BY admission FOR UPDATE`,
			session.ID, int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
		if err != nil {
			return err
		}
		var interrupts []*Command
		for rows.Next() {
			candidate, err := scanCommand(rows)
			if err != nil {
				rows.Close()
				return err
			}
			payload := &commandpb.Command{}
			if err := proto.Unmarshal(candidate.Payload, payload); err != nil {
				rows.Close()
				return err
			}
			interrupt := payload.GetInterruptTurn()
			if interrupt == nil || interrupt.GetTurnId() != *session.ActiveTurn {
				continue
			}
			interrupts = append(interrupts, candidate)
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()
		for _, candidate := range interrupts {
			if err := setCommandOutcome(ctx, tx, session.ID, candidate.ID, samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED, nil); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandNoop{
				CommandNoop: &eventpb.CommandNoop{CommandId: candidate.ID, Reason: "target turn failed"},
			}}); err != nil {
				return err
			}
		}
		if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{
			TurnCompleted: &eventpb.TurnCompleted{TurnId: *session.ActiveTurn, Status: eventpb.TurnStatus_TURN_STATUS_FAILED, Error: reason},
		}}); err != nil {
			return err
		}
		session.ActiveTurn = nil
	}
	if session.Blocked {
		if err := s.blockQueue(ctx, tx, session, "session blocked by unresolved effects"); err != nil {
			return err
		}
	}
	if err := updateConversation(ctx, tx, conversation); err != nil {
		return err
	}
	return persistSession(ctx, tx, session)
}

func commandFor(ctx context.Context, tx *sql.Tx, sessionID, commandID string) (*Command, error) {
	var command Command
	var outcome int32
	var failure sql.NullString
	err := tx.QueryRowContext(ctx, `SELECT session_id,id,payload,admission,outcome,failure_reason FROM sampler_command WHERE session_id=$1 AND id=$2`, sessionID, commandID).Scan(
		&command.SessionID, &command.ID, &command.Payload, &command.Admission, &outcome, &failure,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, protocol.ErrMissing
	}
	if err != nil {
		return nil, err
	}
	command.Outcome = samplerpb.CommandOutcome(outcome)
	if failure.Valid {
		command.FailureReason = &failure.String
	}
	return &command, nil
}

func setCommandOutcome(ctx context.Context, tx *sql.Tx, sessionID, commandID string, outcome samplerpb.CommandOutcome, reason *string) error {
	_, err := tx.ExecContext(ctx, `UPDATE sampler_command SET outcome=$3,failure_reason=$4 WHERE session_id=$1 AND id=$2`, sessionID, commandID, int32(outcome), nullableString(reason))
	return err
}
