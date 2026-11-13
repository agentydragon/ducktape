package storage

import (
	"bytes"
	"compress/gzip"
	"context"
	"database/sql"
	"fmt"
	"io"
	"slices"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

const (
	blockEntries = 256
	blockMinimum = 64
	blockBytes   = 128 * 1024
)

// Blocks retain original entries, including unknown protobuf fields and native JSON bytes. Kind and
// cursor/size arrays are the bounded read index; gzip-compressed Entries is private to this store.
var compactKinds = []historypb.EventKind{
	historypb.EventKind_EVENT_KIND_NATIVE,
	historypb.EventKind_EVENT_KIND_TEXT_DELTA,
	historypb.EventKind_EVENT_KIND_TOOL_ARGUMENTS_DELTA,
	historypb.EventKind_EVENT_KIND_TOOL_OUTPUT_DELTA,
}

type eventReference struct {
	cursor uint64
	kind   historypb.EventKind
	size   int
	block  *uint64
}

func encodeBlock(entries []*eventlogpb.EventEntry) ([]byte, error) {
	raw, err := marshal(&historypb.Entries{Entries: entries})
	if err != nil {
		return nil, err
	}
	var buffer bytes.Buffer
	writer := gzip.NewWriter(&buffer)
	if _, err := writer.Write(raw); err != nil {
		return nil, err
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	return buffer.Bytes(), nil
}

func decodeBlock(payload []byte) ([]*eventlogpb.EventEntry, error) {
	reader, err := gzip.NewReader(bytes.NewReader(payload))
	if err != nil {
		return nil, fmt.Errorf("event block gzip: %w", err)
	}
	defer reader.Close()
	raw, err := io.ReadAll(io.LimitReader(reader, blockBytes+6*blockEntries+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > blockBytes+6*blockEntries {
		return nil, fmt.Errorf("event block exceeds decoded size limit")
	}
	var block historypb.Entries
	if err := proto.Unmarshal(raw, &block); err != nil {
		return nil, err
	}
	if len(block.Entries) == 0 || len(block.Entries) > blockEntries {
		return nil, fmt.Errorf("event block has %d entries", len(block.Entries))
	}
	var previous uint64
	for _, entry := range block.Entries {
		if entry.GetCursor() <= previous || entry.GetEvent() == nil {
			return nil, fmt.Errorf("event block contains an invalid cursor or event")
		}
		previous = entry.GetCursor()
	}
	return block.Entries, nil
}

func insertBlock(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, entries []*eventlogpb.EventEntry) error {
	payload, err := encodeBlock(entries)
	if err != nil {
		return err
	}
	cursors, sizes := make([]int64, len(entries)), make([]int64, len(entries))
	kind := observationNumber(entries[0])
	for index, entry := range entries {
		if observationNumber(entry) != kind {
			return fmt.Errorf("event block mixes observation kinds")
		}
		cursors[index], sizes[index] = int64(entry.GetCursor()), int64(proto.Size(entry))
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO session_event_block
		(session_id,first_cursor,last_cursor,kind,cursors,sizes,payload) VALUES($1,$2,$3,$4,$5,$6,$7)`,
		sessionID, cursors[0], cursors[len(cursors)-1], kind, pq.Array(cursors), pq.Array(sizes), payload)
	return err
}

// compactEvents seeks only the partial index of eligible new writes. Oversized frames and entries
// written with compaction disabled cannot make each append rescan an ever-growing retained prefix.
func compactEvents(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID) error {
	for _, kind := range compactKinds {
		for {
			rows, err := tx.QueryContext(ctx, `SELECT payload FROM session_event
				WHERE session_id=$1 AND kind=$2 AND compactable
				ORDER BY cursor LIMIT $3`, sessionID, kind, blockEntries)
			if err != nil {
				return err
			}
			var entries []*eventlogpb.EventEntry
			var size int
			for rows.Next() {
				var payload []byte
				if err := rows.Scan(&payload); err != nil {
					rows.Close()
					return err
				}
				if size+len(payload) > blockBytes {
					break
				}
				entry := &eventlogpb.EventEntry{}
				if err := proto.Unmarshal(payload, entry); err != nil {
					rows.Close()
					return err
				}
				entries, size = append(entries, entry), size+len(payload)
			}
			if err := rows.Err(); err != nil {
				rows.Close()
				return err
			}
			rows.Close()
			if len(entries) < blockMinimum {
				if size < blockBytes/2 || len(entries) < 2 {
					break
				}
			}
			if err := insertBlock(ctx, tx, sessionID, entries); err != nil {
				return err
			}
			cursors := make([]int64, len(entries))
			for index, entry := range entries {
				cursors[index] = int64(entry.GetCursor())
			}
			if _, err := tx.ExecContext(ctx, `DELETE FROM session_event WHERE session_id=$1 AND cursor=ANY($2)`, sessionID, pq.Array(cursors)); err != nil {
				return err
			}
		}
	}
	return nil
}

// eventReferences merges the singleton index and the four packed-kind indexes. Each seek is bounded
// by the requested event count, independent of the length of an excluded or already-read prefix.
func eventReferences(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, after, through uint64, kinds []historypb.EventKind, limit int, reverse bool) ([]eventReference, error) {
	kinds = slices.Clone(kinds)
	slices.Sort(kinds)
	kinds = slices.Compact(kinds)
	order := "ASC"
	if reverse {
		order = "DESC"
	}
	var references []eventReference
	readRaw := func(kind *historypb.EventKind) error {
		query := `SELECT cursor,kind,octet_length(payload) FROM session_event WHERE session_id=$1 AND cursor>$2 AND cursor<=$3`
		args := []any{sessionID, after, through, limit}
		if kind != nil {
			query += " AND kind=$5"
			args = append(args, *kind)
		}
		rows, err := tx.QueryContext(ctx, query+" ORDER BY cursor "+order+" LIMIT $4", args...)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var reference eventReference
			if err := rows.Scan(&reference.cursor, &reference.kind, &reference.size); err != nil {
				return err
			}
			references = append(references, reference)
		}
		return rows.Err()
	}
	if len(kinds) == 0 {
		if err := readRaw(nil); err != nil {
			return nil, err
		}
	} else {
		for _, kind := range kinds {
			if err := readRaw(&kind); err != nil {
				return nil, err
			}
		}
	}
	for _, kind := range compactKinds {
		if len(kinds) > 0 && !slices.Contains(kinds, kind) {
			continue
		}
		// One block can straddle the requested boundary for a kind. The rest begin (or end,
		// for reverse reads) inside the window. Blocks of one kind never overlap.
		query := `SELECT first_cursor,cursors,sizes FROM (
			(SELECT first_cursor,cursors,sizes FROM session_event_block WHERE session_id=$1 AND kind=$2
				AND first_cursor<=$3 ORDER BY first_cursor DESC LIMIT 1)
			UNION ALL
			(SELECT first_cursor,cursors,sizes FROM session_event_block WHERE session_id=$1 AND kind=$2
				AND first_cursor>$3 AND first_cursor<=$4 ORDER BY first_cursor LIMIT $5)
		) blocks ORDER BY first_cursor`
		if reverse {
			query = `SELECT first_cursor,cursors,sizes FROM (
				(SELECT first_cursor,cursors,sizes FROM session_event_block WHERE session_id=$1 AND kind=$2
					AND last_cursor>$4 ORDER BY last_cursor LIMIT 1)
				UNION ALL
				(SELECT first_cursor,cursors,sizes FROM session_event_block WHERE session_id=$1 AND kind=$2
					AND last_cursor<=$4 AND last_cursor>$3 ORDER BY last_cursor DESC LIMIT $5)
			) blocks ORDER BY first_cursor DESC`
		}
		rows, err := tx.QueryContext(ctx, query, sessionID, kind, after, through, limit)
		if err != nil {
			return nil, err
		}
		count := 0
		for rows.Next() {
			var first uint64
			var cursors, sizes pq.Int64Array
			if err := rows.Scan(&first, &cursors, &sizes); err != nil {
				rows.Close()
				return nil, err
			}
			if len(cursors) != len(sizes) {
				rows.Close()
				return nil, fmt.Errorf("event block index lengths disagree")
			}
			if reverse {
				slices.Reverse(cursors)
				slices.Reverse(sizes)
			}
			for index, cursor := range cursors {
				if uint64(cursor) > after && uint64(cursor) <= through && count < limit {
					references = append(references, eventReference{uint64(cursor), kind, int(sizes[index]), &first})
					count++
				}
			}
			if count == limit {
				break
			}
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return nil, err
		}
		rows.Close()
	}
	slices.SortFunc(references, func(a, b eventReference) int {
		if a.cursor == b.cursor {
			return 0
		}
		if (a.cursor < b.cursor) != reverse {
			return -1
		}
		return 1
	})
	return references[:min(limit, len(references))], nil
}

func loadEntries(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, references []eventReference) ([]*eventlogpb.EventEntry, error) {
	if len(references) == 0 {
		return nil, nil
	}
	var cursors []int64
	blocks := map[uint64]bool{}
	for _, reference := range references {
		if reference.block == nil {
			cursors = append(cursors, int64(reference.cursor))
		} else {
			blocks[*reference.block] = true
		}
	}
	byCursor := map[uint64]*eventlogpb.EventEntry{}
	if len(cursors) > 0 {
		rows, err := tx.QueryContext(ctx, `SELECT payload FROM session_event WHERE session_id=$1 AND cursor=ANY($2)`, sessionID, pq.Array(cursors))
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var payload []byte
			if err := rows.Scan(&payload); err != nil {
				rows.Close()
				return nil, err
			}
			entry := &eventlogpb.EventEntry{}
			if err := proto.Unmarshal(payload, entry); err != nil {
				rows.Close()
				return nil, err
			}
			byCursor[entry.GetCursor()] = entry
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return nil, err
		}
		rows.Close()
	}
	if len(blocks) > 0 {
		firsts := make([]int64, 0, len(blocks))
		for first := range blocks {
			firsts = append(firsts, int64(first))
		}
		rows, err := tx.QueryContext(ctx, `SELECT first_cursor,last_cursor,kind,cursors,sizes,payload
			FROM session_event_block WHERE session_id=$1 AND first_cursor=ANY($2)`, sessionID, pq.Array(firsts))
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var first, last uint64
			var kind int32
			var cursors, sizes pq.Int64Array
			var payload []byte
			if err := rows.Scan(&first, &last, &kind, &cursors, &sizes, &payload); err != nil {
				rows.Close()
				return nil, err
			}
			entries, err := decodeBlock(payload)
			if err != nil {
				rows.Close()
				return nil, err
			}
			if len(entries) != len(cursors) || len(entries) != len(sizes) || entries[0].Cursor != first || entries[len(entries)-1].Cursor != last {
				rows.Close()
				return nil, fmt.Errorf("event block index disagrees with its content")
			}
			for index, entry := range entries {
				if int64(entry.Cursor) != cursors[index] || int64(proto.Size(entry)) != sizes[index] || observationNumber(entry) != kind {
					rows.Close()
					return nil, fmt.Errorf("event block index disagrees at cursor %d", entry.Cursor)
				}
				byCursor[entry.GetCursor()] = entry
			}
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return nil, err
		}
		rows.Close()
	}
	entries := make([]*eventlogpb.EventEntry, 0, len(references))
	for _, reference := range references {
		entry := byCursor[reference.cursor]
		if entry == nil || observationNumber(entry) != int32(reference.kind) || proto.Size(entry) != reference.size {
			return nil, fmt.Errorf("event %d disagrees with its storage index", reference.cursor)
		}
		entries = append(entries, entry)
	}
	return entries, nil
}

// ReadEntry reads an exact retained cursor, including a packed entry. Callers hold their own snapshot.
func ReadEntry(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, cursor uint64) (*eventlogpb.EventEntry, error) {
	if cursor == 0 {
		return nil, sql.ErrNoRows
	}
	refs, err := eventReferences(ctx, tx, sessionID, cursor-1, cursor, nil, 1, false)
	if err != nil {
		return nil, err
	}
	entries, err := loadEntries(ctx, tx, sessionID, refs)
	if err != nil {
		return nil, err
	}
	if len(entries) == 0 {
		return nil, sql.ErrNoRows
	}
	return entries[0], nil
}

// CountEventPayloads counts logical events and uncompressed protobuf bytes, including packed entries.
// Block compression is shared across entries, so it has no exact per-entry physical byte cost.
func CountEventPayloads(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, first, last uint64) (int64, int64, error) {
	var count, size int64
	err := tx.QueryRowContext(ctx, `SELECT count(*),COALESCE(sum(size),0) FROM (
		SELECT octet_length(payload) AS size FROM session_event WHERE session_id=$1 AND cursor BETWEEN $2 AND $3
		UNION ALL
		SELECT e.size FROM session_event_block b CROSS JOIN LATERAL unnest(b.cursors,b.sizes) e(cursor,size)
		WHERE b.session_id=$1 AND b.first_cursor<=$3 AND b.last_cursor>=$2 AND e.cursor BETWEEN $2 AND $3
	) events`, sessionID, first, last).Scan(&count, &size)
	return count, size, err
}

func deleteEventRanges(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, ranges []*historypb.CursorRange) error {
	if len(ranges) == 0 {
		return nil
	}
	firsts, lasts := make([]int64, len(ranges)), make([]int64, len(ranges))
	for index, r := range ranges {
		firsts[index], lasts[index] = int64(r.First), int64(r.Last)
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM session_event e WHERE session_id=$1 AND EXISTS (
		SELECT FROM unnest($2::bigint[],$3::bigint[]) r(lo,hi) WHERE e.cursor BETWEEN r.lo AND r.hi)`, sessionID, pq.Array(firsts), pq.Array(lasts)); err != nil {
		return err
	}
	rows, err := tx.QueryContext(ctx, `SELECT first_cursor,payload FROM session_event_block b WHERE session_id=$1 AND EXISTS (
		SELECT FROM unnest($2::bigint[],$3::bigint[]) r(lo,hi) WHERE b.first_cursor<=r.hi AND b.last_cursor>=r.lo)`, sessionID, pq.Array(firsts), pq.Array(lasts))
	if err != nil {
		return err
	}
	type retainedBlock struct {
		first   uint64
		entries []*eventlogpb.EventEntry
	}
	var retained []retainedBlock
	for rows.Next() {
		var first uint64
		var payload []byte
		if err := rows.Scan(&first, &payload); err != nil {
			rows.Close()
			return err
		}
		entries, err := decodeBlock(payload)
		if err != nil {
			rows.Close()
			return err
		}
		entries = slices.DeleteFunc(entries, func(entry *eventlogpb.EventEntry) bool {
			return slices.ContainsFunc(ranges, func(r *historypb.CursorRange) bool { return entry.Cursor >= r.First && entry.Cursor <= r.Last })
		})
		retained = append(retained, retainedBlock{first, entries})
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()
	for _, block := range retained {
		if _, err := tx.ExecContext(ctx, `DELETE FROM session_event_block WHERE session_id=$1 AND first_cursor=$2`, sessionID, block.first); err != nil {
			return err
		}
		if len(block.entries) > 0 {
			if err := insertBlock(ctx, tx, sessionID, block.entries); err != nil {
				return err
			}
		}
	}
	return nil
}
