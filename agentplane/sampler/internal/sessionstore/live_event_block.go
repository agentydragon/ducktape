package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
)

const insertJournalBlock = `INSERT INTO sampler_event_block(session_id,first_sequence,last_sequence,payload) VALUES($1,$2,$3,$4)`

type pendingJournalFrame struct {
	sequence uint64
	wire     []byte
	frame    []byte
}

// sealJournalTail evaluates only the bounded live tail. A tail remains in ordinary
// rows until the profile seals it or a cursor gap ends its stream.
func sealJournalTail(ctx context.Context, tx *sql.Tx, sessionID string, through uint64, profile eventblock.Profile) error {
	packer, err := eventblock.NewPacker(profile)
	if err != nil {
		return err
	}
	first := uint64(1)
	if through >= eventblock.MaxEntries {
		first = through - eventblock.MaxEntries + 1
	}
	rows, err := tx.QueryContext(ctx, `
		WITH tail AS (
			SELECT sequence,entry,octet_length(entry) AS stored_bytes FROM sampler_event
			WHERE session_id=$1 AND sequence BETWEEN $2 AND $3
			ORDER BY sequence DESC LIMIT $4
		), bounded AS (
			SELECT *,row_number() OVER (ORDER BY sequence DESC) AS ordinal,
				sum(stored_bytes) OVER (ORDER BY sequence DESC ROWS UNBOUNDED PRECEDING) AS cumulative_bytes
			FROM tail
		)
		SELECT sequence,CASE WHEN cumulative_bytes<=$5 THEN entry END
		FROM bounded
		WHERE cumulative_bytes<=$5 OR ordinal=1 OR cumulative_bytes-stored_bytes<$5
		ORDER BY sequence`, sessionID, first, through, eventblock.MaxEntries, eventblock.MaxBytes+(64<<10))
	if err != nil {
		return err
	}
	var frames []pendingJournalFrame
	for rows.Next() {
		var frame pendingJournalFrame
		if err := rows.Scan(&frame.sequence, &frame.wire); err != nil {
			rows.Close()
			return err
		}
		frame.frame, err = eventcodec.UnpackPackableEntry(frame.wire)
		if err != nil {
			rows.Close()
			return fmt.Errorf("unpack live event at cursor %d: %w", frame.sequence, err)
		}
		frames = append(frames, frame)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	if err := rows.Close(); err != nil {
		return err
	}
	for start := 0; start < len(frames); {
		if !packableJournalFrame(frames[start]) {
			start++
			continue
		}
		end := start + 1
		for end < len(frames) && packableJournalFrame(frames[end]) && frames[end].sequence == frames[end-1].sequence+1 {
			end++
		}
		flushTail := end < len(frames)
		stream := frames[start:end]
		for offset := 0; offset < len(stream); {
			wireFrames := make([][]byte, len(stream)-offset)
			for i := range wireFrames {
				wireFrames[i] = stream[offset+i].frame
			}
			cut := packer.Next(wireFrames, flushTail)
			switch cut.Decision {
			case eventblock.NeedMore:
				offset = len(stream)
			case eventblock.Standalone:
				offset += cut.N
			case eventblock.Seal:
				selected := stream[offset : offset+cut.N]
				blockFrames := wireFrames[:cut.N]
				payload, err := packer.Encode(blockFrames)
				if err != nil {
					return err
				}
				storedBytes := 0
				for _, frame := range selected {
					storedBytes += len(frame.wire)
				}
				if len(payload) < storedBytes {
					if err := insertLiveJournalBlock(ctx, tx, sessionID, selected, blockFrames, payload); err != nil {
						return err
					}
				}
				offset += cut.N
			default:
				return fmt.Errorf("unknown event block decision %d", cut.Decision)
			}
		}
		start = end
	}
	return nil
}

func packableJournalFrame(frame pendingJournalFrame) bool {
	return len(frame.frame) > 0
}

func insertLiveJournalBlock(ctx context.Context, tx *sql.Tx, sessionID string, source []pendingJournalFrame, frames [][]byte, payload []byte) error {
	first, last := source[0].sequence, source[len(source)-1].sequence
	if uint64(len(source)) != last-first+1 {
		return fmt.Errorf("noncontiguous live journal block %d–%d", first, last)
	}
	decoded, err := eventblock.Decode(payload)
	if err != nil {
		return fmt.Errorf("verify live event block %d–%d: %w", first, last, err)
	}
	if len(decoded) != len(frames) {
		return fmt.Errorf("live event block %d–%d contains %d frames, want %d", first, last, len(decoded), len(frames))
	}
	for i := range frames {
		if !bytes.Equal(decoded[i], frames[i]) {
			return fmt.Errorf("live event block frame at cursor %d changed during encoding", source[i].sequence)
		}
	}
	if _, err := tx.ExecContext(ctx, insertJournalBlock, sessionID, first, last, payload); err != nil {
		return err
	}
	deleted, err := tx.ExecContext(ctx, `DELETE FROM sampler_event WHERE session_id=$1 AND sequence BETWEEN $2 AND $3`,
		sessionID, first, last)
	if err != nil {
		return err
	}
	count, err := deleted.RowsAffected()
	if err != nil {
		return err
	}
	if count != int64(len(source)) {
		return fmt.Errorf("deleted %d live journal rows while sealing %d–%d", count, first, last)
	}
	return nil
}
