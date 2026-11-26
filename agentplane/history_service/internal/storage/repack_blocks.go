package storage

import (
	"bytes"
	"compress/gzip"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

const legacyDecodedLimit = blockBytes + 6*blockEntries

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

// decodeStoredBlock returns the original serialized EventEntry frames. The legacy
// container is parsed with protowire so repacking never re-marshals event payloads.
func decodeStoredBlock(payload []byte) ([]rawFrame, error) {
	var wireFrames [][]byte
	if eventblock.IsBlock(payload) {
		frames, err := eventblock.Decode(payload)
		if err != nil {
			return nil, fmt.Errorf("versioned event block: %w", err)
		}
		wireFrames = frames
	} else {
		reader, err := gzip.NewReader(bytes.NewReader(payload))
		if err != nil {
			return nil, fmt.Errorf("legacy event block gzip: %w", err)
		}
		raw, readErr := io.ReadAll(io.LimitReader(reader, legacyDecodedLimit+1))
		closeErr := reader.Close()
		if readErr != nil {
			return nil, fmt.Errorf("legacy event block read: %w", readErr)
		}
		if closeErr != nil {
			return nil, fmt.Errorf("legacy event block close: %w", closeErr)
		}
		if len(raw) > legacyDecodedLimit {
			return nil, errors.New("legacy event block exceeds decoded size limit")
		}
		for len(raw) > 0 {
			number, typ, n := protowire.ConsumeTag(raw)
			if n < 0 {
				return nil, protowire.ParseError(n)
			}
			raw = raw[n:]
			if number == 1 && typ == protowire.BytesType {
				frame, consumed := protowire.ConsumeBytes(raw)
				if consumed < 0 {
					return nil, protowire.ParseError(consumed)
				}
				wireFrames = append(wireFrames, bytes.Clone(frame))
				raw = raw[consumed:]
				continue
			}
			consumed := protowire.ConsumeFieldValue(number, typ, raw)
			if consumed < 0 {
				return nil, protowire.ParseError(consumed)
			}
			raw = raw[consumed:]
		}
	}
	if len(wireFrames) == 0 || len(wireFrames) > eventblock.MaxEntries {
		return nil, fmt.Errorf("event block contains %d frames", len(wireFrames))
	}
	frames := make([]rawFrame, 0, len(wireFrames))
	var previous uint64
	for _, wire := range wireFrames {
		entry := new(eventlogpb.EventEntry)
		if err := proto.Unmarshal(wire, entry); err != nil {
			return nil, fmt.Errorf("event block entry: %w", err)
		}
		if entry.GetCursor() == 0 || entry.GetCursor() <= previous || entry.GetEvent() == nil {
			return nil, errors.New("event block contains an invalid cursor or event")
		}
		frames = append(frames, rawFrame{
			cursor: entry.GetCursor(), kind: historypb.EventKind(observationNumber(entry)), size: proto.Size(entry), payload: bytes.Clone(wire),
		})
		previous = entry.GetCursor()
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
