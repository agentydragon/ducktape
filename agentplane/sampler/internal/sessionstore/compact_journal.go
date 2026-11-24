package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
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
	const window = eventcodec.MaxBlockEntries
	through := cursor / window * window
	lastProgress := time.Now()
	for after := uint64(0); after < through; {
		// Seek the next standalone row via the primary key. Fully packed and empty
		// windows require no transaction, decompression, or session lock.
		searchCtx, cancel := context.WithTimeout(ctx, s.operationTimeout)
		var first uint64
		err := s.db.QueryRowContext(searchCtx, `SELECT sequence FROM sampler_event
			WHERE session_id=$1 AND sequence>$2 AND sequence<=$3 ORDER BY sequence LIMIT 1`,
			sessionID, after, through).Scan(&first)
		cancel()
		if errors.Is(err, sql.ErrNoRows) {
			break
		}
		if err != nil {
			return result, err
		}
		last := ((first-1)/window + 1) * window
		batch, err := s.compactJournalWindow(ctx, sessionID, last-window+1, last, apply)
		if err != nil {
			return result, fmt.Errorf("compact cursors %d–%d: %w", last-window+1, last, err)
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
			timer := time.NewTimer(50 * time.Millisecond)
			select {
			case <-ctx.Done():
				timer.Stop()
				return result, ctx.Err()
			case <-timer.C:
			}
		}
	}
	return result, nil
}

func (s *Store) compactJournalWindow(ctx context.Context, sessionID string, first, last uint64, apply bool) (JournalCompactionResult, error) {
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	var result JournalCompactionResult
	err := postgres.Transaction(ctx, s.db, sql.TxOptions{ReadOnly: !apply}, func(tx *sql.Tx) error {
		if apply {
			if _, err := tx.ExecContext(ctx, `SET LOCAL lock_timeout='1s'`); err != nil {
				return err
			}
			// Appenders take this same lock. Packing never changes the conversation or
			// requires its lock, ownership fence, or retained transcript.
			var id string
			err := tx.QueryRowContext(ctx, `SELECT id FROM sampler_session WHERE id=$1 FOR UPDATE`, sessionID).Scan(&id)
			if errors.Is(err, sql.ErrNoRows) {
				return protocol.ErrMissing
			}
			if err != nil {
				return err
			}
		}
		blocks, err := prepareJournalBlocks(ctx, tx, sessionID, first, last)
		if err != nil {
			return err
		}
		if apply {
			if err := replaceJournalBlocks(ctx, tx, sessionID, blocks, true); err != nil {
				return err
			}
		}
		for _, block := range blocks {
			result.Events += int64(block.last - block.first + 1)
			result.Blocks++
			result.StoredBytes += int64(block.storedBytes)
			result.PackedBytes += int64(len(block.payload))
		}
		return nil
	})
	return result, err
}

func insertVerifiedJournalBlock(ctx context.Context, tx *sql.Tx, sessionID string, block sealedJournalBlock) error {
	// Blocks in a valid journal occupy gaps in standalone storage. Fail if an
	// overlapping block already exists, including one starting before the window.
	oldestBlock := max(block.first, eventcodec.MaxBlockEntries) - eventcodec.MaxBlockEntries + 1
	var overlap bool
	if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_event_block
		WHERE session_id=$1 AND first_sequence BETWEEN $4 AND $3 AND last_sequence>=$2)`,
		sessionID, block.first, block.last, oldestBlock).Scan(&overlap); err != nil {
		return err
	}
	if overlap {
		return fmt.Errorf("existing journal block overlaps cursors %d–%d", block.first, block.last)
	}
	var first, last uint64
	var payload []byte
	if err := tx.QueryRowContext(ctx, insertJournalBlock+` RETURNING first_sequence,last_sequence,payload`,
		sessionID, block.first, block.last, block.payload).Scan(&first, &last, &payload); err != nil {
		return err
	}
	if first != block.first || last != block.last {
		return fmt.Errorf("stored journal block interval %d–%d differs from %d–%d", first, last, block.first, block.last)
	}
	frames, err := eventcodec.UnpackBlock(payload, int(last-first+1))
	if err != nil {
		return err
	}
	rows, err := tx.QueryContext(ctx, `SELECT sequence,entry FROM sampler_event
		WHERE session_id=$1 AND sequence BETWEEN $2 AND $3 ORDER BY sequence`, sessionID, first, last)
	if err != nil {
		return err
	}
	defer rows.Close()
	next := first
	for rows.Next() {
		var sequence uint64
		var stored []byte
		if err := rows.Scan(&sequence, &stored); err != nil {
			return err
		}
		raw, err := eventcodec.UnpackBlockEntry(stored)
		if err != nil {
			return err
		}
		if sequence != next || sequence > last || !bytes.Equal(raw, frames[sequence-first]) {
			return fmt.Errorf("packed frame at cursor %d differs from its original", sequence)
		}
		next++
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if next != last+1 {
		return fmt.Errorf("journal block verification stopped before cursor %d", last)
	}
	return nil
}
