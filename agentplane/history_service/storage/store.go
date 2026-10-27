// Package storage keeps each Session's committed runner Event prefix, with the settlement and
// removal records that account for entries no longer stored.
package storage

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"math"
	"slices"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

const (
	// ChannelHistory is notified when raw Session history committed.
	ChannelHistory = "agentplane_session_history_changes"
	// ChannelSessions is notified when a Session was recorded.
	ChannelSessions = "agentplane_history_session_openings"

	// openingLock is held from drawing a Session's opening position to commit, so positions become
	// visible in order.
	openingLock = 0x534F504E
)

var (
	// ErrConflict means an entry or snapshot skips or disagrees with what is already stored.
	ErrConflict = errors.New("history conflict")
	// ErrNotFound means the Session has no history.
	ErrNotFound = errors.New("session history not found")
	// ErrWriterMismatch means the Session is bound to another writer.
	ErrWriterMismatch = errors.New("session is bound to another writer")
	// ErrSandboxMismatch means the Session is pinned to another Sandbox incarnation.
	ErrSandboxMismatch = errors.New("session is bound to another sandbox")
	// ErrInvalidArgument means the request names a page the store cannot serve.
	ErrInvalidArgument = errors.New("invalid session history request")
)

// Writer is the ServiceAccount a Session's ingress pushes come from; the UID tells apart an account
// recreated under the same name.
type Writer struct{ Namespace, Name, UID string }

type Checkpoint struct {
	Cursor uint64
	// Seal is the seal at Cursor, once committed: nothing more is accepted from its own incarnation.
	Seal *runnerpb.Sealed
}

type OpenedSession struct {
	SessionID uuid.UUID
	Position  uint64
}

type Observation struct {
	Cursor uint64
	Kind   string
}

type CursorRange struct{ First, Last uint64 }

type ObservationPage struct {
	LastCursor   uint64
	Observations []Observation
	// Settled holds the settled and removed cursor ranges intersecting the page's window.
	Settled []CursorRange
}

type Store struct {
	db           *sql.DB
	settleDeltas bool
}

// NewStore returns a store over db. With settleDeltas, Append removes an item's streamed delta entries
// once its completion provably holds their concatenation; only the runner ingress sets it.
func NewStore(db *sql.DB, settleDeltas bool) *Store {
	return &Store{db: db, settleDeltas: settleDeltas}
}

func transaction(ctx context.Context, db *sql.DB, opts *sql.TxOptions, f func(*sql.Tx) error) error {
	tx, err := db.BeginTx(ctx, opts)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := f(tx); err != nil {
		return err
	}
	return tx.Commit()
}

// snapshot is one read-only snapshot: settlement deletes entries and adds their range in one commit,
// so a page read across that commit could pair entries with the range that removed them.
var snapshot = &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true}

func notify(ctx context.Context, tx *sql.Tx, channel string) error {
	_, err := tx.ExecContext(ctx, "SELECT pg_notify($1, '')", channel)
	return err
}

func conflict(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrConflict, fmt.Sprintf(format, args...))
}

func invalid(detail string) error { return fmt.Errorf("%w: %s", ErrInvalidArgument, detail) }

func marshal(message proto.Message) ([]byte, error) {
	return proto.MarshalOptions{Deterministic: true}.Marshal(message)
}

type historyRow struct {
	sourceID   *string
	lastCursor uint64
	feedState  []byte
	writer     *Writer
	sandboxUID *string
}

// lockRow reads and locks the Session's history row.
func lockRow(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID) (*historyRow, error) {
	var row historyRow
	var namespace, name, uid *string
	err := tx.QueryRowContext(ctx, `SELECT source_id, last_cursor, feed_state, writer_namespace, writer_name, writer_uid, sandbox_uid
		FROM session_history WHERE id = $1 FOR UPDATE`, sessionID).
		Scan(&row.sourceID, &row.lastCursor, &row.feedState, &namespace, &name, &uid, &row.sandboxUID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, fmt.Errorf("%w: %s", ErrNotFound, sessionID)
	}
	if err != nil {
		return nil, err
	}
	if uid != nil {
		row.writer = &Writer{Namespace: *namespace, Name: *name, UID: *uid}
	}
	return &row, nil
}

func (row *historyRow) checkpoint() (Checkpoint, error) {
	checkpoint := Checkpoint{Cursor: row.lastCursor}
	if row.feedState == nil {
		return checkpoint, nil
	}
	var feed historypb.SessionFeedState
	if err := proto.Unmarshal(row.feedState, &feed); err != nil {
		return Checkpoint{}, err
	}
	checkpoint.Seal = feed.GetSealed()
	return checkpoint, nil
}

// BindWriter binds the Session to writer if it has no writer yet, creating its row; any other writer
// is refused. Of two concurrent first writers, the row lock lets one bind and refuses the other.
func (s *Store) BindWriter(ctx context.Context, sessionID uuid.UUID, writer Writer, sandboxUID string) (Checkpoint, error) {
	var checkpoint Checkpoint
	err := transaction(ctx, s.db, nil, func(tx *sql.Tx) error {
		created := false
		changed := false
		var sandboxUIDValue any
		if sandboxUID != "" {
			sandboxUIDValue = sandboxUID
		}
		var exists bool
		if err := tx.QueryRowContext(ctx, "SELECT EXISTS (SELECT 1 FROM session_history WHERE id = $1)", sessionID).Scan(&exists); err != nil {
			return err
		}
		// Only a first push takes the opening lock, so a reconnecting runner never waits on another's.
		if !exists {
			if _, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock($1)", openingLock); err != nil {
				return err
			}
			result, err := tx.ExecContext(ctx, `INSERT INTO session_history
				(id, writer_namespace, writer_name, writer_uid, sandbox_namespace, sandbox_name, sandbox_uid, source_id, last_cursor)
				VALUES ($1, $2, $3, $4, $2, $3, $5, NULL, 0) ON CONFLICT DO NOTHING`,
				sessionID, writer.Namespace, writer.Name, writer.UID, sandboxUIDValue)
			if err != nil {
				return err
			}
			inserted, err := result.RowsAffected()
			if err != nil {
				return err
			}
			created = inserted == 1
		}
		row, err := lockRow(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		switch {
		case row.writer == nil:
			if _, err := tx.ExecContext(ctx, `UPDATE session_history SET writer_namespace = $2, writer_name = $3, writer_uid = $4,
				sandbox_namespace = COALESCE(sandbox_namespace, $2), sandbox_name = COALESCE(sandbox_name, $3), sandbox_uid = $5
				WHERE id = $1`, sessionID, writer.Namespace, writer.Name, writer.UID, sandboxUIDValue); err != nil {
				return err
			}
			row.sandboxUID = optionalString(sandboxUID)
			changed = true
		case *row.writer != writer:
			return fmt.Errorf("%w: %s", ErrWriterMismatch, sessionID)
		}
		if row.writer != nil && *row.writer == writer {
			switch {
			case row.sandboxUID == nil && sandboxUID != "":
				if _, err := tx.ExecContext(ctx, `UPDATE session_history SET sandbox_uid = $2 WHERE id = $1`, sessionID, sandboxUID); err != nil {
					return err
				}
				row.sandboxUID = optionalString(sandboxUID)
				changed = true
			case row.sandboxUID != nil && *row.sandboxUID != sandboxUID:
				return fmt.Errorf("%w: %s", ErrSandboxMismatch, sessionID)
			}
		}
		if created {
			if err := notify(ctx, tx, ChannelHistory); err != nil {
				return err
			}
			if err := notify(ctx, tx, ChannelSessions); err != nil {
				return err
			}
		} else if changed {
			if err := notify(ctx, tx, ChannelHistory); err != nil {
				return err
			}
		}
		checkpoint, err = row.checkpoint()
		return err
	})
	return checkpoint, err
}

func optionalString(value string) *string {
	if value == "" {
		return nil
	}
	return &value
}

// OpenedSessions returns up to limit Sessions recorded after afterPosition, in recording order.
func (s *Store) OpenedSessions(ctx context.Context, afterPosition uint64, limit int) ([]OpenedSession, error) {
	if afterPosition > math.MaxInt64 || limit < 1 || limit > 1000 {
		return nil, invalid("opened Session page")
	}
	rows, err := s.db.QueryContext(ctx, `SELECT id, opened_position FROM session_history
		WHERE opened_position > $1 ORDER BY opened_position LIMIT $2`, afterPosition, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var opened []OpenedSession
	for rows.Next() {
		var session OpenedSession
		if err := rows.Scan(&session.SessionID, &session.Position); err != nil {
			return nil, err
		}
		opened = append(opened, session)
	}
	return opened, rows.Err()
}

// Append replays exact duplicates or extends the prefix, serializing concurrent writers by Session ID,
// and returns the Session's last cursor. Validation, settlement of the items the batch completes, and
// the new checkpoint commit together; on any error the entire batch rolls back. An empty batch is a checkpoint read, not an inferred runner high-water mark.
func (s *Store) Append(ctx context.Context, sessionID uuid.UUID, entries []*eventlogpb.EventEntry) (uint64, error) {
	var lastCursor uint64
	err := transaction(ctx, s.db, nil, func(tx *sql.Tx) error {
		row, err := lockRow(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		var appended []*eventlogpb.EventEntry
		for _, entry := range entries {
			cursor, sourceID := entry.GetCursor(), entry.GetOrigin().GetSourceId()
			if cursor == 0 || sourceID == "" || entry.GetOrigin().GetSequence() != cursor {
				return conflict("invalid runner origin at %d", cursor)
			}
			if row.sourceID != nil && *row.sourceID != sourceID {
				return conflict("source changed at %d", cursor)
			}
			if cursor <= row.lastCursor {
				if err := checkReplay(ctx, tx, sessionID, entry); err != nil {
					return err
				}
				continue
			}
			if cursor != row.lastCursor+1 {
				return conflict("expected %d, received %d", row.lastCursor+1, cursor)
			}
			payload, err := marshal(entry)
			if err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, "INSERT INTO session_event (session_id, cursor, payload, kind) VALUES ($1, $2, $3, $4)",
				sessionID, cursor, payload, observationNumber(entry)); err != nil {
				return err
			}
			row.sourceID, row.lastCursor, appended = &sourceID, cursor, append(appended, entry)
		}
		if s.settleDeltas {
			for _, entry := range appended {
				if err := settleCompletion(ctx, tx, sessionID, entry); err != nil {
					return err
				}
			}
		}
		if len(appended) > 0 {
			if _, err := tx.ExecContext(ctx, "UPDATE session_history SET source_id = $2, last_cursor = $3 WHERE id = $1",
				sessionID, *row.sourceID, row.lastCursor); err != nil {
				return err
			}
			if err := notify(ctx, tx, ChannelHistory); err != nil {
				return err
			}
		}
		lastCursor = row.lastCursor
		return nil
	})
	return lastCursor, err
}

// checkReplay accepts an entry at or below the stored prefix when it equals what is stored, or when its
// cursor was settled or removed and its exact payload is no longer stored to compare.
func checkReplay(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, entry *eventlogpb.EventEntry) error {
	var payload []byte
	err := tx.QueryRowContext(ctx, "SELECT payload FROM session_event WHERE session_id = $1 AND cursor = $2",
		sessionID, entry.GetCursor()).Scan(&payload)
	if errors.Is(err, sql.ErrNoRows) {
		unstored, err := isUnstored(ctx, tx, sessionID, entry.GetCursor())
		if err != nil {
			return err
		}
		if unstored {
			return nil
		}
		return conflict("conflicting entry at %d", entry.GetCursor())
	}
	if err != nil {
		return err
	}
	var stored eventlogpb.EventEntry
	if err := proto.Unmarshal(payload, &stored); err != nil {
		return err
	}
	if !proto.Equal(&stored, entry) {
		return conflict("conflicting entry at %d", entry.GetCursor())
	}
	return nil
}

// isUnstored reports whether a settlement or a cleanup deleted the entry at cursor.
func isUnstored(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, cursor uint64) (bool, error) {
	var unstored bool
	err := tx.QueryRowContext(ctx, `SELECT EXISTS (
			SELECT 1 FROM session_event_settled_range WHERE session_id = $1 AND first_cursor <= $2 AND last_cursor >= $2
		) OR EXISTS (
			SELECT 1 FROM session_event_removed_range WHERE session_id = $1 AND first_cursor <= $2 AND last_cursor >= $2
		)`, sessionID, cursor).Scan(&unstored)
	return unstored, err
}

// ReopenSealed drops the seal of an earlier incarnation so incarnationUID can continue the journal.
//
// Only a seal that names a different incarnation (or none) is dropped, and only when it ends the
// committed history and the announced snapshot is not behind it; the same incarnation's seal is left as
// it is, which also makes a repeat a no-op.
func (s *Store) ReopenSealed(ctx context.Context, sessionID uuid.UUID, incarnationUID string, announcedCursor uint64) (Checkpoint, error) {
	var checkpoint Checkpoint
	err := transaction(ctx, s.db, nil, func(tx *sql.Tx) error {
		row, err := lockRow(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		if checkpoint, err = row.checkpoint(); err != nil {
			return err
		}
		seal := checkpoint.Seal
		if seal == nil || (seal.IncarnationUid != nil && seal.GetIncarnationUid() == incarnationUID) {
			return nil
		}
		if seal.GetCursor() != row.lastCursor || announcedCursor < seal.GetCursor() {
			return conflict("the continuation does not follow the seal")
		}
		var feed historypb.SessionFeedState
		if err := proto.Unmarshal(row.feedState, &feed); err != nil {
			return err
		}
		feed.Sealed = nil
		if row.feedState, err = marshal(&feed); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, "UPDATE session_history SET feed_state = $2 WHERE id = $1", sessionID, row.feedState); err != nil {
			return err
		}
		if err := notify(ctx, tx, ChannelHistory); err != nil {
			return err
		}
		checkpoint, err = row.checkpoint()
		return err
	})
	return checkpoint, err
}

// RecordFeedState records only a covered runner snapshot; delayed copiers cannot rewind it or drop a seal.
func (s *Store) RecordFeedState(ctx context.Context, sessionID uuid.UUID, feed *historypb.SessionFeedState) error {
	return transaction(ctx, s.db, nil, func(tx *sql.Tx) error {
		row, err := lockRow(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		next := proto.Clone(feed).(*historypb.SessionFeedState)
		if next.GetAttached() == nil || next.GetAttached().GetSessionId() != sessionID.String() {
			return conflict("feed snapshot does not match the bound runner session")
		}
		if next.GetAttached().GetLastCursor() > row.lastCursor {
			return conflict("feed snapshot is ahead of committed history")
		}
		if next.GetSealed() != nil && next.GetSealed().GetCursor() != row.lastCursor {
			return conflict("seal does not end the committed history")
		}
		if row.feedState != nil {
			var prior historypb.SessionFeedState
			if err := proto.Unmarshal(row.feedState, &prior); err != nil {
				return err
			}
			if err := mergeFeedState(next, &prior); err != nil {
				return err
			}
		}
		serialized, err := marshal(next)
		if err != nil {
			return err
		}
		if bytes.Equal(row.feedState, serialized) {
			return nil
		}
		if _, err := tx.ExecContext(ctx, "UPDATE session_history SET feed_state = $2 WHERE id = $1", sessionID, serialized); err != nil {
			return err
		}
		return notify(ctx, tx, ChannelHistory)
	})
}

// mergeFeedState folds the stored snapshot prior into next: a stored seal is sticky, and an older or
// same-cursor snapshot cannot replace a newer one.
func mergeFeedState(next, prior *historypb.SessionFeedState) error {
	if prior.GetSealed() != nil {
		if next.GetSealed() != nil && !proto.Equal(next.GetSealed(), prior.GetSealed()) {
			return conflict("conflicting seals")
		}
		next.Sealed = proto.Clone(prior.GetSealed()).(*runnerpb.Sealed)
	}
	switch priorCursor := prior.GetAttached().GetLastCursor(); {
	case next.GetAttached().GetLastCursor() < priorCursor:
		next.Attached = proto.Clone(prior.GetAttached()).(*runnerpb.Attached)
	case next.GetAttached().GetLastCursor() == priorCursor && !proto.Equal(next.GetAttached(), prior.GetAttached()):
		sameExceptHarness := proto.Clone(prior.GetAttached()).(*runnerpb.Attached)
		sameExceptHarness.HarnessState = next.GetAttached().GetHarnessState()
		// A later incarnation announces again at the cursor the earlier one ended at.
		sameExceptHarness.IncarnationUid = next.GetAttached().IncarnationUid
		if !proto.Equal(sameExceptHarness, next.GetAttached()) {
			return conflict("conflicting attachment snapshots at one cursor")
		}
		nextState, priorState := next.GetAttached().GetHarnessState(), prior.GetAttached().GetHarnessState()
		switch {
		case nextState == priorState:
		case prior.GetSealed() == nil && priorState == runnerpb.HarnessState_HARNESS_STATE_STOPPED &&
			nextState == runnerpb.HarnessState_HARNESS_STATE_RUNNING:
			// Resume can be confirmed before HarnessStarted advances the journal.
		case priorState == runnerpb.HarnessState_HARNESS_STATE_RUNNING &&
			nextState == runnerpb.HarnessState_HARNESS_STATE_STOPPED:
			// Stops have journal entries. A stopped snapshot at the same cursor after Resume belongs to
			// the previous run.
			next.Attached = proto.Clone(prior.GetAttached()).(*runnerpb.Attached)
		default:
			return conflict("conflicting attachment snapshots at one cursor")
		}
	}
	return nil
}

// ReadPage reads the prefix watermark and lifecycle from the same history-row snapshot.
func (s *Store) ReadPage(ctx context.Context, sessionID uuid.UUID, afterCursor uint64, limit int) (*historypb.ReadSessionEventsResponse, error) {
	if limit < 1 || limit > 1000 {
		return nil, invalid("history page")
	}
	var page *historypb.ReadSessionEventsResponse
	err := transaction(ctx, s.db, snapshot, func(tx *sql.Tx) error {
		var lastCursor uint64
		var feedState []byte
		err := tx.QueryRowContext(ctx, "SELECT last_cursor, feed_state FROM session_history WHERE id = $1", sessionID).
			Scan(&lastCursor, &feedState)
		if errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("%w: %s", ErrNotFound, sessionID)
		}
		if err != nil {
			return err
		}
		if afterCursor > lastCursor {
			return invalid("cursor beyond stored prefix")
		}
		page = &historypb.ReadSessionEventsResponse{LastCursor: lastCursor}
		through := afterCursor
		rows, err := tx.QueryContext(ctx, `SELECT cursor, payload FROM session_event
			WHERE session_id = $1 AND cursor > $2 AND cursor <= $3 ORDER BY cursor LIMIT $4`,
			sessionID, afterCursor, lastCursor, limit)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var payload []byte
			if err := rows.Scan(&through, &payload); err != nil {
				return err
			}
			entry := &eventlogpb.EventEntry{}
			if err := proto.Unmarshal(payload, entry); err != nil {
				return err
			}
			page.Entries = append(page.Entries, entry)
		}
		if err := rows.Err(); err != nil {
			return err
		}
		if page.Settlements, err = readSettlements(ctx, tx, sessionID, afterCursor, through); err != nil {
			return err
		}
		// A short page reaches the end of the prefix, where the last cursors may be removed ones.
		removalWindowEnd := lastCursor
		if len(page.Entries) == limit {
			removalWindowEnd = through
		}
		removed, err := readRanges(ctx, tx, `SELECT first_cursor, last_cursor FROM session_event_removed_range
			WHERE session_id = $1 AND first_cursor <= $3 AND last_cursor > $2 ORDER BY first_cursor`,
			sessionID, afterCursor, removalWindowEnd)
		if err != nil {
			return err
		}
		for _, cursorRange := range removed {
			page.Removed = append(page.Removed, &historypb.CursorRange{First: cursorRange.First, Last: cursorRange.Last})
		}
		if feedState != nil {
			page.FeedState = &historypb.SessionFeedState{}
			return proto.Unmarshal(feedState, page.FeedState)
		}
		return nil
	})
	return page, err
}

// readSettlements returns every settlement whose completion entry or settled range falls in the window
// after afterCursor and through through, in completion order.
func readSettlements(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, afterCursor, through uint64) ([]*historypb.SettledDeltas, error) {
	rows, err := tx.QueryContext(ctx, `SELECT payload FROM session_event_settlement
		WHERE session_id = $1 AND completion_cursor IN (
			SELECT completion_cursor FROM session_event_settled_range
				WHERE session_id = $1 AND first_cursor <= $3 AND last_cursor > $2
			UNION
			SELECT completion_cursor FROM session_event_settlement
				WHERE session_id = $1 AND completion_cursor > $2 AND completion_cursor <= $3)
		ORDER BY completion_cursor`, sessionID, afterCursor, through)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var settlements []*historypb.SettledDeltas
	for rows.Next() {
		var payload []byte
		if err := rows.Scan(&payload); err != nil {
			return nil, err
		}
		settled := &historypb.SettledDeltas{}
		if err := proto.Unmarshal(payload, settled); err != nil {
			return nil, err
		}
		settlements = append(settlements, settled)
	}
	return settlements, rows.Err()
}

func readRanges(ctx context.Context, tx *sql.Tx, query string, args ...any) ([]CursorRange, error) {
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ranges []CursorRange
	for rows.Next() {
		var cursorRange CursorRange
		if err := rows.Scan(&cursorRange.First, &cursorRange.Last); err != nil {
			return nil, err
		}
		ranges = append(ranges, cursorRange)
	}
	return ranges, rows.Err()
}

// ReadObservations seeks a bounded metadata window in the retained prefix, including deleted Sandboxes.
// Neither bound selects the tail; observations ascend by cursor.
func (s *Store) ReadObservations(ctx context.Context, sessionID uuid.UUID, before, after *uint64, limit int) (ObservationPage, error) {
	if limit < 1 || limit > 200 || (before != nil && after != nil) ||
		(before != nil && *before > math.MaxInt64) || (after != nil && *after > math.MaxInt64) {
		return ObservationPage{}, invalid("observation page")
	}
	var page ObservationPage
	err := transaction(ctx, s.db, snapshot, func(tx *sql.Tx) error {
		err := tx.QueryRowContext(ctx, "SELECT last_cursor FROM session_history WHERE id = $1", sessionID).Scan(&page.LastCursor)
		if errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("%w: %s", ErrNotFound, sessionID)
		}
		if err != nil {
			return err
		}
		query := `SELECT cursor, payload FROM session_event WHERE session_id = $1 AND cursor <= $2`
		args := []any{sessionID, page.LastCursor}
		switch {
		case after != nil:
			query += " AND cursor > $3 ORDER BY cursor LIMIT $4"
			args = append(args, *after, limit)
		case before != nil:
			query += " AND cursor < $3 ORDER BY cursor DESC LIMIT $4"
			args = append(args, *before, limit)
		default:
			query += " ORDER BY cursor DESC LIMIT $3"
			args = append(args, limit)
		}
		if page.Observations, err = readObservations(ctx, tx, query, args...); err != nil {
			return err
		}
		if after == nil {
			slices.Reverse(page.Observations)
		}
		// The window runs from the requested bound to the far end of what was returned, or for a short
		// page forward to the end of the prefix, where the last cursors may be removed ones. Signed
		// because a window below the first cursor is empty rather than wrapped.
		observations, last := page.Observations, int64(page.LastCursor)
		var low, high int64
		if after != nil {
			low, high = int64(*after)+1, last
			if len(observations) == limit {
				high = int64(observations[limit-1].Cursor)
			}
		} else {
			high = last
			if before != nil {
				high = min(last, int64(*before)-1)
			}
			low = high + 1
			if len(observations) > 0 {
				low = int64(observations[0].Cursor)
			}
		}
		ranges, err := readRanges(ctx, tx, `SELECT first_cursor, last_cursor FROM (
				SELECT first_cursor, last_cursor FROM session_event_settled_range
					WHERE session_id = $1 AND first_cursor <= $2 AND last_cursor >= $3
				UNION ALL
				SELECT first_cursor, last_cursor FROM session_event_removed_range
					WHERE session_id = $1 AND first_cursor <= $2 AND last_cursor >= $3) ranges
			ORDER BY first_cursor, last_cursor`, sessionID, high, low)
		page.Settled = ranges
		return err
	})
	return page, err
}

func readObservations(ctx context.Context, tx *sql.Tx, query string, args ...any) ([]Observation, error) {
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var observations []Observation
	for rows.Next() {
		var cursor uint64
		var payload []byte
		if err := rows.Scan(&cursor, &payload); err != nil {
			return nil, err
		}
		var entry eventlogpb.EventEntry
		if err := proto.Unmarshal(payload, &entry); err != nil {
			return nil, err
		}
		observations = append(observations, Observation{Cursor: cursor, Kind: observationKind(&entry)})
	}
	return observations, rows.Err()
}

// observationKind names the Event's observation, or is empty when it carries none.
func observationKind(entry *eventlogpb.EventEntry) string {
	event := entry.GetEvent().ProtoReflect()
	if field := event.WhichOneof(event.Descriptor().Oneofs().ByName("observation")); field != nil {
		return string(field.Name())
	}
	return ""
}

// observationNumber stores the Event.observation field number for FollowSession's indexed reads.
// Zero represents an entry without an observation.
func observationNumber(entry *eventlogpb.EventEntry) int32 {
	event := entry.GetEvent().ProtoReflect()
	if field := event.WhichOneof(event.Descriptor().Oneofs().ByName("observation")); field != nil {
		return int32(field.Number())
	}
	return 0
}

// settlementSpanLimit bounds how far back an item's start may lie before the item is kept whole rather
// than read in one transaction.
const settlementSpanLimit = 20_000

// settleCompletion settles the item field the entry completes, if it completes one and its stored span
// qualifies: it stores the settlement and deletes the entries in its ranges.
func settleCompletion(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, completion *eventlogpb.EventEntry) error {
	item := completionTarget(completion)
	if item == nil {
		return nil
	}
	span := []*eventlogpb.EventEntry{completion}
	var floor uint64
	started := false
	for upper := completion.GetCursor(); !started || upper > floor; {
		batch, err := entriesBefore(ctx, tx, sessionID, upper, 256)
		if err != nil {
			return err
		}
		if len(batch) == 0 || len(span)+len(batch) > settlementSpanLimit {
			return nil
		}
		for _, stored := range batch {
			if started && stored.cursor < floor {
				break
			}
			span = append(span, stored.entry)
			if id, isStart := startedItem(stored.entry); !started && isStart && id == item.itemID {
				// The item's native start frame precedes its derived ItemStarted.
				floor, started = slices.Min(append([]uint64{stored.cursor}, stored.entry.GetEvent().GetSourceSequences()...)), true
			}
		}
		upper = batch[len(batch)-1].cursor
	}
	slices.Reverse(span)
	if settled := settle(span); settled != nil {
		return recordSettlement(ctx, tx, sessionID, settled)
	}
	return nil
}

type storedEntry struct {
	cursor uint64
	entry  *eventlogpb.EventEntry
}

// entriesBefore returns up to limit stored entries below cursor, nearest first.
func entriesBefore(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, cursor uint64, limit int) ([]storedEntry, error) {
	rows, err := tx.QueryContext(ctx, `SELECT cursor, payload FROM session_event
		WHERE session_id = $1 AND cursor < $2 ORDER BY cursor DESC LIMIT $3`, sessionID, cursor, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var batch []storedEntry
	for rows.Next() {
		var stored storedEntry
		var payload []byte
		if err := rows.Scan(&stored.cursor, &payload); err != nil {
			return nil, err
		}
		stored.entry = &eventlogpb.EventEntry{}
		if err := proto.Unmarshal(payload, stored.entry); err != nil {
			return nil, err
		}
		batch = append(batch, stored)
	}
	return batch, rows.Err()
}

// recordSettlement stores settled and deletes the entries in its ranges.
func recordSettlement(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, settled *historypb.SettledDeltas) error {
	payload, err := marshal(settled)
	if err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "INSERT INTO session_event_settlement (session_id, completion_cursor, payload) VALUES ($1, $2, $3)",
		sessionID, settled.GetCompletionCursor(), payload); err != nil {
		return err
	}
	for _, cursorRange := range settled.GetRanges() {
		if _, err := tx.ExecContext(ctx, `INSERT INTO session_event_settled_range (session_id, first_cursor, last_cursor, completion_cursor)
			VALUES ($1, $2, $3, $4)`, sessionID, cursorRange.GetFirst(), cursorRange.GetLast(), settled.GetCompletionCursor()); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, "DELETE FROM session_event WHERE session_id = $1 AND cursor >= $2 AND cursor <= $3",
			sessionID, cursorRange.GetFirst(), cursorRange.GetLast()); err != nil {
			return err
		}
	}
	return nil
}
