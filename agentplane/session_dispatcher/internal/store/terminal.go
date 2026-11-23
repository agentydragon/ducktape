package store

import (
	"context"
	"database/sql"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"github.com/google/uuid"
)

// EndSandbox settles delivery for an exact Sandbox UID. Retained Sessions own
// their original commands and receipts. The UID fence prevents further delivery.
func (s *Store) EndSandbox(ctx context.Context, sandboxUID string) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "sandbox:"+sandboxUID); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, "INSERT INTO deleted_sandbox (sandbox_uid) VALUES ($1) ON CONFLICT DO NOTHING", sandboxUID); err != nil {
			return err
		}
		// Lock every Session lease before touching delivery rows. Inserting an
		// expired placeholder also serializes acquisition of a missing lease row. The
		// exclusive Sandbox advisory lock above prevents new admission and worker
		// writes until cleanup commits.
		if _, err := tx.ExecContext(ctx, `INSERT INTO session_delivery_lease (session_id,holder,expires_at)
			SELECT session_id,$2,'-infinity'::timestamptz FROM session WHERE sandbox_uid=$1
			ORDER BY session_id ON CONFLICT (session_id) DO UPDATE SET session_id=EXCLUDED.session_id`, sandboxUID, uuid.Nil); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `WITH settled AS (UPDATE dispatch SET
			state_value=CASE WHEN delivery_attempted OR state_value=$4 THEN $5::integer ELSE $6::integer END,
			reason='sandbox_deleted',updated_at=now()
			WHERE sandbox_uid=$1 AND (state_value IN ($2,$3) OR (state_value=$4 AND operation_value=$8))
			RETURNING session_id)
			SELECT pg_notify($7, session_id) FROM (SELECT DISTINCT session_id FROM settled) s`,
			sandboxUID, QueueState, DeliveringState, AdmittedState, UnknownState, FailedState, NotifyChannel, commandpb.Operation_OPERATION_SUBMIT_INPUT); err != nil {
			return err
		}
		// Older schemas allowed standalone dispatches. Preserve their existing cleanup.
		if _, err := tx.ExecContext(ctx, `DELETE FROM dispatch d WHERE d.sandbox_uid=$1 AND NOT EXISTS
			(SELECT FROM session s WHERE (s.session_id,s.sandbox_name,s.sandbox_uid) =
				(d.session_id,d.sandbox_name,d.sandbox_uid))`, sandboxUID); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE session SET
			environment_deleted_at=COALESCE(environment_deleted_at, now()),
			state_value=CASE state_value WHEN $2 THEN $3 WHEN $4 THEN $5 ELSE state_value END,
			reason=CASE WHEN state_value IN ($2,$4) THEN 'sandbox_deleted' ELSE reason END,
			updated_at=CASE WHEN environment_deleted_at IS NULL THEN now() ELSE updated_at END
			WHERE sandbox_uid=$1 AND environment_deleted_at IS NULL`,
			sandboxUID, SessionWaitingState, SessionFailedState, SessionInProgressState, SessionUnknownState); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `DELETE FROM session_delivery_lease WHERE session_id IN
			(SELECT session_id FROM session WHERE sandbox_uid=$1)`, sandboxUID); err != nil {
			return err
		}
		return nil
	})
}

func (s *Store) ListSandboxUIDs(ctx context.Context) (map[string]struct{}, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT sandbox_uid::text FROM dispatch d
		WHERE NOT EXISTS (SELECT FROM deleted_sandbox e WHERE e.sandbox_uid=d.sandbox_uid)
		UNION SELECT sandbox_uid::text FROM session WHERE environment_deleted_at IS NULL`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]struct{}{}
	for rows.Next() {
		var uid string
		if err := rows.Scan(&uid); err != nil {
			return nil, err
		}
		result[uid] = struct{}{}
	}
	return result, rows.Err()
}
