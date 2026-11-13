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

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

const (
	// ChannelHistory is notified when raw Session history committed.
	ChannelHistory = "agentplane_session_history_changes"
	// ChannelSessions is notified when a Session was recorded.
	ChannelSessions = "agentplane_history_sessions"

	// sessionLock is held from drawing a Session's session position to commit, so positions become
	// visible in order.
	sessionLock = 0x534F504E
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
	// ErrExecutionIdentityMismatch means a Session's immutable execution Identity is absent or differs.
	ErrExecutionIdentityMismatch = errors.New("session execution Identity does not match retained identity")
	// ErrInvalidArgument means the request names a page the store cannot serve.
	ErrInvalidArgument = errors.New("invalid session history request")
)

// Writer is who a Session's ingress pushes come from: a ServiceAccountWriter or an IdentityWriter.
// Each UID tells apart a subject recreated under the same name.
type Writer interface{ isWriter() }

// ServiceAccountWriter is a runner or central sampler authenticated as a ServiceAccount.
type ServiceAccountWriter struct{ Namespace, Name, UID string }

// IdentityWriter is a runner whose workload credential names an Identity incarnation.
type IdentityWriter struct{ Identity }

func (ServiceAccountWriter) isWriter() {}
func (IdentityWriter) isWriter()       {}

// Identity is an Identity incarnation in the policy namespace.
type Identity struct{ Name, UID string }

func (i Identity) valid() bool { return i.Name != "" && i.UID != "" }

// Sandbox is the Sandbox incarnation a Session executes in. UID is empty for a legacy writer whose
// ServiceAccount has no Sandbox controller.
type Sandbox struct{ Namespace, Name, UID string }

// ReadScope limits retained reads to one immutable execution Identity, or
// explicitly to all Sessions for an existing Pod-bound backend reader. The zero value denies every
// Session.
type ReadScope struct {
	identity *Identity
	all      bool
}

// OwnedByIdentity returns a scope for the Sessions recorded as executing as exactly one Identity
// incarnation. An incomplete identity yields a scope that denies reads.
func OwnedByIdentity(identity Identity) ReadScope {
	if !identity.valid() {
		return ReadScope{}
	}
	copy := identity
	return ReadScope{identity: &copy}
}

// AllSessions returns the explicit unfiltered scope used only by configured Pod-bound backend readers.
func AllSessions() ReadScope { return ReadScope{all: true} }

// Predicate returns a SQL predicate and its parameters for a session_history alias. The alias is a
// source constant, never request data. firstArgument is the first free PostgreSQL bind number.
func (scope ReadScope) Predicate(alias string, firstArgument int) (string, []any) {
	if scope.all {
		return "TRUE", nil
	}
	if firstArgument >= 1 && scope.identity != nil && scope.identity.valid() {
		return fmt.Sprintf("%s.execution_identity_name = $%d AND %s.execution_identity_uid = $%d",
				alias, firstArgument, alias, firstArgument+1),
			[]any{scope.identity.Name, scope.identity.UID}
	}
	return "FALSE", nil
}

type Checkpoint struct {
	Cursor uint64
	// Seal is the seal at Cursor, once committed: nothing more is accepted from its own incarnation.
	Seal *sessionpb.Sealed
}

type RecordedSession struct {
	SessionID uuid.UUID
	Position  uint64
}

type Observation struct {
	Cursor uint64
	Kind   historypb.EventKind
}

type CursorRange struct{ First, Last uint64 }

type ObservationPage struct {
	LastCursor   uint64
	Observations []Observation
	// Settled holds the settled and removed cursor ranges intersecting the page's window.
	Settled []CursorRange
}

type SequenceRead struct {
	Sequence  uint64
	NextEntry *eventlogpb.EventEntry
	Unstored  bool
}

type SequenceReadPage struct {
	LastCursor uint64
	Reads      []SequenceRead
}

type Store struct {
	db        *sql.DB
	options   Options
	readScope *ReadScope
}

// Options controls writes only. Every reader supports packed entries regardless of these flags.
type Options struct {
	SettleDeltas  bool
	CompactDeltas bool
}

func NewStore(db *sql.DB, options Options) *Store {
	return &Store{db: db, options: options}
}

// WithReadScope returns a read-only view whose SQL reads are scoped before they page or project.
func (s *Store) WithReadScope(scope ReadScope) *Store {
	copy := *s
	copy.readScope = &scope
	return &copy
}

func (s *Store) sessionPredicate(alias string, firstArgument int) (string, []any) {
	if s.readScope == nil {
		return "TRUE", nil
	}
	return s.readScope.Predicate(alias, firstArgument)
}

// snapshot is one read-only snapshot: settlement deletes entries and adds their range in one commit,
// so a page read across that commit could pair entries with the range that removed them.
var snapshot = sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true}

func notify(ctx context.Context, tx *sql.Tx, channel string) error {
	return postgres.Notify(ctx, tx, channel, "")
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
	// writer is nil while no push has bound the Session.
	writer            Writer
	sandboxUID        *string
	executionIdentity *Identity
}

// lockRow reads and locks the Session's history row.
func lockRow(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID) (*historyRow, error) {
	var row historyRow
	var namespace, name, uid *string
	var identityName, identityUID, executionIdentityName, executionIdentityUID *string
	err := tx.QueryRowContext(ctx, `SELECT source_id, last_cursor, feed_state, writer_namespace, writer_name, writer_uid, sandbox_uid,
		writer_identity_name, writer_identity_uid, execution_identity_name, execution_identity_uid
		FROM session_history WHERE id = $1 FOR UPDATE`, sessionID).
		Scan(&row.sourceID, &row.lastCursor, &row.feedState, &namespace, &name, &uid, &row.sandboxUID,
			&identityName, &identityUID, &executionIdentityName, &executionIdentityUID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, fmt.Errorf("%w: %s", ErrNotFound, sessionID)
	}
	if err != nil {
		return nil, err
	}
	// history_writer_one_kind keeps at most one writer kind per row.
	switch {
	case uid != nil:
		row.writer = ServiceAccountWriter{Namespace: *namespace, Name: *name, UID: *uid}
	case identityUID != nil:
		row.writer = IdentityWriter{Identity{Name: *identityName, UID: *identityUID}}
	}
	if executionIdentityUID != nil {
		row.executionIdentity = &Identity{Name: *executionIdentityName, UID: *executionIdentityUID}
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

// BindWriter binds the Session to a ServiceAccount runner if it has no writer yet, creating its row;
// any other writer is refused. Of two concurrent first writers, the row lock lets one bind and
// refuses the other. The account is writer provenance and establishes no execution owner.
func (s *Store) BindWriter(ctx context.Context, sessionID uuid.UUID, writer ServiceAccountWriter, sandboxUID string) (Checkpoint, error) {
	return s.bindWriter(ctx, sessionID, writer, Sandbox{Namespace: writer.Namespace, Name: writer.Name, UID: sandboxUID}, nil)
}

// BindIdentityWriter is BindWriter for a runner authenticated as an Identity, pinned to the Sandbox
// that owns its workload credential. identity, the Identity the Dispatcher recorded the Session as
// executing as, is retained independently of writer provenance.
func (s *Store) BindIdentityWriter(ctx context.Context, sessionID uuid.UUID, writer IdentityWriter, sandbox Sandbox, identity *Identity) (Checkpoint, error) {
	if !writer.valid() || sandbox.Namespace == "" || sandbox.Name == "" || sandbox.UID == "" {
		return Checkpoint{}, ErrInvalidArgument
	}
	return s.bindWriter(ctx, sessionID, writer, sandbox, identity)
}

// BindSamplerWriter retains the producer identity independently of its fixed execution target.
func (s *Store) BindSamplerWriter(ctx context.Context, sessionID uuid.UUID, writer ServiceAccountWriter, sandbox Sandbox, identity *Identity) (Checkpoint, error) {
	if sandbox.Namespace == "" || sandbox.Name == "" || sandbox.UID == "" {
		return Checkpoint{}, ErrInvalidArgument
	}
	return s.bindWriter(ctx, sessionID, writer, sandbox, identity)
}

// writerColumns are writer_{namespace,name,uid} and writer_identity_{name,uid}.
func writerColumns(writer Writer) []any {
	switch writer := writer.(type) {
	case ServiceAccountWriter:
		return []any{writer.Namespace, writer.Name, writer.UID, nil, nil}
	case IdentityWriter:
		return []any{nil, nil, nil, writer.Name, writer.UID}
	default:
		panic(fmt.Sprintf("unknown writer %T", writer))
	}
}

func (s *Store) bindWriter(ctx context.Context, sessionID uuid.UUID, writer Writer, sandbox Sandbox, identity *Identity) (Checkpoint, error) {
	if identity != nil && !identity.valid() {
		return Checkpoint{}, ErrInvalidArgument
	}
	namespace, name, sandboxUID := sandbox.Namespace, sandbox.Name, sandbox.UID
	written := writerColumns(writer)
	var checkpoint Checkpoint
	err := postgres.Transaction(ctx, s.db, sql.TxOptions{}, func(tx *sql.Tx) error {
		created := false
		changed := false
		var sandboxUIDValue any
		if sandboxUID != "" {
			sandboxUIDValue = sandboxUID
		}
		var executionIdentityName, executionIdentityUID any
		if identity != nil {
			executionIdentityName, executionIdentityUID = identity.Name, identity.UID
		}
		var exists bool
		if err := tx.QueryRowContext(ctx, "SELECT EXISTS (SELECT 1 FROM session_history WHERE id = $1)", sessionID).Scan(&exists); err != nil {
			return err
		}
		// Only a first push takes the session lock, so a reconnecting runner never waits on another's.
		if !exists {
			if _, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock($1)", sessionLock); err != nil {
				return err
			}
			result, err := tx.ExecContext(ctx, `INSERT INTO session_history
				(id, writer_namespace, writer_name, writer_uid, sandbox_namespace, sandbox_name, sandbox_uid,
				writer_identity_name, writer_identity_uid, execution_identity_name, execution_identity_uid, source_id, last_cursor)
				VALUES ($1, $2, $3, $4, $6, $7, $5, $8, $9, $10, $11, NULL, 0) ON CONFLICT DO NOTHING`,
				sessionID, written[0], written[1], written[2], sandboxUIDValue, namespace, name,
				written[3], written[4], executionIdentityName, executionIdentityUID)
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
		if identity != nil && (row.executionIdentity == nil || *row.executionIdentity != *identity) {
			// A later announcement cannot assign an owner to an existing unassociated row.
			return fmt.Errorf("%w: %s", ErrExecutionIdentityMismatch, sessionID)
		}
		switch {
		case row.writer == nil:
			if _, err := tx.ExecContext(ctx, `UPDATE session_history SET writer_namespace = $2, writer_name = $3, writer_uid = $4,
				sandbox_namespace = COALESCE(sandbox_namespace, $6), sandbox_name = COALESCE(sandbox_name, $7), sandbox_uid = $5,
				writer_identity_name = $8, writer_identity_uid = $9
				WHERE id = $1`, sessionID, written[0], written[1], written[2], sandboxUIDValue, namespace, name,
				written[3], written[4]); err != nil {
				return err
			}
			row.sandboxUID = optionalString(sandboxUID)
			changed = true
		case row.writer != writer:
			return fmt.Errorf("%w: %s", ErrWriterMismatch, sessionID)
		}
		if row.writer != nil && row.writer == writer {
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

// RecordedSessions returns up to limit Sessions recorded after afterPosition, in recording order.
func (s *Store) RecordedSessions(ctx context.Context, afterPosition uint64, limit int) ([]RecordedSession, error) {
	if afterPosition > math.MaxInt64 || limit < 1 || limit > 1000 {
		return nil, invalid("recorded Session page")
	}
	predicate, scopeArgs := s.sessionPredicate("h", 2)
	args := append([]any{afterPosition}, scopeArgs...)
	args = append(args, limit)
	query := fmt.Sprintf(`SELECT h.id, h.recorded_position FROM session_history h
		WHERE h.recorded_position > $1 AND %s ORDER BY h.recorded_position LIMIT $%d`, predicate, len(args))
	if s.readScope != nil && !s.readScope.all {
		// The persisted session position is global. Return an ordinal over only the caller's
		// retained Sessions so a normal watch cursor does not reveal foreign session gaps.
		query = fmt.Sprintf(`WITH owned AS (
			SELECT h.id, row_number() OVER (ORDER BY h.recorded_position) AS position
			FROM session_history h WHERE %s
		)
		SELECT owned.id, owned.position FROM owned
		WHERE owned.position > $1 ORDER BY owned.position LIMIT $%d`, predicate, len(args))
	}
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var opened []RecordedSession
	for rows.Next() {
		var session RecordedSession
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
	err := postgres.Transaction(ctx, s.db, sql.TxOptions{}, func(tx *sql.Tx) error {
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
			kind := observationNumber(entry)
			compactable := s.options.CompactDeltas && len(payload) <= blockBytes/2 && slices.Contains(compactKinds, historypb.EventKind(kind))
			if _, err := tx.ExecContext(ctx, "INSERT INTO session_event (session_id, cursor, payload, kind, compactable) VALUES ($1, $2, $3, $4, $5)",
				sessionID, cursor, payload, kind, compactable); err != nil {
				return err
			}
			row.sourceID, row.lastCursor, appended = &sourceID, cursor, append(appended, entry)
		}
		if s.options.SettleDeltas {
			for _, entry := range appended {
				settled, err := SettlementForCompletion(ctx, tx, sessionID, entry)
				if err != nil {
					return err
				}
				if settled != nil {
					if err := RecordSettlement(ctx, tx, sessionID, settled); err != nil {
						return err
					}
				}
			}
		}
		if len(appended) > 0 {
			if s.options.CompactDeltas {
				if err := compactEvents(ctx, tx, sessionID); err != nil {
					return err
				}
			}
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
	stored, err := ReadEntry(ctx, tx, sessionID, entry.GetCursor())
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
	if !proto.Equal(stored, entry) {
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
	err := postgres.Transaction(ctx, s.db, sql.TxOptions{}, func(tx *sql.Tx) error {
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
	return postgres.Transaction(ctx, s.db, sql.TxOptions{}, func(tx *sql.Tx) error {
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
		// Session ownership is Identity-only. Writer provenance cannot assign an execution owner
		// to a previously unassociated Session through a later snapshot.
		executionIdentity := next.GetAttached().GetExecutionIdentity()
		switch {
		case row.executionIdentity == nil && executionIdentity != nil:
			return fmt.Errorf("%w: %s", ErrExecutionIdentityMismatch, sessionID)
		case row.executionIdentity != nil && executionIdentity == nil:
			next.GetAttached().ExecutionIdentity = &identitypb.Identity{Name: row.executionIdentity.Name, Uid: row.executionIdentity.UID}
		case row.executionIdentity != nil && executionIdentity != nil &&
			(executionIdentity.GetName() != row.executionIdentity.Name || executionIdentity.GetUid() != row.executionIdentity.UID):
			return fmt.Errorf("%w: %s", ErrExecutionIdentityMismatch, sessionID)
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
		next.Sealed = proto.Clone(prior.GetSealed()).(*sessionpb.Sealed)
	}
	switch priorCursor := prior.GetAttached().GetLastCursor(); {
	case next.GetAttached().GetLastCursor() < priorCursor:
		next.Attached = proto.Clone(prior.GetAttached()).(*sessionpb.Attached)
	case next.GetAttached().GetLastCursor() == priorCursor && !proto.Equal(next.GetAttached(), prior.GetAttached()):
		sameExceptHarness := proto.Clone(prior.GetAttached()).(*sessionpb.Attached)
		sameExceptHarness.HarnessState = next.GetAttached().GetHarnessState()
		// A later incarnation announces again at the cursor the earlier one ended at.
		sameExceptHarness.IncarnationUid = next.GetAttached().IncarnationUid
		if !proto.Equal(sameExceptHarness, next.GetAttached()) {
			return conflict("conflicting attachment snapshots at one cursor")
		}
		nextState, priorState := next.GetAttached().GetHarnessState(), prior.GetAttached().GetHarnessState()
		switch {
		case nextState == priorState:
		case prior.GetSealed() == nil && priorState == sessionpb.HarnessState_HARNESS_STATE_STOPPED &&
			nextState == sessionpb.HarnessState_HARNESS_STATE_RUNNING:
			// Resume can be confirmed before HarnessStarted advances the journal.
		case priorState == sessionpb.HarnessState_HARNESS_STATE_RUNNING &&
			nextState == sessionpb.HarnessState_HARNESS_STATE_STOPPED:
			// Stops have journal entries. A stopped snapshot at the same cursor after Resume belongs to
			// the previous run.
			next.Attached = proto.Clone(prior.GetAttached()).(*sessionpb.Attached)
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
	err := postgres.Transaction(ctx, s.db, snapshot, func(tx *sql.Tx) error {
		var lastCursor uint64
		var feedState []byte
		predicate, scopeArgs := s.sessionPredicate("h", 2)
		args := append([]any{sessionID}, scopeArgs...)
		err := tx.QueryRowContext(ctx, "SELECT h.last_cursor, h.feed_state FROM session_history h WHERE h.id = $1 AND "+predicate, args...).
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
		references, err := eventReferences(ctx, tx, sessionID, afterCursor, lastCursor, nil, limit, false)
		if err != nil {
			return err
		}
		if page.Entries, err = loadEntries(ctx, tx, sessionID, references); err != nil {
			return err
		}
		if len(page.Entries) > 0 {
			through = page.Entries[len(page.Entries)-1].Cursor
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

// ReadSequences reads the next retained entry at each sequence from one retained-history snapshot,
// with at most batchSize requested sequences in each SQL query.
func (s *Store) ReadSequences(ctx context.Context, sessionID uuid.UUID, sequences []uint64, batchSize int) (SequenceReadPage, error) {
	if len(sequences) == 0 || batchSize < 1 {
		return SequenceReadPage{}, invalid("sequence read page")
	}
	for _, sequence := range sequences {
		if sequence == 0 {
			return SequenceReadPage{}, invalid("sequence read page")
		}
	}
	var page SequenceReadPage
	err := postgres.Transaction(ctx, s.db, snapshot, func(tx *sql.Tx) error {
		predicate, scopeArgs := s.sessionPredicate("h", 2)
		args := append([]any{sessionID}, scopeArgs...)
		if err := tx.QueryRowContext(ctx, "SELECT h.last_cursor FROM session_history h WHERE h.id = $1 AND "+predicate, args...).Scan(&page.LastCursor); err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				return fmt.Errorf("%w: %s", ErrNotFound, sessionID)
			}
			return err
		}

		page.Reads = make([]SequenceRead, len(sequences))
		var retained []int64
		var retainedIndexes []int
		for i, sequence := range sequences {
			page.Reads[i].Sequence = sequence
			if sequence <= page.LastCursor {
				retained = append(retained, int64(sequence))
				retainedIndexes = append(retainedIndexes, i)
			}
		}
		if len(retained) == 0 {
			return nil
		}
		for start := 0; start < len(retained); start += batchSize {
			end := min(start+batchSize, len(retained))
			if err := readSequenceChunk(ctx, tx, sessionID, page.LastCursor, retained[start:end], retainedIndexes[start:end], page.Reads); err != nil {
				return err
			}
		}
		return nil
	})
	return page, err
}

func readSequenceChunk(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, lastCursor uint64, sequences []int64, readIndexes []int, reads []SequenceRead) error {
	rows, err := tx.QueryContext(ctx, `SELECT requested.sequence, candidate.cursor, candidate.kind, candidate.size,
			EXISTS (SELECT FROM session_event_settled_range r
				WHERE r.session_id=$1 AND r.first_cursor<=requested.sequence AND r.last_cursor>=requested.sequence)
			OR EXISTS (SELECT FROM session_event_removed_range r
				WHERE r.session_id=$1 AND r.first_cursor<=requested.sequence AND r.last_cursor>=requested.sequence)
		FROM unnest($2::bigint[]) WITH ORDINALITY AS requested(sequence,position)
		LEFT JOIN LATERAL (
			SELECT cursor,kind,octet_length(payload) AS size FROM session_event
			WHERE session_id=$1 AND cursor>=requested.sequence AND cursor<=$3 ORDER BY cursor LIMIT 1
		) candidate ON true ORDER BY requested.position`, sessionID, pq.Array(sequences), lastCursor)
	if err != nil {
		return err
	}
	candidates := make([]*eventReference, len(sequences))
	index := 0
	for rows.Next() {
		var sequence int64
		var cursor, kind, size sql.NullInt64
		var unstored bool
		if err := rows.Scan(&sequence, &cursor, &kind, &size, &unstored); err != nil {
			rows.Close()
			return err
		}
		if index >= len(sequences) || sequence != sequences[index] {
			rows.Close()
			return fmt.Errorf("sequence query disagrees at position %d", index)
		}
		reads[readIndexes[index]].Unstored = unstored
		if cursor.Valid {
			candidates[index] = &eventReference{cursor: uint64(cursor.Int64), kind: historypb.EventKind(kind.Int64), size: int(size.Int64)}
		}
		index++
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()
	if index != len(sequences) {
		return fmt.Errorf("sequence query returned %d rows, want %d", index, len(sequences))
	}
	// At most two neighboring blocks per requested cursor and packed kind. DISTINCT shares a
	// block's metadata across requests; loadEntries likewise decompresses each selected block once.
	kinds := make([]int64, len(compactKinds))
	for index, kind := range compactKinds {
		kinds[index] = int64(kind)
	}
	rows, err = tx.QueryContext(ctx, `SELECT DISTINCT b.first_cursor,b.kind,b.cursors,b.sizes
		FROM unnest($2::bigint[]) requested(sequence) CROSS JOIN unnest($4::integer[]) requested_kind(kind)
		CROSS JOIN LATERAL (
			(SELECT first_cursor,kind,cursors,sizes FROM session_event_block
				WHERE session_id=$1 AND kind=requested_kind.kind AND first_cursor<=requested.sequence
				ORDER BY first_cursor DESC LIMIT 1)
			UNION ALL
			(SELECT first_cursor,kind,cursors,sizes FROM session_event_block
				WHERE session_id=$1 AND kind=requested_kind.kind AND first_cursor>requested.sequence AND first_cursor<=$3
				ORDER BY first_cursor LIMIT 1)
		) b`, sessionID, pq.Array(sequences), lastCursor, pq.Array(kinds))
	if err != nil {
		return err
	}
	for rows.Next() {
		var first uint64
		var kind historypb.EventKind
		var cursors, sizes pq.Int64Array
		if err := rows.Scan(&first, &kind, &cursors, &sizes); err != nil {
			rows.Close()
			return err
		}
		if len(cursors) != len(sizes) {
			rows.Close()
			return fmt.Errorf("event block index lengths disagree")
		}
		for index, sequence := range sequences {
			position, _ := slices.BinarySearch(cursors, sequence)
			if position == len(cursors) || uint64(cursors[position]) > lastCursor {
				continue
			}
			if previous := candidates[index]; previous == nil || uint64(cursors[position]) < previous.cursor {
				candidates[index] = &eventReference{uint64(cursors[position]), kind, int(sizes[position]), &first}
			}
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()
	var references []eventReference
	var indexes []int
	for index, candidate := range candidates {
		if candidate != nil {
			references = append(references, *candidate)
			indexes = append(indexes, index)
		}
	}
	entries, err := loadEntries(ctx, tx, sessionID, references)
	if err != nil {
		return err
	}
	for index, entry := range entries {
		reads[readIndexes[indexes[index]]].NextEntry = entry
	}
	return nil
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
	err := postgres.Transaction(ctx, s.db, snapshot, func(tx *sql.Tx) error {
		predicate, scopeArgs := s.sessionPredicate("h", 2)
		scopeArguments := append([]any{sessionID}, scopeArgs...)
		err := tx.QueryRowContext(ctx, "SELECT h.last_cursor FROM session_history h WHERE h.id = $1 AND "+predicate, scopeArguments...).Scan(&page.LastCursor)
		if errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("%w: %s", ErrNotFound, sessionID)
		}
		if err != nil {
			return err
		}
		lowCursor, highCursor := uint64(0), page.LastCursor
		if after != nil {
			lowCursor = *after
		}
		if before != nil {
			if *before == 0 {
				highCursor = 0
			} else {
				highCursor = min(highCursor, *before-1)
			}
		}
		references, err := eventReferences(ctx, tx, sessionID, lowCursor, highCursor, nil, limit, after == nil)
		if err != nil {
			return err
		}
		for _, reference := range references {
			page.Observations = append(page.Observations, Observation{Cursor: reference.cursor, Kind: reference.kind})
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

// SettlementForCompletion checks the stored span ending at completion without changing history.
// Callers applying its result must serialize with the Session's append path in the same transaction.
func SettlementForCompletion(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, completion *eventlogpb.EventEntry) (*historypb.SettledDeltas, error) {
	item := completionTarget(completion)
	if item == nil {
		return nil, nil
	}
	span := []*eventlogpb.EventEntry{completion}
	var floor uint64
	started := false
	for upper := completion.GetCursor(); !started || upper > floor; {
		batch, err := entriesBefore(ctx, tx, sessionID, upper, 256)
		if err != nil {
			return nil, err
		}
		if len(batch) == 0 || len(span)+len(batch) > settlementSpanLimit {
			return nil, nil
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
	return settle(span), nil
}

type storedEntry struct {
	cursor uint64
	entry  *eventlogpb.EventEntry
}

// entriesBefore returns up to limit stored entries below cursor, nearest first.
func entriesBefore(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, cursor uint64, limit int) ([]storedEntry, error) {
	references, err := eventReferences(ctx, tx, sessionID, 0, cursor-1, nil, limit, true)
	if err != nil {
		return nil, err
	}
	entries, err := loadEntries(ctx, tx, sessionID, references)
	if err != nil {
		return nil, err
	}
	batch := make([]storedEntry, len(entries))
	for index, entry := range entries {
		batch[index] = storedEntry{cursor: entry.Cursor, entry: entry}
	}
	return batch, nil
}

// RecordSettlement stores settled and deletes the entries in its ranges.
func RecordSettlement(ctx context.Context, tx *sql.Tx, sessionID uuid.UUID, settled *historypb.SettledDeltas) error {
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
	}
	return deleteEventRanges(ctx, tx, sessionID, settled.Ranges)
}
