package inbox

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/google/uuid"
)

// ScheduleSourceTx changes a subscription deadline while its inbox is locked.
func (s *Store) ScheduleSourceTx(ctx context.Context, tx transaction.Tx, subscriptionID uuid.UUID, next *time.Time) error {
	_, err := tx.ExecContext(ctx, `UPDATE subscription SET next_attempt = $2 WHERE id = $1`, subscriptionID, next)
	return err
}

// SourceSucceededTx records successful source reconciliation while its inbox is locked.
func (s *Store) SourceSucceededTx(ctx context.Context, tx transaction.Tx, subscriptionID uuid.UUID, next *time.Time, at time.Time) error {
	_, err := tx.ExecContext(ctx, `
		UPDATE subscription SET next_attempt = $2, last_success_at = $3, error_kind = NULL, error = NULL,
			error_since = NULL, error_observed_at = NULL WHERE id = $1`, subscriptionID, next, at)
	return err
}

func (s *Store) TouchInboxTx(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID, at time.Time) error {
	_, err := tx.ExecContext(ctx, `UPDATE inbox SET updated_at = $2 WHERE id = $1`, inboxID, at)
	return err
}
