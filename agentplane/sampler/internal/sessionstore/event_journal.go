package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"fmt"
	"math"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

type journalQueryer interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func (s *Store) Events(ctx context.Context, id string, after uint64, limit int) ([]*eventlogpb.EventEntry, error) {
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	if limit <= 0 || limit > 4096 {
		return nil, protocol.ErrInvalid
	}
	if after >= math.MaxInt32 {
		return nil, nil
	}
	return readJournalRange(ctx, s.db, id, after+1, min(after+uint64(limit), math.MaxInt32))
}

func journalEntry(ctx context.Context, q journalQueryer, sessionID string, sequence uint64) (*eventlogpb.EventEntry, error) {
	entries, err := readJournalRange(ctx, q, sessionID, sequence, sequence)
	if err != nil {
		return nil, err
	}
	if len(entries) == 0 {
		return nil, sql.ErrNoRows
	}
	return entries[0], nil
}

type journalFrame struct {
	sequence uint64
	wire     []byte
}

func readJournalRange(ctx context.Context, q journalQueryer, sessionID string, first, last uint64) ([]*eventlogpb.EventEntry, error) {
	frames, err := journalFrames(ctx, q, sessionID, first, last)
	if err != nil {
		return nil, err
	}
	var entries []*eventlogpb.EventEntry
	for _, frame := range frames {
		entry := &eventlogpb.EventEntry{}
		if err := eventcodec.Decode(frame.wire, entry); err != nil {
			return nil, fmt.Errorf("decode event at cursor %d: %w", frame.sequence, err)
		}
		if entry.GetCursor() != frame.sequence {
			return nil, fmt.Errorf("event at cursor %d contains cursor %d", frame.sequence, entry.GetCursor())
		}
		entries = append(entries, entry)
	}
	return entries, nil
}

func journalFrames(ctx context.Context, q journalQueryer, sessionID string, first, last uint64) ([]journalFrame, error) {
	// One MVCC snapshot sees either a sealed block or all its former standalone rows.
	// The maximum span lets both parts of the union use their session/sequence index.
	oldestBlock := max(first, eventblock.MaxEntries) - eventblock.MaxEntries + 1
	rows, err := q.QueryContext(ctx, `
		SELECT sequence,sequence,entry FROM sampler_event
		WHERE session_id=$1 AND sequence BETWEEN $2 AND $3
		UNION ALL
		SELECT first_sequence,last_sequence,payload FROM sampler_event_block
		WHERE session_id=$1 AND first_sequence BETWEEN $4 AND $3 AND last_sequence >= $2
		ORDER BY 1`, sessionID, first, last, oldestBlock)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var frames []journalFrame
	var previous uint64
	for rows.Next() {
		var start, end uint64
		var wire []byte
		if err := rows.Scan(&start, &end, &wire); err != nil {
			return nil, err
		}
		if start <= previous {
			return nil, fmt.Errorf("overlapping journal storage at cursor %d", start)
		}
		previous = end
		if eventblock.IsBlock(wire) {
			block, err := eventblock.Decode(wire)
			if err != nil {
				return nil, fmt.Errorf("decode event block %d–%d: %w", start, end, err)
			}
			if uint64(len(block)) != end-start+1 {
				return nil, fmt.Errorf("event block %d–%d contains %d frames", start, end, len(block))
			}
			for cursor := max(first, start); cursor <= min(last, end); cursor++ {
				frames = append(frames, journalFrame{sequence: cursor, wire: block[cursor-start]})
			}
			continue
		}
		if start == end {
			frames = append(frames, journalFrame{sequence: start, wire: wire})
			continue
		}
		block, err := eventcodec.UnpackBlock(wire, int(end-start+1))
		if err != nil {
			return nil, fmt.Errorf("decode event block %d–%d: %w", start, end, err)
		}
		for cursor := max(first, start); cursor <= min(last, end); cursor++ {
			frames = append(frames, journalFrame{sequence: cursor, wire: block[cursor-start]})
		}
	}
	return frames, rows.Err()
}

type sealedJournalBlock struct {
	first, last uint64
	payload     []byte
	storedBytes int
	versions    []string
}

const insertJournalBlock = `INSERT INTO sampler_event_block(session_id,first_sequence,last_sequence,payload) VALUES($1,$2,$3,$4)`

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
					payload: payload, storedBytes: storedSize, versions: versions,
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
