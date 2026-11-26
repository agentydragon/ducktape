package storage

import (
	"context"
	"database/sql"
	"errors"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

type rawFrame struct {
	cursor  uint64
	kind    historypb.EventKind
	size    int
	payload []byte
}

type storedBlock struct {
	first, last uint64
	kind        historypb.EventKind
	cursors     []int64
	sizes       []int64
	payload     []byte
	frames      []rawFrame
	storedBytes int64
}

func scanStoredBlock(scanner interface{ Scan(...any) error }) (storedBlock, error) {
	var block storedBlock
	var cursors, sizes pq.Int64Array
	if err := scanner.Scan(&block.first, &block.last, &block.kind, &cursors, &sizes, &block.payload, &block.storedBytes); err != nil {
		return storedBlock{}, err
	}
	block.cursors, block.sizes = slices.Clone(cursors), slices.Clone(sizes)
	frames, err := decodeStoredBlock(block.payload)
	if err != nil {
		return storedBlock{}, err
	}
	if !validStoredBlockIndex(block, frames) {
		return storedBlock{}, errors.New("event block cursor index disagrees with payload")
	}
	block.frames = frames
	return block, nil
}

func validStoredBlockIndex(block storedBlock, frames []rawFrame) bool {
	if block.first == 0 || block.last < block.first || block.kind == 0 ||
		len(historypb.EventKind_name[int32(block.kind)]) == 0 ||
		len(block.cursors) != len(frames) || len(block.sizes) != len(frames) ||
		len(frames) == 0 || block.first != frames[0].cursor || block.last != frames[len(frames)-1].cursor {
		return false
	}
	for i, frame := range frames {
		if frame.cursor != uint64(block.cursors[i]) || frame.kind != block.kind ||
			block.sizes[i] != int64(frame.size) {
			return false
		}
	}
	return true
}

func decodeStoredBlock(payload []byte) ([]rawFrame, error) {
	entries, wire, err := decodeBlock(payload)
	if err != nil {
		return nil, err
	}
	frames := make([]rawFrame, len(entries))
	for i, entry := range entries {
		frames[i] = rawFrame{cursor: entry.Cursor, kind: historypb.EventKind(observationNumber(entry)), size: proto.Size(entry), payload: wire[i]}
	}
	return frames, nil
}

func insertRawFrame(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, frame rawFrame) error {
	_, err := tx.ExecContext(ctx, `INSERT INTO session_event(session_id,cursor,kind,payload,compactable)
		VALUES($1,$2,$3,$4,false)`, sessionID, int64(frame.cursor), frame.kind, frame.payload)
	return err
}

// InventoryEventBlocks validates each block and returns aggregate counts without
// exposing session identifiers or payloads. One repeatable-read snapshot lets a
// zero-legacy result be used as a reproducible cutover check while writers continue.
func (s *Store) InventoryEventBlocks(ctx context.Context) (BlockInventory, error) {
	var inventory BlockInventory
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		return inventory, err
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(ctx, `SELECT first_cursor,last_cursor,kind,cursors,sizes,payload,pg_column_size(payload)
		FROM session_event_block ORDER BY session_id,first_cursor`)
	if err != nil {
		return inventory, err
	}
	for rows.Next() {
		var block storedBlock
		var cursors, sizes pq.Int64Array
		if err := rows.Scan(&block.first, &block.last, &block.kind, &cursors, &sizes, &block.payload, &block.storedBytes); err != nil {
			rows.Close()
			return inventory, err
		}
		block.cursors, block.sizes = slices.Clone(cursors), slices.Clone(sizes)
		inventory.Blocks++
		inventory.StoredBytes += block.storedBytes
		frames, decodeErr := decodeStoredBlock(block.payload)
		valid := decodeErr == nil && validStoredBlockIndex(block, frames)
		if !valid {
			inventory.InvalidBlocks++
		} else if eventblock.IsBlock(block.payload) {
			inventory.VersionedBlocks++
		} else {
			inventory.LegacyBlocks++
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return inventory, err
	}
	if err := rows.Close(); err != nil {
		return inventory, err
	}
	if err := tx.QueryRowContext(ctx, `SELECT count(*),COALESCE(sum(pg_column_size(payload)),0) FROM session_event`).
		Scan(&inventory.OrdinaryRows, &inventory.OrdinaryStoredBytes); err != nil {
		return inventory, err
	}
	if err := tx.QueryRowContext(ctx, `SELECT count(*) FROM session_history`).Scan(&inventory.Sessions); err != nil {
		return inventory, err
	}
	return inventory, tx.Commit()
}

type BlockInventory struct {
	Sessions            int64 `json:"sessions"`
	OrdinaryRows        int64 `json:"ordinary_rows"`
	OrdinaryStoredBytes int64 `json:"ordinary_stored_bytes"`
	Blocks              int64 `json:"blocks"`
	VersionedBlocks     int64 `json:"versioned_blocks"`
	LegacyBlocks        int64 `json:"legacy_blocks"`
	InvalidBlocks       int64 `json:"invalid_blocks"`
	StoredBytes         int64 `json:"block_stored_bytes"`
}
