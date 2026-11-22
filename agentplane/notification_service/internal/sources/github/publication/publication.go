// Package publication publishes committed GitHub source changes to inbox workers.
package publication

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/wakeup"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/google/uuid"
)

type Adapter struct{}

func (Adapter) Publish(ctx context.Context, tx transaction.Tx, appID int64) error {
	if err := wakeApp(ctx, tx, appID); err != nil {
		return err
	}
	return postgres.Notify(ctx, tx, wakeup.WorkChannel, "")
}

func wakeApp(ctx context.Context, tx transaction.Tx, appID int64) error {
	rows, err := tx.QueryContext(ctx, `
		SELECT id FROM inbox
		WHERE NOT retired AND id IN (
			SELECT inbox_id FROM subscription WHERE github_app_id = $1 AND NOT cancelled AND expires_at > now())
		ORDER BY id FOR UPDATE`, appID)
	if err != nil {
		return err
	}
	var inboxes []string
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		inboxes = append(inboxes, id.String())
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return err
	}
	if len(inboxes) == 0 {
		return nil
	}
	ids := "{" + strings.Join(inboxes, ",") + "}"
	at := now()
	// A failure's backoff stands; otherwise the source is due at once.
	if _, err := tx.ExecContext(ctx, `
		UPDATE subscription SET generation = generation + 1,
			next_attempt = COALESCE(CASE WHEN error IS NOT NULL THEN next_attempt END, $3::timestamptz)
		WHERE github_app_id = $1 AND NOT cancelled AND expires_at > now() AND inbox_id = ANY($2::uuid[])`,
		appID, ids, at); err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE inbox SET next_attempt = $2
		WHERE id = ANY($1::uuid[]) AND (delivery_error IS NULL OR next_attempt IS NULL)`, ids, at)
	return err
}

func now() time.Time { return time.Now().UTC().Truncate(time.Microsecond) }
