package sessionstore

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/lib/pq"
)

// JournalCompactionResult counts the rows that would move in a dry run or committed
// replacements during apply. Byte counts cover the stored payload columns.
type JournalCompactionResult struct {
	Events, Blocks, StoredBytes, PackedBytes int64
}

// ListJournalSessions lists session IDs in keyset order. An empty after starts at the first session.
func (s *Store) ListJournalSessions(ctx context.Context, after string, limit int) ([]string, error) {
	if limit <= 0 || limit > 4096 {
		return nil, protocol.ErrInvalid
	}
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	rows, err := s.db.QueryContext(ctx, `SELECT id FROM sampler_session WHERE id>$1 ORDER BY id LIMIT $2`, after, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// CompactJournal packs completed windows up to the opening cursor, releasing the
// session lock between windows. Rerunning can continue after interruption.
func (s *Store) CompactJournal(ctx context.Context, sessionID string, apply bool) (JournalCompactionResult, error) {
	var result JournalCompactionResult
	cursor, err := s.CurrentCursor(ctx, sessionID)
	if err != nil {
		return result, err
	}
	through := cursor / journalWindowSize * journalWindowSize
	lastProgress := time.Now()
	for after := uint64(0); after < through; {
		last, err := s.nextJournalWindow(ctx, sessionID, after, through)
		if err != nil {
			return result, err
		}
		if last == 0 {
			break
		}
		batch, err := s.compactJournalWindow(ctx, sessionID, last-journalWindowSize+1, last, apply)
		if errors.Is(err, errJournalWindowChanged) {
			continue
		}
		if err != nil {
			return result, fmt.Errorf("compact cursors %d–%d: %w", last-journalWindowSize+1, last, err)
		}
		after = last
		result.Events += batch.Events
		result.Blocks += batch.Blocks
		result.StoredBytes += batch.StoredBytes
		result.PackedBytes += batch.PackedBytes
		if now := time.Now(); now.Sub(lastProgress) >= 30*time.Second {
			slog.InfoContext(ctx, "sampler journal compaction progress", "session_id", sessionID, "apply", apply,
				"cursor", after, "through", through, "events", result.Events, "blocks", result.Blocks,
				"stored_bytes", result.StoredBytes, "packed_bytes", result.PackedBytes)
			lastProgress = now
		}
		if apply && batch.Blocks > 0 {
			// Give live appenders a chance to acquire the lock and pace WAL generation.
			if err := waitForJournalCompaction(ctx, journalCompactionPace); err != nil {
				return result, err
			}
		}
	}
	return result, nil
}

const (
	journalWindowSize     = eventcodec.MaxBlockEntries
	journalCompactionPace = 50 * time.Millisecond
)

func waitForJournalCompaction(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// Seek the first standalone row through the opening cursor, skipping fully packed windows.
func (s *Store) nextJournalWindow(ctx context.Context, sessionID string, after, through uint64) (uint64, error) {
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	var first uint64
	err := s.db.QueryRowContext(ctx, `SELECT sequence FROM sampler_event
		WHERE session_id=$1 AND sequence>$2 AND sequence<=$3 ORDER BY sequence LIMIT 1`,
		sessionID, after, through).Scan(&first)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	return ((first-1)/journalWindowSize + 1) * journalWindowSize, nil
}

var errJournalWindowChanged = errors.New("standalone journal window changed during packing")

func (s *Store) compactJournalWindow(ctx context.Context, sessionID string, first, last uint64, apply bool) (JournalCompactionResult, error) {
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	var result JournalCompactionResult
	blocks, err := prepareJournalBlocks(ctx, s.db, sessionID, first, last)
	if err != nil {
		return result, err
	}
	if apply && len(blocks) > 0 {
		if err := s.commitJournalBlocks(ctx, sessionID, blocks); err != nil {
			return result, err
		}
	}
	for _, block := range blocks {
		result.Events += int64(block.last - block.first + 1)
		result.Blocks++
		result.StoredBytes += int64(block.storedBytes)
		result.PackedBytes += int64(len(block.payload))
	}
	return result, nil
}

func (s *Store) commitJournalBlocks(ctx context.Context, sessionID string, blocks []sealedJournalBlock) error {
	return postgres.Transaction(ctx, s.db, sql.TxOptions{}, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `SET LOCAL lock_timeout='1s'`); err != nil {
			return err
		}
		// Old inline writers, appenders and compactors all serialize on this row.
		// The packed window was already committed before we read it.
		var id string
		err := tx.QueryRowContext(ctx, `SELECT id FROM sampler_session WHERE id=$1 FOR UPDATE`, sessionID).Scan(&id)
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrMissing
		}
		if err != nil {
			return err
		}
		for _, block := range blocks {
			if err := replaceJournalBlock(ctx, tx, sessionID, block); err != nil {
				return err
			}
		}
		return nil
	})
}

func replaceJournalBlock(ctx context.Context, tx *sql.Tx, sessionID string, block sealedJournalBlock) error {
	// Match the source row versions captured by the original read. A competing
	// compactor may have removed them while we were compressing. Roll back the
	// entire window in that case so the caller can take a fresh snapshot.
	deleted, err := tx.ExecContext(ctx, `
		DELETE FROM sampler_event AS event
		USING unnest($4::text[]) WITH ORDINALITY AS source(version,position)
		WHERE event.session_id=$1 AND event.sequence BETWEEN $2 AND $3
		  AND event.sequence=$2+source.position-1 AND event.xmin=source.version::xid`,
		sessionID, block.first, block.last, pq.Array(block.versions))
	if err != nil {
		return err
	}
	count, err := deleted.RowsAffected()
	if err != nil {
		return err
	}
	if count != int64(len(block.versions)) {
		return errJournalWindowChanged
	}
	// In a valid journal the deleted rows cannot overlap any block, including
	// one starting before this window.
	oldestBlock := max(block.first, journalWindowSize) - journalWindowSize + 1
	var verified bool
	err = tx.QueryRowContext(ctx, `
		INSERT INTO sampler_event_block(session_id,first_sequence,last_sequence,payload)
		SELECT $1::varchar,$2::integer,$3::integer,$4::bytea WHERE NOT EXISTS (
			SELECT 1 FROM sampler_event_block
			WHERE session_id=$1 AND first_sequence BETWEEN $5 AND $3 AND last_sequence>=$2
		)
		RETURNING first_sequence=$2 AND last_sequence=$3 AND payload=$4`,
		sessionID, block.first, block.last, block.payload, oldestBlock).Scan(&verified)
	if errors.Is(err, sql.ErrNoRows) {
		return fmt.Errorf("existing journal block overlaps cursors %d–%d", block.first, block.last)
	}
	if err != nil {
		return err
	}
	if !verified {
		return fmt.Errorf("stored journal block differs from prepared cursors %d–%d", block.first, block.last)
	}
	return nil
}
