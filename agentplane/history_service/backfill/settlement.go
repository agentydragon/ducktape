// Package backfill settles completed items already retained by History.
package backfill

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/google/uuid"
)

var (
	ErrSessionAdvanced = errors.New("Session advanced during backfill; retry from a fresh scan")
	ErrProjectionBusy  = errors.New("Session projection lease held; retry after the fold releases it")
)

// Result counts eligible raw delta entries and their logical protobuf bytes, excluding indexes and rows.
type Result struct {
	Items              int
	Rows, PayloadBytes int64
}

type scan struct {
	last        uint64
	completions []uint64
	// Later observations can cite a native chunk outside its original item span. Keep those chunks.
	lastReference map[uint64]uint64
}

func scanSession(ctx context.Context, db *sql.DB, sessionID uuid.UUID) (scan, error) {
	found := scan{lastReference: map[uint64]uint64{}}
	history := storage.NewStore(db, storage.Options{})
	for after := uint64(0); ; {
		pageCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
		page, err := history.ReadFollowEntries(pageCtx, sessionID, after, nil, false)
		cancel()
		if err != nil {
			return found, err
		}
		if after == 0 {
			found.last = page.LastCursor
		}
		if page.LastCursor != found.last {
			return found, ErrSessionAdvanced
		}
		for _, entry := range page.Entries {
			for _, source := range entry.GetEvent().GetSourceSequences() {
				found.lastReference[source] = entry.GetCursor()
			}
			if entry.GetEvent().GetItemCompleted() != nil || entry.GetEvent().GetToolArguments() != nil {
				found.completions = append(found.completions, entry.GetCursor())
			}
		}
		if page.ThroughCursor == found.last {
			return found, nil
		}
		after = page.ThroughCursor
		if after%4096 == 0 {
			slog.InfoContext(ctx, "scanning retained history", "session_id", sessionID, "through_cursor", after, "last_cursor", found.last)
		}
	}
}

// RunSession uses bounded reads and one write transaction per eligible completion. Dry runs use
// read-only transactions. Apply fences ingress and the projector, and refuses a changed raw prefix.
// Restarting skips recorded settlements; no runner events or provider calls are replayed.
func RunSession(ctx context.Context, db *sql.DB, sessionID uuid.UUID, apply bool) (Result, error) {
	found, err := scanSession(ctx, db, sessionID)
	if err != nil {
		return Result{}, err
	}
	var total Result
	for _, cursor := range found.completions {
		itemCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
		result, err := settleCompletion(itemCtx, db, sessionID, cursor, found, false)
		if err == nil && apply && result.Items != 0 {
			// Recheck under the write fences: eligibility may change after this read-only check.
			result, err = settleCompletion(itemCtx, db, sessionID, cursor, found, true)
		}
		cancel()
		if err != nil {
			return total, fmt.Errorf("Session %s completion %d: %w", sessionID, cursor, err)
		}
		total.Items += result.Items
		total.Rows += result.Rows
		total.PayloadBytes += result.PayloadBytes
		if result.Items != 0 {
			slog.InfoContext(ctx, "historical settlement", "session_id", sessionID, "completion_cursor", cursor,
				"apply", apply, "rows", result.Rows, "logical_payload_bytes", result.PayloadBytes)
		}
	}
	return total, nil
}

func settleCompletion(ctx context.Context, db *sql.DB, sessionID uuid.UUID, cursor uint64, found scan, apply bool) (Result, error) {
	var result Result
	options := sql.TxOptions{ReadOnly: !apply, Isolation: sql.LevelRepeatableRead}
	if apply {
		options.Isolation = sql.LevelReadCommitted
	}
	err := postgres.Transaction(ctx, db, options, func(tx *sql.Tx) error {
		var held *projection.Lease
		var err error
		if apply {
			if _, err := tx.ExecContext(ctx, "SET LOCAL lock_timeout = '1s'"); err != nil {
				return err
			}
			// A projector can fetch a page before taking its write fence. Taking over its expired
			// lease invalidates that page's token; a live owner must finish before we proceed.
			held, err = projection.AcquireLease(ctx, tx, sessionID, time.Minute)
			if err != nil {
				return err
			}
			if held == nil {
				return ErrProjectionBusy
			}
		}
		query := "SELECT last_cursor FROM session_history WHERE id = $1"
		if apply {
			query += " FOR UPDATE"
		}
		var last uint64
		if err := tx.QueryRowContext(ctx, query, sessionID).Scan(&last); err != nil {
			return err
		}
		if last != found.last {
			return ErrSessionAdvanced
		}
		var recorded bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS (SELECT FROM session_event_settlement
			WHERE session_id = $1 AND completion_cursor = $2)`, sessionID, cursor).Scan(&recorded); err != nil {
			return err
		}
		if !recorded {
			entry, err := storage.ReadEntry(ctx, tx, sessionID, cursor)
			if err != nil {
				return err
			}
			settled, err := storage.SettlementForCompletion(ctx, tx, sessionID, entry)
			if err != nil {
				return err
			}
			if settled != nil {
				for _, r := range settled.GetRanges() {
					for source := r.GetFirst(); source <= r.GetLast(); source++ {
						if found.lastReference[source] > cursor {
							if held != nil {
								return projection.ReleaseLease(ctx, tx, *held)
							}
							return nil
						}
					}
				}
				for _, r := range settled.GetRanges() {
					rows, bytes, err := storage.CountEventPayloads(ctx, tx, sessionID, r.GetFirst(), r.GetLast())
					if err != nil {
						return err
					}
					result.Rows += rows
					result.PayloadBytes += bytes
				}
				result.Items = 1
				if apply {
					if err := applySettlement(ctx, tx, sessionID, settled); err != nil {
						return err
					}
				}
			}
		}
		if held != nil {
			return projection.ReleaseLease(ctx, tx, *held)
		}
		return nil
	})
	return result, err
}

func applySettlement(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, settled *historypb.SettledDeltas) error {
	if err := storage.RecordSettlement(ctx, tx, sessionID, settled); err != nil {
		return err
	}
	var epoch string
	err := tx.QueryRowContext(ctx, "SELECT projection_epoch FROM thread_checkpoint WHERE thread_id = $1", sessionID).Scan(&epoch)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	if err == nil {
		if err := projection.ForgetSettled(ctx, tx, sessionID, epoch, []*historypb.SettledDeltas{settled}); err != nil {
			return err
		}
	}
	// Followers already past the completion do not receive old settlements. Cleanup ranges
	// are the existing correction protocol for deleting an entry below their resume cursor.
	for _, r := range settled.GetRanges() {
		if _, err := tx.ExecContext(ctx, `INSERT INTO session_event_removed_range (session_id, first_cursor, last_cursor)
			VALUES ($1, $2, $3)`, sessionID, r.GetFirst(), r.GetLast()); err != nil {
			return err
		}
	}
	return postgres.Notify(ctx, tx, storage.ChannelHistory, "")
}
