package centralsampler

import (
	"context"
	"database/sql"
	"fmt"
)

// Codex rust-v0.162.1 core/src/tools/code_mode/delegate.rs (CoreTurnHost::notify) injects a
// notify() call as pending input on the active turn: a custom_tool_call_output for the exec call.
// core/src/session/turn.rs drains pending input before each sampling step, after the previous
// step's tool outputs; core/src/tasks/mod.rs records what remains when the turn finishes and
// clears it when the turn aborts. The tables below follow those points.

// notificationTokenLimit is the history budget upstream attaches to a notify() output: the model's
// truncation policy (Tokens(10000) for every model in models-manager/models.json) with
// utils/output-truncation's 20% serialization allowance.
const notificationTokenLimit = 12000

// QueueNotification durably queues a non-empty notify() text for the active turn. It returns
// ErrPrecondition when no turn is active; upstream then reports "no active turn" to the host.
func (s *Store) QueueNotification(ctx context.Context, owner Owner, callID, cellID, text string) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwnerHeader(ctx, tx, owner)
		if err != nil {
			return err
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if session.ActiveTurn == nil {
			return ErrPrecondition
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO sampler_notification(conversation_id,call_id,cell_id,text) VALUES($1,$2,$3,$4)`,
			conversation.ID, callID, cellID, text)
		return err
	})
}

// DrainNotifications appends queued notify() outputs to the transcript. The caller must have
// recorded every output of the current step first.
func (s *Store) DrainNotifications(ctx context.Context, owner Owner) (bool, error) {
	// Skip decoding the transcript on the common path; a later insert waits for the next drain point.
	var queued bool
	if err := s.db.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_notification WHERE conversation_id=$1)`,
		owner.ConversationID).Scan(&queued); err != nil || !queued {
		return false, err
	}
	drained := false
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		if conversation.Pending != nil {
			return ErrPrecondition
		}
		session, err := sessionForConversation(ctx, tx, conversation.ID)
		if err != nil {
			return err
		}
		if drained, err = s.settleNotifications(ctx, tx, owner, conversation, session, true); err != nil {
			return err
		}
		return updateConversation(ctx, tx, conversation)
	})
	return drained, err
}

// settleNotifications appends queued outputs to conversation (record) or discards them. Either way
// it leaves evidence per notification; the caller persists conversation.
func (s *Store) settleNotifications(ctx context.Context, tx *sql.Tx, owner Owner, conversation *Conversation, session *Session, record bool) (bool, error) {
	rows, err := tx.QueryContext(ctx, `WITH drained AS (DELETE FROM sampler_notification WHERE conversation_id=$1
		RETURNING sequence,call_id,cell_id,text) SELECT sequence,call_id,cell_id,text FROM drained ORDER BY sequence`, conversation.ID)
	if err != nil {
		return false, err
	}
	type queued struct {
		sequence             int64
		callID, cellID, text string
	}
	var notifications []queued
	for rows.Next() {
		var notification queued
		if err := rows.Scan(&notification.sequence, &notification.callID, &notification.cellID, &notification.text); err != nil {
			rows.Close()
			return false, err
		}
		notifications = append(notifications, notification)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return false, err
	}
	if err := rows.Close(); err != nil {
		return false, err
	}
	for _, notification := range notifications {
		operationID := fmt.Sprintf("%s:notification:%d", conversation.ID, notification.sequence)
		if !record {
			if err := s.recordEvidence(ctx, tx, owner, conversation, session, operationID, "notification", "discarded",
				map[string]any{"call_id": notification.callID, "cell_id": notification.cellID}); err != nil {
				return false, err
			}
			continue
		}
		call, err := conversation.Archive.FindCall(notification.callID)
		if err != nil {
			return false, err
		}
		output := []ContentPart{{Type: "input_text", Text: truncateMiddleWithTokenBudget(notification.text, notificationTokenLimit)}}
		if err := s.recordEvidence(ctx, tx, owner, conversation, session, operationID, "notification", "recorded",
			map[string]any{"cell_id": notification.cellID, "call_id": notification.callID, "output": output}); err != nil {
			return false, err
		}
		if err := conversation.appendToolResult(call, output); err != nil {
			return false, err
		}

	}
	return record && len(notifications) > 0, nil
}
