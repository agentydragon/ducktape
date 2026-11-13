package samplerauditmigration

import (
	"bytes"
	"compress/gzip"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io"
	"log/slog"

	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/proto"
)

type journal struct {
	sessions, page, read, update, lock string
	intents                            bool
}

// Down refuses to claim that deleting a Goose history row restores the removed duplicates.
func Down(context.Context, *sql.Tx) error {
	return errors.New("sampler audit deduplication cannot be downgraded; use the compact-audit writer image")
}

// Sampler rewrites both copies atomically per audit. Serving replicas and exporters must be stopped.
func Sampler(ctx context.Context, db *sql.DB) error {
	return rewrite(ctx, db, journal{
		sessions: `SELECT id FROM sampler_session ORDER BY id`,
		page:     `SELECT sequence,entry FROM sampler_event WHERE session_id=$1 AND sequence>$2 ORDER BY sequence LIMIT 32`,
		read:     `SELECT entry FROM sampler_event WHERE session_id=$1 AND sequence=$2 FOR UPDATE`,
		update:   `UPDATE sampler_event SET entry=$3 WHERE session_id=$1 AND sequence=$2`,
		lock:     `SELECT cursor FROM sampler_session WHERE id=$1 FOR UPDATE`,
		intents:  true,
	})
}

// History rewrites only result boundaries. Kind 31 is not eligible for packed event storage.
func History(ctx context.Context, db *sql.DB) error {
	var packed bool
	if err := db.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM session_event_block WHERE kind=31)`).Scan(&packed); err != nil {
		return err
	}
	if packed {
		return fmt.Errorf("audit migration requires ordinary boundary records; found packed kind 31")
	}
	return rewrite(ctx, db, journal{
		sessions: `SELECT id::text FROM session_history ORDER BY id`,
		page:     `SELECT cursor,payload FROM session_event WHERE session_id=$1 AND kind=31 AND cursor>$2 ORDER BY cursor LIMIT 32`,
		read:     `SELECT payload FROM session_event WHERE session_id=$1 AND cursor=$2 FOR UPDATE`,
		update:   `UPDATE session_event SET payload=$3 WHERE session_id=$1 AND cursor=$2`,
		lock:     `SELECT last_cursor FROM session_history WHERE id=$1 FOR UPDATE`,
	})
}

func rewrite(ctx context.Context, db *sql.DB, j journal) error {
	rows, err := db.QueryContext(ctx, j.sessions)
	if err != nil {
		return err
	}
	var sessions []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		sessions = append(sessions, id)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, session := range sessions {
		var r rewriter
		var after uint64
		var changed int
		for {
			rows, err := db.QueryContext(ctx, j.page, session, after)
			if err != nil {
				return err
			}
			var page []*eventlogpb.EventEntry
			for rows.Next() {
				var cursor uint64
				var wire []byte
				if err := rows.Scan(&cursor, &wire); err != nil {
					rows.Close()
					return err
				}
				entry, err := decode(wire)
				if err != nil {
					rows.Close()
					return fmt.Errorf("decode %s/%d: %w", session, cursor, err)
				}
				if entry.Cursor != cursor {
					rows.Close()
					return fmt.Errorf("cursor mismatch %s/%d", session, cursor)
				}
				page = append(page, entry)
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return err
			}
			if len(page) == 0 {
				break
			}
			for _, entry := range page {
				change, err := r.visit(entry)
				if err != nil {
					return fmt.Errorf("audit migration %s/%d: %w", session, entry.Cursor, err)
				}
				if change != nil {
					if err := apply(ctx, db, j, session, change); err != nil {
						return err
					}
					changed++
				}
				after = entry.Cursor
			}
		}
		if err := r.finish(); err != nil {
			return fmt.Errorf("session %s: %w", session, err)
		}
		if changed > 0 {
			slog.InfoContext(ctx, "removed duplicated sampler audit events", "session_id", session, "records", changed)
		}
	}
	if j.intents {
		var remaining int
		if err := db.QueryRowContext(ctx, `SELECT count(*) FROM sampler_intent WHERE request->>'kind' IN ('sampling','compaction') AND result ? 'events' AND NOT (result ? 'status')`).Scan(&remaining); err != nil {
			return err
		}
		if remaining != 0 {
			return fmt.Errorf("%d successful intent audits have no rewritten result boundary", remaining)
		}
	}
	return nil
}

func apply(ctx context.Context, db *sql.DB, j journal, session string, c *change) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var watermark uint64
	if err := tx.QueryRowContext(ctx, j.lock, session).Scan(&watermark); err != nil {
		return err
	}
	for i, before := range c.before {
		if before.Cursor > watermark {
			return fmt.Errorf("audit exceeds session watermark: %s/%d", session, before.Cursor)
		}
		var stored []byte
		if err := tx.QueryRowContext(ctx, j.read, session, before.Cursor).Scan(&stored); err != nil {
			return err
		}
		entry, err := decode(stored)
		if err != nil {
			return err
		}
		if !proto.Equal(entry, before) {
			return fmt.Errorf("audit changed during migration: %s/%d", session, before.Cursor)
		}
		wire, err := encode(c.after[i], j.intents)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, j.update, session, before.Cursor, wire); err != nil {
			return err
		}
	}
	if j.intents {
		var same bool
		if err := tx.QueryRowContext(ctx, `SELECT result=$2::jsonb FROM sampler_intent
			WHERE id=$1 AND request->>'kind' IN ('sampling','compaction')
			AND conversation_id=(SELECT conversation_id FROM sampler_session WHERE id=$3)
			FOR UPDATE`, c.intentID, string(c.original), session).Scan(&same); err != nil {
			return err
		}
		if !same {
			return fmt.Errorf("intent %s disagrees with its result boundary", c.intentID)
		}
		if _, err := tx.ExecContext(ctx, `UPDATE sampler_intent SET result=$2 WHERE id=$1`, c.intentID, string(c.compacted)); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func decode(wire []byte) (*eventlogpb.EventEntry, error) {
	if len(wire) >= 2 && wire[0] == 0x1f && wire[1] == 0x8b {
		reader, err := gzip.NewReader(bytes.NewReader(wire))
		if err != nil {
			return nil, err
		}
		wire, err = io.ReadAll(io.LimitReader(reader, maxRecordBytes+1))
		closeErr := reader.Close()
		if err != nil {
			return nil, err
		}
		if closeErr != nil {
			return nil, closeErr
		}
	}
	if len(wire) > maxRecordBytes {
		return nil, fmt.Errorf("stored event exceeds migration size limit")
	}
	entry := new(eventlogpb.EventEntry)
	return entry, proto.Unmarshal(wire, entry)
}

func encode(entry *eventlogpb.EventEntry, compress bool) ([]byte, error) {
	wire, err := proto.Marshal(entry)
	if err != nil {
		return nil, err
	}
	if !compress || len(wire) < 4096 {
		return wire, nil
	}
	var buffer bytes.Buffer
	writer, err := gzip.NewWriterLevel(&buffer, gzip.BestSpeed)
	if err != nil {
		return nil, err
	}
	if _, err := writer.Write(wire); err != nil {
		return nil, err
	}
	if err := writer.Close(); err != nil {
		return nil, err
	}
	if buffer.Len() < len(wire) {
		return buffer.Bytes(), nil
	}
	return wire, nil
}
