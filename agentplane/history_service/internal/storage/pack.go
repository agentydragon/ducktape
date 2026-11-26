package storage

import (
	"context"
	"database/sql"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

// sealVersionedTail runs under ingress's session lock. Inspect only lengths until
// the next block is ready. Existing blocks are immutable barriers, including
// when a writer is enabled again after a period of ordinary writes.
func sealVersionedTail(ctx context.Context, tx *sql.Tx, id uuid.UUID, kind historypb.EventKind, p *eventblock.Packer) error {
	for {
		rows, err := tx.QueryContext(ctx, `SELECT cursor,octet_length(payload),pg_column_size(payload) FROM session_event
			WHERE session_id=$1 AND kind=$2 AND compactable AND cursor>COALESCE(
			(SELECT last_cursor FROM session_event_block WHERE session_id=$1 AND kind=$2 ORDER BY last_cursor DESC LIMIT 1),0)
			ORDER BY cursor LIMIT $3`, id, kind, eventblock.MaxEntries)
		if err != nil {
			return err
		}
		var cursors []int64
		var sizes, stored []int
		for rows.Next() {
			var cursor int64
			var size, onDisk int
			if err := rows.Scan(&cursor, &size, &onDisk); err != nil {
				rows.Close()
				return err
			}
			cursors = append(cursors, cursor)
			sizes = append(sizes, size)
			stored = append(stored, onDisk)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			return err
		}
		cut := p.NextSizes(sizes, false)
		if cut.Decision == eventblock.NeedMore {
			return nil
		}
		cursors = cursors[:cut.N]
		sizes = sizes[:cut.N]
		if cut.Decision == eventblock.Seal {
			rows, err := tx.QueryContext(ctx, `SELECT payload FROM session_event WHERE session_id=$1 AND cursor=ANY($2) ORDER BY cursor`, id, pq.Array(cursors))
			if err != nil {
				return err
			}
			var frames [][]byte
			for rows.Next() {
				var f []byte
				if err := rows.Scan(&f); err != nil {
					rows.Close()
					return err
				}
				frames = append(frames, f)
			}
			rows.Close()
			if err := rows.Err(); err != nil {
				return err
			}
			if len(frames) != cut.N {
				return fmt.Errorf("event tail changed during seal")
			}
			b, err := p.Encode(frames)
			if err != nil {
				return err
			}
			var oldBytes int
			for _, v := range stored[:cut.N] {
				oldBytes += v
			}
			// Account for both bigint index arrays plus a conservative fixed
			// allowance for the row, array headers and its B-tree entries.
			if len(b)+16*cut.N+160 < oldBytes {
				if _, err := tx.ExecContext(ctx, `INSERT INTO session_event_block(session_id,first_cursor,last_cursor,kind,cursors,sizes,payload)
					VALUES($1,$2,$3,$4,$5,$6,$7)`, id, cursors[0], cursors[cut.N-1], kind, pq.Array(cursors), pq.Array(sizes), b); err != nil {
					return err
				}
				if _, err := tx.ExecContext(ctx, `DELETE FROM session_event WHERE session_id=$1 AND cursor=ANY($2)`, id, pq.Array(cursors)); err != nil {
					return err
				}
				continue
			}
		}
		// Leave incompressible records ordinary. Removing them from the
		// partial index also prevents re-encoding them at every append.
		if _, err := tx.ExecContext(ctx, `UPDATE session_event SET compactable=false WHERE session_id=$1 AND cursor=ANY($2)`, id, pq.Array(cursors)); err != nil {
			return err
		}
	}
}
