package storage

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

// CompactionResult counts original entries and serialized bytes, excluding rows and indexes.
// Skipped counts eligible ordinary entries inside existing blocks' cursor spans. These stay ordinary
// because creating a nested block would violate the per-kind non-overlapping seek index.
type CompactionResult struct {
	Entries, Blocks, UncompressedBytes, PackedBytes, Skipped int64
}

// CompactHistory packs eligible ordinary entries up to the opening watermark. Every apply commits
// one bounded block under the same Session lock as ingress. No event, cursor or projection changes,
// so readers need no correction and the projector needs no write lease. Reruns retain existing blocks.
func (s *Store) CompactHistory(ctx context.Context, sessionID uuid.UUID, apply bool) (CompactionResult, error) {
	var result CompactionResult
	var through uint64
	if err := s.db.QueryRowContext(ctx, "SELECT last_cursor FROM session_history WHERE id=$1", sessionID).Scan(&through); err != nil {
		return result, err
	}
	for _, kind := range compactKinds {
		for after := uint64(0); after < through; {
			batchCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
			batch, next, err := s.compactHistoryBatch(batchCtx, sessionID, kind, after, through, apply)
			cancel()
			if err != nil {
				return result, err
			}
			after = next
			result.Entries += batch.Entries
			result.Blocks += batch.Blocks
			result.UncompressedBytes += batch.UncompressedBytes
			result.PackedBytes += batch.PackedBytes
			result.Skipped += batch.Skipped
			if batch.Blocks > 0 && result.Blocks%100 == 0 {
				slog.InfoContext(ctx, "historical compaction progress", "session_id", sessionID, "apply", apply,
					"events", result.Entries, "blocks", result.Blocks, "uncompressed_bytes", result.UncompressedBytes,
					"packed_bytes", result.PackedBytes)
			}
			if apply && batch.Blocks > 0 {
				// Pace WAL generation and yield the Session lock between blocks.
				timer := time.NewTimer(50 * time.Millisecond)
				select {
				case <-ctx.Done():
					timer.Stop()
					return result, ctx.Err()
				case <-timer.C:
				}
			}
		}
	}
	return result, nil
}

func (s *Store) compactHistoryBatch(ctx context.Context, sessionID uuid.UUID, kind historypb.EventKind, after, through uint64, apply bool) (CompactionResult, uint64, error) {
	var result CompactionResult
	next := through
	options := sql.TxOptions{ReadOnly: !apply}
	if !apply {
		options.Isolation = sql.LevelRepeatableRead
	}
	err := postgres.Transaction(ctx, s.db, options, func(tx *sql.Tx) error {
		if apply {
			if _, err := tx.ExecContext(ctx, "SET LOCAL lock_timeout='1s'"); err != nil {
				return err
			}
			if _, err := lockRow(ctx, tx, sessionID); err != nil {
				return err
			}
		}
		var first uint64
		err := tx.QueryRowContext(ctx, `SELECT cursor FROM session_event
			WHERE session_id=$1 AND kind=$2 AND cursor>$3 AND cursor<=$4 AND octet_length(payload)<=$5
			ORDER BY cursor LIMIT 1`, sessionID, kind, after, through, blockBytes/2).Scan(&first)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		// Existing blocks are barriers: historical and live packing must never create overlapping
		// cursor spans for one kind. Ordinary entries can coexist inside a block after a flag toggle.
		upper := through
		var blockFirst, blockLast uint64
		err = tx.QueryRowContext(ctx, `SELECT first_cursor,last_cursor FROM session_event_block
			WHERE session_id=$1 AND kind=$2 AND last_cursor>=$3 ORDER BY last_cursor LIMIT 1`,
			sessionID, kind, first).Scan(&blockFirst, &blockLast)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if err == nil {
			if blockFirst <= first {
				next = min(through, blockLast)
				return tx.QueryRowContext(ctx, `SELECT count(*) FROM session_event
					WHERE session_id=$1 AND kind=$2 AND cursor>=$3 AND cursor<=$4 AND octet_length(payload)<=$5`,
					sessionID, kind, first, next, blockBytes/2).Scan(&result.Skipped)
			}
			upper = min(through, blockFirst-1)
		}
		rows, err := tx.QueryContext(ctx, `SELECT cursor,octet_length(payload) FROM session_event
			WHERE session_id=$1 AND kind=$2 AND cursor>=$3 AND cursor<=$4 AND octet_length(payload)<=$5
			ORDER BY cursor LIMIT $6`, sessionID, kind, first, upper, blockBytes/2, blockEntries)
		if err != nil {
			return err
		}
		var references []eventReference
		var size int
		for rows.Next() {
			reference := eventReference{kind: kind}
			if err := rows.Scan(&reference.cursor, &reference.size); err != nil {
				rows.Close()
				return err
			}
			if size+reference.size > blockBytes {
				break
			}
			references, size = append(references, reference), size+reference.size
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()
		if len(references) < blockMinimum && (size < blockBytes/2 || len(references) < 2) {
			next = upper
			return nil
		}
		entries, err := loadEntries(ctx, tx, sessionID, references)
		if err != nil {
			return err
		}
		var packed int
		if apply {
			if packed, err = insertBlock(ctx, tx, sessionID, entries); err != nil {
				return err
			}
			cursors := make([]int64, len(entries))
			for index, entry := range entries {
				cursors[index] = int64(entry.Cursor)
			}
			if _, err := tx.ExecContext(ctx, "DELETE FROM session_event WHERE session_id=$1 AND cursor=ANY($2)", sessionID, pq.Array(cursors)); err != nil {
				return err
			}
			// Read every moved entry through the production block reader before committing.
			// A codec, index or content mismatch rolls back both the insertion and deletion.
			blockFirst := references[0].cursor
			for index := range references {
				references[index].block = &blockFirst
			}
			restored, err := loadEntries(ctx, tx, sessionID, references)
			if err != nil {
				return err
			}
			for index, entry := range entries {
				if !proto.Equal(entry, restored[index]) {
					return fmt.Errorf("packed event %d differs from its original", entry.Cursor)
				}
			}
		} else {
			payload, err := encodeBlock(entries)
			if err != nil {
				return err
			}
			packed = len(payload)
		}
		result.Entries, result.Blocks = int64(len(entries)), 1
		result.UncompressedBytes, result.PackedBytes = int64(size), int64(packed)
		next = references[len(references)-1].cursor
		return nil
	})
	return result, next, err
}
