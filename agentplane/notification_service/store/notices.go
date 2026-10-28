package store

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"slices"

	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

// InvalidHistoryPrefix starts the delivery error of an inbox whose runner history cannot be followed. Such an
// inbox is not delivered to again: each release stores the error again.
const InvalidHistoryPrefix = "invalid history"

// InvalidHistoryError is runner history that a notice cannot be reconciled with: a gap that may hold a receipt,
// or entries that contradict the ones already copied.
type InvalidHistoryError struct{ Reason string }

func (e *InvalidHistoryError) Error() string { return InvalidHistoryPrefix + ": " + e.Reason }

// noticeText is the text of a notice command. Preparation fixes it once, with the cursors at that moment, so
// a lost response replays the same command. It is `json.dumps` of the three values with compact separators,
// after the prefix.
func noticeText(inboxID uuid.UUID, acknowledged, through int64) string {
	return fmt.Sprintf(`Agentplane inbox notice: {"inbox_id":"%s","acknowledged_at_preparation":%d,"through_at_preparation":%d}`,
		inboxID, acknowledged, through)
}

// noticeRow is a notice with the runner entry its receipts stopped at.
type noticeRow struct {
	Notice
	runnerEntry []byte
}

// lockedNotice is the claimed inbox's notice, which must be the one the caller holds.
func lockedNotice(ctx context.Context, tx *sql.Tx, inboxID, commandID uuid.UUID) (noticeRow, error) {
	var row noticeRow
	var noticeError sql.NullString
	err := tx.QueryRowContext(ctx, `
		SELECT command_id, through_cursor, text, attempted, admitted, confirmed, error, runner_cursor, runner_entry
		FROM notice WHERE inbox_id = $1`, inboxID).Scan(&row.CommandID, &row.ThroughCursor, &row.Text, &row.Attempted,
		&row.Admitted, &row.Confirmed, &noticeError, &row.RunnerCursor, &row.runnerEntry)
	if errors.Is(err, sql.ErrNoRows) {
		return noticeRow{}, fmt.Errorf("inbox %s has no notice", inboxID)
	}
	if err != nil {
		return noticeRow{}, err
	}
	if row.CommandID != commandID {
		return noticeRow{}, fmt.Errorf("inbox %s holds notice %s, not %s", inboxID, row.CommandID, commandID)
	}
	if noticeError.Valid {
		row.Error = &noticeError.String
	}
	return row, nil
}

// CurrentNotice returns the claimed inbox's latest notice in whatever state it is, or nil before the first.
func (s *Store) CurrentNotice(ctx context.Context, claim Claim) (*Notice, error) {
	var notice *Notice
	err := s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) (err error) {
		notice, err = latestNotice(ctx, tx, held.ID)
		return err
	})
	return notice, err
}

// PrepareNotice returns the notice that is in flight (neither confirmed nor failed), or else prepares one that
// names every entry up to the inbox's last, and so replaces the previous notice. It returns nil when no entry
// awaits one. Preparing is not delivering: nothing is sent.
func (s *Store) PrepareNotice(ctx context.Context, claim Claim) (*Notice, error) {
	var prepared *Notice
	err := s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
		current, err := latestNotice(ctx, tx, held.ID)
		if err != nil {
			return err
		}
		if current != nil && !current.Confirmed && current.Error == nil {
			prepared = current
			return nil
		}
		if max(held.Covered, held.Acknowledged, held.ExpiredThrough) >= held.LastCursor {
			return nil
		}
		// Only the latest terminal outcome is kept; what older notices covered stays in `covered`.
		if _, err := tx.ExecContext(ctx, `DELETE FROM notice WHERE inbox_id = $1`, held.ID); err != nil {
			return err
		}
		notice := Notice{
			CommandID: uuid.New(), ThroughCursor: held.LastCursor,
			Text: noticeText(held.ID, held.Acknowledged, held.LastCursor),
		}
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO notice (inbox_id, command_id, through_cursor, text, attempted, admitted, confirmed, runner_cursor)
			VALUES ($1, $2, $3, $4, false, false, false, 0)`,
			held.ID, notice.CommandID, notice.ThroughCursor, notice.Text); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE inbox SET covered = $2 WHERE id = $1`, held.ID, held.LastCursor); err != nil {
			return err
		}
		prepared = &notice
		return notify(ctx, tx)
	})
	return prepared, err
}

// CheckpointBeforeAttempt moves the notice's runner cursor over history that cannot hold its receipts, to
// entry, which it keeps to verify the boundary again after a restart. It is only possible before the first
// attempt to deliver the notice.
func (s *Store) CheckpointBeforeAttempt(ctx context.Context, claim Claim, notice Notice, entry *eventlogpb.EventEntry) error {
	return s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
		row, err := lockedNotice(ctx, tx, held.ID, notice.CommandID)
		if err != nil {
			return err
		}
		if row.Attempted {
			return &InvalidHistoryError{"cannot skip runner history after a delivery attempt"}
		}
		if int64(entry.GetCursor()) <= row.RunnerCursor {
			return &InvalidHistoryError{"initial checkpoint must advance the runner cursor"}
		}
		wire, err := marshalEntry(entry)
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `UPDATE notice SET runner_cursor = $2, runner_entry = $3 WHERE inbox_id = $1`,
			held.ID, entry.GetCursor(), wire)
		return err
	})
}

// marshalEntry serializes an entry as Python's `SerializeToString(deterministic=True)` does, which is what
// `notice.runner_entry` holds and a replayed boundary is compared with.
func marshalEntry(entry *eventlogpb.EventEntry) ([]byte, error) {
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(entry)
	if wire == nil {
		// An empty bytea, not NULL.
		wire = []byte{}
	}
	return wire, err
}

// Attempt marks the notice as about to be submitted, which it must be before the first submission. It returns
// false, and ends the notice with an error instead, when the agent has acknowledged the entries it names
// before any attempt: nothing is left to tell it.
func (s *Store) Attempt(ctx context.Context, claim Claim, notice Notice) (bool, error) {
	var attempt bool
	err := s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
		row, err := lockedNotice(ctx, tx, held.ID, notice.CommandID)
		if err != nil {
			return err
		}
		if !row.Attempted && max(held.Acknowledged, held.ExpiredThrough) >= row.ThroughCursor {
			if _, err := tx.ExecContext(ctx, `UPDATE notice SET error = 'suppressed: acknowledged before submission' WHERE inbox_id = $1`,
				held.ID); err != nil {
				return err
			}
			return notify(ctx, tx)
		}
		if _, err := tx.ExecContext(ctx, `UPDATE notice SET attempted = true WHERE inbox_id = $1`, held.ID); err != nil {
			return err
		}
		attempt = true
		return notify(ctx, tx)
	})
	return attempt, err
}

// DeliveryFailed ends the notice with a terminal outcome from the sender's side. No other notice is prepared
// for the entries it covers.
func (s *Store) DeliveryFailed(ctx context.Context, claim Claim, notice Notice, message string) error {
	return s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
		if _, err := lockedNotice(ctx, tx, held.ID, notice.CommandID); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE notice SET error = $2 WHERE inbox_id = $1`, held.ID, message); err != nil {
			return err
		}
		return notify(ctx, tx)
	})
}

// Receipt copies a runner history entry into the notice: whether the runner admitted its command, the harness
// confirmed it, or the runner failed it. entry follows afterCursor, with only settled cursors (deltas the
// History Service dropped) between them. Replaying the entry the notice stopped at changes nothing, and
// fails with *InvalidHistoryError if History now holds a different entry there; so does an entry that does not
// follow the notice's runner cursor.
func (s *Store) Receipt(ctx context.Context, claim Claim, notice Notice, entry *eventlogpb.EventEntry, afterCursor int64) error {
	return s.fencedTransaction(ctx, claim, func(tx *sql.Tx, held heldInbox) error {
		row, err := lockedNotice(ctx, tx, held.ID, notice.CommandID)
		if err != nil {
			return err
		}
		wire, err := marshalEntry(entry)
		if err != nil {
			return err
		}
		cursor := int64(entry.GetCursor())
		if cursor == row.RunnerCursor && row.RunnerCursor != 0 {
			// TODO: Compare decoded entries semantically with an explicit policy for unknown and newly
			// added fields; deterministic protobuf bytes are not canonical across builds or languages.
			if !bytes.Equal(row.runnerEntry, wire) {
				return &InvalidHistoryError{"runner history changed at the committed boundary"}
			}
			return nil
		}
		if afterCursor > row.RunnerCursor || row.RunnerCursor >= cursor {
			return &InvalidHistoryError{"runner history has a gap"}
		}
		commandID := row.CommandID.String()
		event := entry.GetEvent()
		if admitted := event.GetCommandAdmitted(); admitted.GetCommand().GetCommandId() == commandID {
			if admitted.GetCommand().GetSubmitInput().GetText() != row.Text {
				return &InvalidHistoryError{"runner admitted different notice content"}
			}
			row.Admitted = true
		}
		if slices.Contains(event.GetHarnessUserMessageConfirmed().GetOriginCommandIds(), commandID) {
			row.Confirmed = true
		}
		if event.GetCommandFailed().GetCommandId() == commandID {
			failure := "runner command failed; inspect native runner evidence"
			row.Error = &failure
		}
		if event.GetCommandNoop().GetCommandId() == commandID {
			noop := "runner command was a no-op; not confirmed"
			row.Error = &noop
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE notice SET admitted = $2, confirmed = $3, error = $4, runner_cursor = $5, runner_entry = $6
			WHERE inbox_id = $1`, held.ID, row.Admitted, row.Confirmed, row.Error, cursor, wire); err != nil {
			return err
		}
		return notify(ctx, tx)
	})
}
