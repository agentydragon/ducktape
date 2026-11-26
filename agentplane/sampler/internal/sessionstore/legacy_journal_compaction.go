package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/lib/pq"
)

const (
	journalWindowSize     = eventcodec.MaxBlockEntries
	journalCompactionPace = 50 * time.Millisecond
)

var errJournalWindowChanged = errors.New("standalone journal window changed during packing")

// nextJournalWindow finds the next legacy 32-cursor window with standalone rows.
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

// compactJournalWindow retains the existing background worker's legacy gzip
// writer. It prepares and verifies frame bytes before the short Session lock.
func (s *Store) compactJournalWindow(ctx context.Context, sessionID string, first, last uint64) error {
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	blocks, err := prepareJournalBlocks(ctx, s.db, sessionID, first, last)
	if err != nil || len(blocks) == 0 {
		return err
	}
	return s.commitJournalBlocks(ctx, sessionID, blocks)
}

type sealedJournalBlock struct {
	first, last uint64
	payload     []byte
	versions    []string
}

func prepareJournalBlocks(ctx context.Context, q journalQueryer, sessionID string, first, last uint64) ([]sealedJournalBlock, error) {
	rows, err := q.QueryContext(ctx, `
		SELECT sequence, xmin::text, CASE WHEN octet_length(entry) <= $4 THEN entry END FROM sampler_event
		WHERE session_id=$1 AND sequence BETWEEN $2 AND $3 ORDER BY sequence`, sessionID, first, last, eventcodec.MaxBlockBytes)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var blocks []sealedJournalBlock
	var frames [][]byte
	var versions []string
	var start uint64
	var rawSize, storedSize int
	flush := func() error {
		if len(frames) > 1 {
			payload, err := eventcodec.PackBlock(frames)
			if err != nil {
				return err
			}
			if len(payload) < storedSize {
				verified, err := eventcodec.UnpackBlock(payload, len(frames))
				if err != nil {
					return err
				}
				if !slices.EqualFunc(frames, verified, bytes.Equal) {
					return fmt.Errorf("packed frames differ from originals at cursor %d", start)
				}
				blocks = append(blocks, sealedJournalBlock{
					first: start, last: start + uint64(len(frames)) - 1,
					payload: payload, versions: versions,
				})
			}
		}
		frames, versions, rawSize, storedSize = nil, nil, 0, 0
		return nil
	}
	for rows.Next() {
		var sequence uint64
		var version string
		var stored []byte
		if err := rows.Scan(&sequence, &version, &stored); err != nil {
			return nil, err
		}
		frame, err := eventcodec.UnpackBlockEntry(stored)
		if err != nil {
			return nil, fmt.Errorf("prepare event at cursor %d: %w", sequence, err)
		}
		size := len(frame) + eventcodec.FrameHeaderBytes
		if len(frame) == 0 || rawSize+size > eventcodec.MaxBlockBytes || sequence != start+uint64(len(frames)) {
			if err := flush(); err != nil {
				return nil, err
			}
		}
		if len(frame) == 0 {
			continue
		}
		if len(frames) == 0 {
			start = sequence
		}
		frames = append(frames, frame)
		versions = append(versions, version)
		rawSize += size
		storedSize += len(stored)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if err := flush(); err != nil {
		return nil, err
	}
	return blocks, nil
}

func (s *Store) commitJournalBlocks(ctx context.Context, sessionID string, blocks []sealedJournalBlock) error {
	return postgres.NamedTransaction(ctx, s.db, sql.TxOptions{}, "sampler.commit_journal_blocks", func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `SET LOCAL lock_timeout='1s'`); err != nil {
			return err
		}
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
	var verified bool
	err = tx.QueryRowContext(ctx, `
		INSERT INTO sampler_event_block(session_id,first_sequence,last_sequence,payload)
		SELECT $1::varchar,$2::integer,$3::integer,$5::bytea WHERE NOT EXISTS (
			SELECT 1 FROM sampler_event_block
			WHERE `+overlappingJournalBlocks+`
		)
		RETURNING first_sequence=$2 AND last_sequence=$3 AND payload=$5`,
		sessionID, block.first, block.last, earliestJournalBlockStart(block.first), block.payload).Scan(&verified)
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
