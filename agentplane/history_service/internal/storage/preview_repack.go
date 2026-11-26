package storage

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

type repackPreviewStream struct {
	tx        *sql.Tx
	sessionID uuid.UUID
	kind      historypb.EventKind
	through   uint64

	rawAfter uint64
	rawPage  []repackRawMeta
	rawIndex int

	blockAfter uint64
	block      *storedBlock
	blockIndex int
	lastBlock  uint64

	result CompactionResult
}

// previewHistorySession runs the same packer over a bounded cursor merge of ordinary rows and
// decoded blocks. It uses a read-only MVCC snapshot and never changes journal or checkpoint rows.
func (s *Store) previewHistorySession(ctx context.Context, sessionID uuid.UUID,
	kinds []historypb.EventKind, before RepackBeforeBatch,
) (CompactionResult, error) {
	var total CompactionResult
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		return total, err
	}
	defer tx.Rollback()
	var through uint64
	if err := tx.QueryRowContext(ctx, `SELECT last_cursor FROM session_history WHERE id=$1`, sessionID).Scan(&through); err != nil {
		return total, err
	}
	for _, kind := range kinds {
		stream := &repackPreviewStream{tx: tx, sessionID: sessionID, kind: kind, through: through}
		result, err := s.previewRepackKind(ctx, stream, s.options.EventPackers[kind], before)
		result.SourceBlocks += stream.result.SourceBlocks
		result.LegacyBlocks += stream.result.LegacyBlocks
		result.VersionedSourceBlocks += stream.result.VersionedSourceBlocks
		addCompactionResult(&total, result)
		if err != nil {
			return total, err
		}
	}
	return total, nil
}

func (s *Store) previewRepackKind(ctx context.Context, stream *repackPreviewStream, packer *eventblock.Packer,
	before RepackBeforeBatch,
) (CompactionResult, error) {
	result := CompactionResult{}
	var pending []repackRawFrame
	var done bool
	for {
		if len(pending) == 0 && !done {
			frame, ok, err := stream.next(ctx)
			if err != nil {
				return result, err
			}
			if !ok {
				done = true
			} else {
				pending = append(pending, frame)
			}
		}
		if len(pending) == 0 && done {
			return result, nil
		}
		sizes := make([]int, len(pending))
		totalBytes := 0
		for i, frame := range pending {
			sizes[i] = frame.meta.size
			totalBytes += frame.meta.size + 4
		}
		cut := packer.NextSizes(sizes, false)
		if cut.Decision == eventblock.NeedMore && !done && len(pending) < eventblock.MaxEntries && totalBytes < eventblock.MaxBytes {
			frame, ok, err := stream.next(ctx)
			if err != nil {
				return result, err
			}
			if ok {
				pending = append(pending, frame)
				continue
			}
			done = true
			cut = packer.NextSizes(sizes, true)
		}
		if cut.Decision == eventblock.NeedMore && done {
			cut = packer.NextSizes(sizes, true)
		}
		if cut.N < 1 {
			return result, errors.New("event block profile did not advance the dry-run stream")
		}
		frames := pending[:cut.N]
		if err := stream.materialize(ctx, frames); err != nil {
			return result, err
		}
		plan, err := buildRepackPlan(packer, frames, cut)
		if err != nil {
			return result, err
		}
		var rateBytes int64
		for _, frame := range frames {
			rateBytes += frame.meta.storedBytes
			result.UncompressedBytes += int64(frame.meta.size)
		}
		if before != nil {
			if err := before(ctx, rateBytes); err != nil {
				return result, err
			}
		}
		result.Entries += int64(len(frames))
		if plan.pack {
			result.Blocks++
			result.PackedBytes += int64(len(plan.encoded))
		} else {
			result.Skipped += int64(len(frames))
		}
		pending = pending[cut.N:]
	}
}

func (stream *repackPreviewStream) next(ctx context.Context) (repackRawFrame, bool, error) {
	for {
		if stream.rawIndex >= len(stream.rawPage) {
			if err := stream.loadRawPage(ctx); err != nil {
				return repackRawFrame{}, false, err
			}
		}
		if stream.block == nil || stream.blockIndex >= len(stream.block.frames) {
			if stream.block != nil {
				stream.lastBlock = stream.block.last
				stream.block = nil
			}
			block, found, err := stream.loadBlock(ctx)
			if err != nil {
				return repackRawFrame{}, false, err
			}
			if found {
				stream.block = &block
				stream.blockIndex = 0
				stream.blockAfter = block.first
				stream.result.SourceBlocks++
				if eventblock.IsBlock(block.payload) {
					stream.result.VersionedSourceBlocks++
				} else {
					stream.result.LegacyBlocks++
				}
			}
		}
		rawOK := stream.rawIndex < len(stream.rawPage)
		blockOK := stream.block != nil && stream.blockIndex < len(stream.block.frames)
		for blockOK && stream.block.frames[stream.blockIndex].cursor > stream.through {
			stream.blockIndex++
			blockOK = stream.blockIndex < len(stream.block.frames)
		}
		if !rawOK && !blockOK {
			return repackRawFrame{}, false, nil
		}
		if rawOK && blockOK && stream.rawPage[stream.rawIndex].cursor == stream.block.frames[stream.blockIndex].cursor {
			return repackRawFrame{}, false, fmt.Errorf("ordinary row and block both contain cursor %d", stream.rawPage[stream.rawIndex].cursor)
		}
		if blockOK && (!rawOK || stream.block.frames[stream.blockIndex].cursor < stream.rawPage[stream.rawIndex].cursor) {
			frame := stream.block.frames[stream.blockIndex]
			stream.blockIndex++
			return repackRawFrame{meta: repackRawMeta{
				cursor: frame.cursor, size: len(frame.payload), indexSize: frame.size,
				storedBytes: int64(len(frame.payload) + 4),
			}, payload: bytes.Clone(frame.payload)}, true, nil
		}
		meta := stream.rawPage[stream.rawIndex]
		stream.rawIndex++
		stream.rawAfter = meta.cursor
		return repackRawFrame{meta: meta}, true, nil
	}
}

func (stream *repackPreviewStream) loadRawPage(ctx context.Context) error {
	stream.rawPage, stream.rawIndex = nil, 0
	rows, err := stream.tx.QueryContext(ctx, `SELECT cursor,octet_length(payload),pg_column_size(payload) FROM session_event
		WHERE session_id=$1 AND kind=$2 AND cursor>$3 AND cursor<=$4 ORDER BY cursor LIMIT $5`,
		stream.sessionID, stream.kind, stream.rawAfter, stream.through, eventblock.MaxEntries)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var meta repackRawMeta
		if err := rows.Scan(&meta.cursor, &meta.size, &meta.storedBytes); err != nil {
			return err
		}
		stream.rawPage = append(stream.rawPage, meta)
	}
	return rows.Err()
}

func (stream *repackPreviewStream) loadBlock(ctx context.Context) (storedBlock, bool, error) {
	block, err := scanStoredBlock(stream.tx.QueryRowContext(ctx, `SELECT first_cursor,last_cursor,kind,cursors,sizes,payload,pg_column_size(payload)
		FROM session_event_block WHERE session_id=$1 AND kind=$2 AND first_cursor>$3 AND first_cursor<=$4
		ORDER BY first_cursor LIMIT 1`, stream.sessionID, stream.kind, stream.blockAfter, stream.through))
	if errors.Is(err, sql.ErrNoRows) {
		return storedBlock{}, false, nil
	}
	if err != nil {
		return storedBlock{}, false, err
	}
	if block.first <= stream.lastBlock {
		return storedBlock{}, false, errors.New("overlapping History event block spans")
	}
	return block, true, nil
}

func (stream *repackPreviewStream) materialize(ctx context.Context, frames []repackRawFrame) error {
	var cursors []int64
	byCursor := make(map[uint64]int, len(frames))
	for i, frame := range frames {
		if frame.payload != nil || frame.meta.size+4 > eventblock.MaxBytes {
			continue
		}
		cursors = append(cursors, int64(frame.meta.cursor))
		byCursor[frame.meta.cursor] = i
	}
	if len(cursors) == 0 {
		return nil
	}
	rows, err := stream.tx.QueryContext(ctx, `SELECT cursor,kind,payload FROM session_event
		WHERE session_id=$1 AND cursor=ANY($2) ORDER BY cursor`, stream.sessionID, pq.Array(cursors))
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var cursor uint64
		var kind historypb.EventKind
		var payload []byte
		if err := rows.Scan(&cursor, &kind, &payload); err != nil {
			return err
		}
		index, ok := byCursor[cursor]
		if !ok || kind != stream.kind || len(payload) != frames[index].meta.size {
			return errStaleRepackCandidate
		}
		entry, err := unmarshalRawEntry(payload)
		if err != nil || entry.GetCursor() != cursor || historypb.EventKind(observationNumber(entry)) != kind {
			return fmt.Errorf("ordinary History row %d disagrees with its index", cursor)
		}
		frames[index].meta.indexSize = proto.Size(entry)
		frames[index].payload = bytes.Clone(payload)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for _, cursor := range cursors {
		index := byCursor[uint64(cursor)]
		if frames[index].payload == nil {
			return errStaleRepackCandidate
		}
	}
	return nil
}
