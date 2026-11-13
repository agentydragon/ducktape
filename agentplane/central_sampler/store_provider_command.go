package centralsampler

import (
	"context"
	"database/sql"
	"errors"
	"strings"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"google.golang.org/protobuf/proto"
)

// FailProviderCommand settles a terminal provider-only failure while leaving unrelated live
// execution handles and the Session itself usable.
func (s *Store) FailProviderCommand(ctx context.Context, owner Owner, sessionID, commandID, intentID, reason string) error {
	reason = strings.TrimSpace(reason)
	if commandID == "" || intentID == "" || reason == "" || len(reason) > 4096 {
		return protocol.ErrInvalid
	}
	return s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		if session.ConversationID != owner.ConversationID || session.ActiveCommand == nil || *session.ActiveCommand != commandID ||
			conversation.Pending != nil || conversation.Stopped || session.Blocked ||
			(session.ActiveTurn != nil && *session.ActiveTurn != commandID) {
			return protocol.ErrPrecondition
		}
		if err := checkInterrupt(ctx, tx, owner); err != nil {
			return err
		}
		command, err := commandFor(ctx, tx, session.ID, commandID)
		if err != nil {
			return err
		}
		if command.Outcome != samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED {
			return protocol.ErrPrecondition
		}
		payload := &commandpb.Command{}
		if err := proto.Unmarshal(command.Payload, payload); err != nil {
			return err
		}
		if payload.GetSubmitInput() == nil && payload.GetCompact() == nil {
			return protocol.ErrPrecondition
		}
		confirmedInput := payload.GetSubmitInput() != nil
		if confirmedInput && session.ActiveTurn == nil {
			return protocol.ErrPrecondition
		}

		var status, failureClass, suppressed string
		var attemptNo, budget int
		var providerCommand sql.NullString
		err = tx.QueryRowContext(ctx, `SELECT result->>'status',COALESCE(result->>'failure_class',''),
			COALESCE(result->>'provider_retry_suppressed',''),provider_attempt_no,provider_attempt_budget,provider_command_id
			FROM sampler_intent i WHERE i.id=$1 AND i.conversation_id=$2 AND i.request->>'kind' IN ('sampling','compaction')
				AND i.result->>'status'='abandoned' AND NOT EXISTS(SELECT 1 FROM sampler_intent n WHERE n.previous_provider_intent_id=i.id)
			FOR UPDATE`, intentID, owner.ConversationID).Scan(&status, &failureClass, &suppressed, &attemptNo, &budget, &providerCommand)
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrPrecondition
		}
		if err != nil {
			return err
		}
		if status != string(IntentResultAbandoned) || !providerCommand.Valid || providerCommand.String != commandID || attemptNo <= 0 || budget < attemptNo {
			return protocol.ErrPrecondition
		}
		// This method is called only after the runtime has a permanent provider-level reason
		// to stop this command. That reason can include a changed route after a transient
		// attempt; preserve the predecessor's result and evidence while clearing its due time.
		if suppressed == string(ProviderRetrySuppressionInterrupted) ||
			failureClass == string(ProviderAttemptInterrupted) || failureClass == string(ProviderAttemptDraining) ||
			failureClass != "" && failureClass != string(ProviderAttemptPermanent) && failureClass != string(ProviderAttemptTransient) {
			return protocol.ErrPrecondition
		}
		unresolved, err := hasUnresolvedIntent(ctx, tx, conversation.ID, "")
		if err != nil {
			return err
		}
		if unresolved {
			return protocol.ErrPrecondition
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET provider_retry_not_before=NULL WHERE id=$1`, intentID); err != nil {
			return err
		}

		if err := setCommandOutcome(ctx, tx, session.ID, commandID, samplerpb.CommandOutcome_COMMAND_OUTCOME_FAILED, &reason); err != nil {
			return err
		}
		// Input confirmation has already settled the command as effected in History.
		// Preserve that receipt while failing the sampler run and its turn.
		if !confirmedInput {
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandFailed{
				CommandFailed: &eventpb.CommandFailed{CommandId: commandID, Reason: reason},
			}}); err != nil {
				return err
			}
		}
		if session.ActiveTurn != nil {
			if err := settleTurnInterrupts(ctx, tx, session, TurnFailed); err != nil {
				return err
			}
			if _, err := appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{
				TurnCompleted: &eventpb.TurnCompleted{TurnId: commandID, Status: eventpb.TurnStatus_TURN_STATUS_FAILED, Error: reason},
			}}); err != nil {
				return err
			}
			session.ActiveTurn = nil
		} else if err := appendProgress(ctx, tx, session, &eventpb.ExecutionProgress{
			OperationId: commandID, Kind: eventpb.ExecutionProgress_KIND_COMMAND, Phase: eventpb.ExecutionProgress_PHASE_FAILED,
		}); err != nil {
			return err
		}
		session.ActiveCommand = nil
		conversation.Stopped = false
		if err := updateConversation(ctx, tx, conversation); err != nil {
			return err
		}
		return persistSession(ctx, tx, session)
	})
}
