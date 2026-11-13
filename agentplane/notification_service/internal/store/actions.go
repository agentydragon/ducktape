package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// ErrActionHistoryGap is a read of an Action request's events that skips a sequence the subscription has not
// recorded.
var ErrActionHistoryGap = errors.New("action event history has a gap")

// actionsReadInterval is the wait before an Actions subscription is read again after a read that succeeded.
const actionsReadInterval = 5 * time.Second

// RecordActionEvents records what a read of the Action request an Actions subscription follows returned, in the
// claimed inbox and in one fenced transaction. Events at or before the sequence the subscription has recorded
// were recorded already and are skipped; the first of the others must follow that sequence directly, or nothing
// is recorded and the read fails with ErrActionHistoryGap.
//
// A subscription that was cancelled, expired or renewed since it was read as due (its version moved) is left
// as it is. Otherwise the read counts as a success: the failure shown on the subscription clears, and the
// request is read again in five seconds.
func (s *Store) RecordActionEvents(ctx context.Context, claim Claim, due DueSubscription, events []storagecodec.ActionEvent) error {
	source, ok := due.Source.(model.ActionsSource)
	if !ok {
		return fmt.Errorf("subscription %s does not follow an Action request", due.ID)
	}
	return s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
		recorded := now()
		var after sql.NullInt64
		err := tx.QueryRowContext(ctx, `
			UPDATE subscription SET next_attempt = $3, last_success_at = $4,
				error = NULL, error_kind = NULL, error_since = NULL, error_observed_at = NULL
			WHERE id = $1 AND inbox_id = $2 AND NOT cancelled AND version = $5 AND expires_at > $4
			RETURNING actions_after_sequence`,
			due.ID, held.ID, recorded.Add(actionsReadInterval), recorded, due.Version).Scan(&after)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if !after.Valid {
			return fmt.Errorf("subscription %s follows an Action request but has no recorded sequence", due.ID)
		}
		sequence := after.Int64
		for _, event := range events {
			if event.Sequence <= sequence {
				continue
			}
			if event.Sequence != sequence+1 {
				return ErrActionHistoryGap
			}
			payload, err := json.Marshal(event)
			if err != nil {
				return err
			}
			identity := model.ActionsEvent{RequestID: source.RequestID, Sequence: event.Sequence}
			if err := s.AppendEvent(ctx, tx, held.ID, due.ID, identity, payload); err != nil {
				return err
			}
			sequence = event.Sequence
		}
		if sequence != after.Int64 {
			if _, err := tx.ExecContext(ctx, `UPDATE subscription SET actions_after_sequence = $2 WHERE id = $1`,
				due.ID, sequence); err != nil {
				return err
			}
		}
		if len(events) > 0 {
			if _, err := tx.ExecContext(ctx, `UPDATE inbox SET updated_at = $2 WHERE id = $1`, held.ID, recorded); err != nil {
				return err
			}
		}
		return notify(ctx, tx)
	})
}
