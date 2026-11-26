package sessionstore

import (
	"context"
	"errors"
	"log/slog"
	"time"
)

// RunJournalCompaction packs one completed window per Session per sweep.
// Maintenance failures retry without interrupting the sampler's serving paths.
func (s *Store) RunJournalCompaction(ctx context.Context) error {
	after := ""
	advanced := false
	for ctx.Err() == nil {
		sessions, err := s.pendingJournalCompactions(ctx, after)
		if err != nil && ctx.Err() == nil {
			slog.WarnContext(ctx, "list pending sampler journal compactions", "error", err)
		}
		if len(sessions) == 0 {
			after = ""
			if !advanced {
				if err := waitForJournalCompaction(ctx, time.Second); err != nil {
					break
				}
			}
			advanced = false
			continue
		}
		for _, session := range sessions {
			err := s.compactPendingJournal(ctx, session)
			if err == nil {
				advanced = true
			} else if !errors.Is(err, errJournalWindowChanged) && ctx.Err() == nil {
				slog.WarnContext(ctx, "pack sampler journal window", "session_id", session.id, "error", err)
			}
			after = session.id
			if err := waitForJournalCompaction(ctx, journalCompactionPace); err != nil {
				return nil
			}
		}
	}
	return nil
}

type pendingJournalCompaction struct {
	id            string
	cursor, after uint64
}

func (s *Store) pendingJournalCompactions(ctx context.Context, after string) ([]pendingJournalCompaction, error) {
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	rows, err := s.db.QueryContext(ctx, `
		SELECT id,cursor,journal_compacted_through FROM sampler_session
		WHERE id>$1 AND cursor-journal_compacted_through >= $2 ORDER BY id LIMIT 32`, after, journalWindowSize)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var sessions []pendingJournalCompaction
	for rows.Next() {
		var session pendingJournalCompaction
		if err := rows.Scan(&session.id, &session.cursor, &session.after); err != nil {
			return nil, err
		}
		sessions = append(sessions, session)
	}
	return sessions, rows.Err()
}

func (s *Store) compactPendingJournal(ctx context.Context, session pendingJournalCompaction) error {
	through := session.cursor / journalWindowSize * journalWindowSize
	last, err := s.nextJournalWindow(ctx, session.id, session.after, through)
	if err != nil {
		return err
	}
	if last == 0 {
		last = through
	} else if _, err := s.compactJournalWindow(ctx, session.id, last-journalWindowSize+1, last, true); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	// Advance even for incompressible windows. If the worker exits after the
	// blocks commit and before this update, a fresh search skips packed rows.
	_, err = s.db.ExecContext(ctx, `UPDATE sampler_session SET journal_compacted_through=$2
		WHERE id=$1 AND journal_compacted_through<$2`, session.id, last)
	return err
}
