package store

import (
	"context"
	"database/sql"
	"log/slog"
	"time"

	"github.com/lib/pq"
)

const (
	DispatchTombstoneRetention = 24 * time.Hour
	tombstonePruneInterval     = time.Minute
	tombstonePruneBatch        = 1000
)

// PruneDispatchTombstones expires one bounded batch and advances the durable coverage floor
// in the same transaction. Concurrent replica pruners claim disjoint rows.
func (s *Store) PruneDispatchTombstones(ctx context.Context) (int64, error) {
	var count int64
	var sessions []string
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := tx.QueryRowContext(ctx, `WITH expired AS (
			SELECT command_id,version FROM dispatch_tombstone
			WHERE deleted_at < statement_timestamp() - $1 * interval '1 second'
			ORDER BY deleted_at,version LIMIT $2 FOR UPDATE SKIP LOCKED
		), removed AS (
			DELETE FROM dispatch_tombstone t USING expired e
			WHERE t.command_id=e.command_id AND t.version=e.version RETURNING t.version,t.session_id
		), coverage AS (
			UPDATE dispatch_watch_retention
			SET pruned_through=GREATEST(pruned_through,COALESCE((SELECT max(version) FROM removed),0))
			WHERE singleton RETURNING pruned_through
		)
		SELECT (SELECT count(*) FROM removed),ARRAY(SELECT DISTINCT session_id FROM removed) FROM coverage`,
			DispatchTombstoneRetention.Seconds(), tombstonePruneBatch).Scan(&count, pq.Array(&sessions)); err != nil {
			return err
		}
		for _, id := range sessions {
			if err := notify(ctx, tx, id); err != nil {
				return err
			}
		}
		return nil
	})
	return count, err
}

// RunTombstonePruner retains the coverage floor across replica restarts. A storage outage
// delays expiry; it cannot erase the evidence needed to reject an expired resume cursor.
func (s *Store) RunTombstonePruner(ctx context.Context) error {
	ticker := time.NewTicker(tombstonePruneInterval)
	defer ticker.Stop()
	for {
		for {
			count, err := s.PruneDispatchTombstones(ctx)
			if err != nil {
				if ctx.Err() != nil {
					return ctx.Err()
				}
				slog.WarnContext(ctx, "Dispatcher tombstone pruning unavailable; retrying", "error", err)
				break
			}
			if count < tombstonePruneBatch {
				break
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}
