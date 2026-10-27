// Package dispatcher stores accepted Session work before delivery. PostgreSQL is
// both the queue and the record of delivery; notifications are only invalidations.
package dispatcher

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres/go"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

const (
	QueueState      = "QUEUED_IN_DISPATCHER"
	DeliveringState = "DELIVERING_TO_RUNNER"
	AdmittedState   = "RUNNER_ADMITTED"
	FailedState     = "FAILED"
	CancelledState  = "CANCELLED_IN_DISPATCHER"
	UnknownState    = "UNKNOWN"
	WakeLimit       = 10
	WakeWindow      = time.Minute
	WakeDeadline    = 10 * time.Minute
	NotifyChannel   = "agentplane_dispatcher_dispatch"
)

var (
	ErrCommandConflict = errors.New("command_id was used for other work")
	ErrOpeningConflict = errors.New("session_id was opened with another request")
	ErrQueueFull       = errors.New("session queue is full")
	ErrNotFound        = errors.New("dispatch not found")
	ErrNotCancellable  = errors.New("delivery already began")
)

type Account struct{ Namespace, Name string }

type Dispatch struct {
	Position                int64
	CommandID, SessionID    string
	SandboxName, SandboxUID string
	WakeEligible            bool
	WakeCaller              *Account
	WakeFromIncarnation     *string
	WakeRequestedAt         *time.Time
	Operation               string
	Command                 *commandpb.Command
	State, Reason           string
	Admission               *eventlogpb.EventEntry
	Traceparent             string
	CreatedAt, UpdatedAt    time.Time
}

type Opening struct {
	SessionID, SandboxName, SandboxUID string
	WakeEligible                       bool
	WakeCaller                         *Account
	WakeFromIncarnation                *string
	WakeRequestedAt                    *time.Time
	Spec                               *runnerpb.SessionSpec
	SetupScript                        *string
	State, Reason                      string
	Traceparent                        string
	CreatedAt, UpdatedAt               time.Time
}

// Owner is a fresh Kubernetes Lease proof paired with the committed DB generation.
// Every worker transition locks and compares this full token before changing work.
type Owner struct {
	SandboxUID, LeaseUID, HolderIdentity string
	Generation                           int64
	LeaseTransitions                     int
}

type Store struct {
	DB       *sql.DB
	QueueCap int
}

func NewStore(db *sql.DB, queueCap int) *Store { return &Store{DB: db, QueueCap: queueCap} }

func transaction(ctx context.Context, db *sql.DB, f func(*sql.Tx) error) error {
	return postgres.Transaction(ctx, db, sql.TxOptions{}, f)
}

func notify(ctx context.Context, tx *sql.Tx) error {
	return postgres.Notify(ctx, tx, NotifyChannel, "")
}

func sessionLock(ctx context.Context, tx *sql.Tx, sessionID string) error {
	_, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "session:"+sessionID)
	return err
}

func operation(command *commandpb.Command) (string, error) {
	switch command.GetOperation().(type) {
	case *commandpb.Command_SubmitInput:
		return "submit_input", nil
	case *commandpb.Command_ChangeModel:
		return "change_model", nil
	case *commandpb.Command_ChangeReasoningEffort:
		return "change_reasoning_effort", nil
	default:
		return "", errors.New("not a stored command operation")
	}
}

func wire(message proto.Message) ([]byte, error) {
	return proto.MarshalOptions{Deterministic: true}.Marshal(message)
}

const dispatchColumns = `position, command_id, session_id, sandbox_name, sandbox_uid::text,
	wake_eligible, wake_caller_namespace, wake_caller_name, wake_from_incarnation,
	wake_requested_at, operation, command, state, reason, admission, traceparent, created_at, updated_at`

func scanDispatch(row interface{ Scan(...any) error }) (*Dispatch, error) {
	d := &Dispatch{}
	var callerNS, callerName, incarnation, reason sql.NullString
	var wakeAt sql.NullTime
	var commandBytes, admissionBytes []byte
	err := row.Scan(&d.Position, &d.CommandID, &d.SessionID, &d.SandboxName, &d.SandboxUID,
		&d.WakeEligible, &callerNS, &callerName, &incarnation, &wakeAt, &d.Operation,
		&commandBytes, &d.State, &reason, &admissionBytes, &d.Traceparent, &d.CreatedAt, &d.UpdatedAt)
	if err != nil {
		return nil, err
	}
	if callerNS.Valid && callerName.Valid {
		d.WakeCaller = &Account{callerNS.String, callerName.String}
	}
	if incarnation.Valid {
		d.WakeFromIncarnation = &incarnation.String
	}
	if wakeAt.Valid {
		d.WakeRequestedAt = &wakeAt.Time
	}
	if reason.Valid {
		d.Reason = reason.String
	}
	d.Command = &commandpb.Command{}
	if err := proto.Unmarshal(commandBytes, d.Command); err != nil {
		return nil, err
	}
	if admissionBytes != nil {
		d.Admission = &eventlogpb.EventEntry{}
		if err := proto.Unmarshal(admissionBytes, d.Admission); err != nil {
			return nil, err
		}
	}
	return d, nil
}

// Enqueue commits the complete command and NOTIFY in one transaction. Repeated
// command IDs return the original row even after a Sandbox was recreated under
// the same name, matching the Python store's exact retry contract.
func (s *Store) Enqueue(ctx context.Context, sessionID, sandboxName, sandboxUID string, command *commandpb.Command, wakeCaller *Account) (*Dispatch, error) {
	if command == nil || command.GetCommandId() == "" || sessionID == "" || sandboxName == "" {
		return nil, errors.New("command_id, session_id and sandbox are required")
	}
	op, err := operation(command)
	if err != nil {
		return nil, err
	}
	data, err := wire(command)
	if err != nil {
		return nil, err
	}
	var result *Dispatch
	err = transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := sessionLock(ctx, tx, sessionID); err != nil {
			return err
		}
		// A command ID is global, even when two senders race through different
		// Session locks.
		if _, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "command:"+command.GetCommandId()); err != nil {
			return err
		}
		existing, err := scanDispatch(tx.QueryRowContext(ctx, "SELECT "+dispatchColumns+" FROM dispatch WHERE command_id=$1", command.GetCommandId()))
		if err == nil {
			oldBytes, err := wire(existing.Command)
			if err != nil {
				return err
			}
			if existing.SessionID != sessionID || existing.SandboxName != sandboxName || !bytes.Equal(oldBytes, data) {
				return ErrCommandConflict
			}
			result = existing
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		var count int
		err = tx.QueryRowContext(ctx, "SELECT count(*) FROM dispatch WHERE session_id=$1 AND state=$2", sessionID, QueueState).Scan(&count)
		if err == nil && count >= s.QueueCap {
			return fmt.Errorf("%w: session %s holds %d queued commands", ErrQueueFull, sessionID, count)
		}
		if err != nil {
			return err
		}
		var ns, name any
		wakeEligible := wakeCaller != nil
		if wakeEligible {
			ns, name = wakeCaller.Namespace, wakeCaller.Name
		}
		result, err = scanDispatch(tx.QueryRowContext(ctx, `INSERT INTO dispatch
			(command_id,session_id,sandbox_name,sandbox_uid,wake_eligible,wake_caller_namespace,wake_caller_name,
			 operation,command,state,traceparent,created_at,updated_at)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now(),now()) RETURNING `+dispatchColumns,
			command.GetCommandId(), sessionID, sandboxName, sandboxUID, wakeEligible, ns, name, op, data, QueueState,
			observability.Traceparent(ctx)))
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	return result, err
}

func (s *Store) GetDispatch(ctx context.Context, id string) (*Dispatch, error) {
	d, err := scanDispatch(s.DB.QueryRowContext(ctx, "SELECT "+dispatchColumns+" FROM dispatch WHERE command_id=$1", id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return d, err
}

// ListDispatches returns the Dispatches of one Session or Sandbox, oldest first; a non-empty commandIDs
// narrows them to those commands.
func (s *Store) ListDispatches(ctx context.Context, sessionID, sandboxName string, commandIDs []string) ([]*Dispatch, error) {
	if (sessionID == "") == (sandboxName == "") {
		return nil, errors.New("exactly one dispatch scope is required")
	}
	column, value := "session_id", sessionID
	if sandboxName != "" {
		column, value = "sandbox_name", sandboxName
	}
	query, arguments := "SELECT "+dispatchColumns+" FROM dispatch WHERE "+column+"=$1", []any{value}
	if len(commandIDs) > 0 {
		query += " AND command_id = ANY($2)"
		arguments = append(arguments, pq.Array(commandIDs))
	}
	rows, err := s.DB.QueryContext(ctx, query+" ORDER BY position", arguments...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []*Dispatch
	for rows.Next() {
		d, err := scanDispatch(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, d)
	}
	return result, rows.Err()
}

func (s *Store) Cancel(ctx context.Context, id string) (*Dispatch, error) {
	var result *Dispatch
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		var err error
		result, err = scanDispatch(tx.QueryRowContext(ctx, `UPDATE dispatch SET state=$1, reason='sender', updated_at=now()
			WHERE command_id=$2 AND state=$3 RETURNING `+dispatchColumns, CancelledState, id, QueueState))
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	if result != nil {
		return result, nil
	}
	if _, err := s.GetDispatch(ctx, id); err != nil {
		return nil, err
	}
	return nil, ErrNotCancellable
}

const openingColumns = `session_id, sandbox_name, sandbox_uid::text, wake_eligible,
	wake_caller_namespace, wake_caller_name, wake_from_incarnation, wake_requested_at,
	spec, setup_script, state, reason, traceparent, created_at, updated_at`

func scanOpening(row interface{ Scan(...any) error }) (*Opening, error) {
	o := &Opening{}
	var callerNS, callerName, incarnation, script, reason sql.NullString
	var wakeAt sql.NullTime
	var spec []byte
	err := row.Scan(&o.SessionID, &o.SandboxName, &o.SandboxUID, &o.WakeEligible,
		&callerNS, &callerName, &incarnation, &wakeAt, &spec, &script, &o.State,
		&reason, &o.Traceparent, &o.CreatedAt, &o.UpdatedAt)
	if err != nil {
		return nil, err
	}
	if callerNS.Valid && callerName.Valid {
		o.WakeCaller = &Account{callerNS.String, callerName.String}
	}
	if incarnation.Valid {
		o.WakeFromIncarnation = &incarnation.String
	}
	if wakeAt.Valid {
		o.WakeRequestedAt = &wakeAt.Time
	}
	if script.Valid {
		o.SetupScript = &script.String
	}
	if reason.Valid {
		o.Reason = reason.String
	}
	o.Spec = &runnerpb.SessionSpec{}
	if err := proto.Unmarshal(spec, o.Spec); err != nil {
		return nil, err
	}
	return o, nil
}

func (s *Store) OpenSession(ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *runnerpb.SessionSpec, setupScript *string, wakeCaller *Account) (*Opening, error) {
	if sessionID == "" || sandboxName == "" || spec == nil {
		return nil, errors.New("session_id, sandbox and spec are required")
	}
	data, err := wire(spec)
	if err != nil {
		return nil, err
	}
	var result *Opening
	err = transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := sessionLock(ctx, tx, sessionID); err != nil {
			return err
		}
		existing, err := scanOpening(tx.QueryRowContext(ctx, "SELECT "+openingColumns+" FROM session_opening WHERE session_id=$1", sessionID))
		if err == nil {
			oldBytes, err := wire(existing.Spec)
			if err != nil {
				return err
			}
			if existing.SandboxName != sandboxName || existing.SandboxUID != sandboxUID || !bytes.Equal(oldBytes, data) || !equalOptional(existing.SetupScript, setupScript) {
				return ErrOpeningConflict
			}
			result = existing
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		var ns, name any
		if wakeCaller != nil {
			ns, name = wakeCaller.Namespace, wakeCaller.Name
		}
		result, err = scanOpening(tx.QueryRowContext(ctx, `INSERT INTO session_opening
			(session_id,sandbox_name,sandbox_uid,wake_eligible,wake_caller_namespace,wake_caller_name,
			 spec,setup_script,state,traceparent,created_at,updated_at)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,now(),now()) RETURNING `+openingColumns,
			sessionID, sandboxName, sandboxUID, wakeCaller != nil, ns, name, data, setupScript, QueueState,
			observability.Traceparent(ctx)))
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	return result, err
}

func equalOptional(a, b *string) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
}

func (s *Store) GetOpening(ctx context.Context, id string) (*Opening, error) {
	o, err := scanOpening(s.DB.QueryRowContext(ctx, "SELECT "+openingColumns+" FROM session_opening WHERE session_id=$1", id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return o, err
}

func (s *Store) OpeningHeads(ctx context.Context) ([]*Opening, error) {
	rows, err := s.DB.QueryContext(ctx, "SELECT "+openingColumns+" FROM session_opening WHERE state=$1 ORDER BY created_at", QueueState)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []*Opening
	for rows.Next() {
		o, err := scanOpening(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, o)
	}
	return result, rows.Err()
}

// SessionHead reads the durable head after a queue key becomes runnable. The
// in-process queue never decides ordering or whether an opening is complete.
func (s *Store) SessionHead(ctx context.Context, sessionID string) (*Opening, *Dispatch, error) {
	opening, err := scanOpening(s.DB.QueryRowContext(ctx,
		"SELECT "+openingColumns+" FROM session_opening WHERE session_id=$1 AND state=$2", sessionID, QueueState))
	if err == nil {
		return opening, nil, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, nil, err
	}
	head, err := scanDispatch(s.DB.QueryRowContext(ctx, `SELECT `+dispatchColumns+` FROM dispatch d WHERE d.session_id=$1 AND d.state=$2
		AND NOT EXISTS (SELECT 1 FROM dispatch earlier WHERE earlier.session_id=d.session_id
			AND earlier.position<d.position AND earlier.state IN ($2,$3))
		AND NOT EXISTS (SELECT 1 FROM session_opening o WHERE o.session_id=d.session_id
			AND o.state IN ($2,$3)) ORDER BY d.position LIMIT 1`, sessionID, QueueState, DeliveringState))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil, nil
	}
	return nil, head, err
}

func (s *Store) Heads(ctx context.Context) ([]*Dispatch, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+dispatchColumns+` FROM dispatch d WHERE d.state=$1
		AND NOT EXISTS (SELECT 1 FROM dispatch earlier WHERE earlier.session_id=d.session_id
			AND earlier.position<d.position AND earlier.state IN ($1,$2))
		AND NOT EXISTS (SELECT 1 FROM session_opening o WHERE o.session_id=d.session_id
			AND o.state IN ($1,$2)) ORDER BY d.position`, QueueState, DeliveringState)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []*Dispatch
	for rows.Next() {
		d, err := scanDispatch(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, d)
	}
	return result, rows.Err()
}

// SandboxesWithPendingWork lists the Sandboxes owning a QUEUED or DELIVERING
// dispatch or opening, the only rows an owner changes. Unlike Heads, it counts
// a DELIVERING row and the QUEUED rows blocked behind it, which a successor
// must requeue after the previous owner dies.
func (s *Store) SandboxesWithPendingWork(ctx context.Context) (map[string]struct{}, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT sandbox_uid::text FROM dispatch WHERE state = ANY($1)
		UNION SELECT sandbox_uid::text FROM session_opening WHERE state = ANY($1)`,
		pq.Array([]string{QueueState, DeliveringState}))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]struct{}{}
	for rows.Next() {
		var uid string
		if err := rows.Scan(&uid); err != nil {
			return nil, err
		}
		result[uid] = struct{}{}
	}
	return result, rows.Err()
}

func (s *Store) OwnershipGeneration(ctx context.Context, sandboxUID string) (int64, error) {
	var generation int64
	err := s.DB.QueryRowContext(ctx, "SELECT generation FROM sandbox_ownership WHERE sandbox_uid=$1", sandboxUID).Scan(&generation)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, nil
	}
	return generation, err
}

func ownerMatch(ctx context.Context, tx *sql.Tx, owner Owner) (bool, error) {
	var generation int64
	var leaseUID, holder string
	var transitions int
	err := tx.QueryRowContext(ctx, `SELECT generation, lease_uid, lease_transitions, holder_identity
		FROM sandbox_ownership WHERE sandbox_uid=$1 FOR UPDATE`, owner.SandboxUID).Scan(&generation, &leaseUID, &transitions, &holder)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return generation == owner.Generation && leaseUID == owner.LeaseUID && transitions == owner.LeaseTransitions && holder == owner.HolderIdentity, nil
}

func (s *Store) IsCurrentOwner(ctx context.Context, owner Owner) (bool, error) {
	var generation int64
	var leaseUID, holder string
	var transitions int
	err := s.DB.QueryRowContext(ctx, `SELECT generation, lease_uid, lease_transitions, holder_identity
		FROM sandbox_ownership WHERE sandbox_uid=$1`, owner.SandboxUID).Scan(&generation, &leaseUID, &transitions, &holder)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return generation == owner.Generation && leaseUID == owner.LeaseUID && transitions == owner.LeaseTransitions && holder == owner.HolderIdentity, nil
}

// ClaimSandboxOwner must be called only after validating a fresh Kubernetes
// Lease. expectedGeneration is read before Lease acquisition; this CAS prevents
// a paused old holder from replacing a successor's DB fence.
func (s *Store) ClaimSandboxOwner(ctx context.Context, sandboxUID, leaseUID string, leaseTransitions int, holder string, expectedGeneration int64) (*Owner, error) {
	var result *Owner
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		var generation int64
		var oldLease, oldHolder string
		var oldTransitions int
		err := tx.QueryRowContext(ctx, `SELECT generation,lease_uid,lease_transitions,holder_identity
			FROM sandbox_ownership WHERE sandbox_uid=$1 FOR UPDATE`, sandboxUID).Scan(&generation, &oldLease, &oldTransitions, &oldHolder)
		if errors.Is(err, sql.ErrNoRows) {
			generation = 0
		} else if err != nil {
			return err
		}
		if generation != expectedGeneration {
			return nil
		}
		same := generation != 0 && oldLease == leaseUID && oldTransitions == leaseTransitions && oldHolder == holder
		if generation == 0 {
			generation = 1
			err = tx.QueryRowContext(ctx, `INSERT INTO sandbox_ownership
				(sandbox_uid,generation,lease_uid,lease_transitions,holder_identity,updated_at)
				VALUES ($1,$2,$3,$4,$5,now()) ON CONFLICT DO NOTHING RETURNING generation`,
				sandboxUID, generation, leaseUID, leaseTransitions, holder).Scan(&generation)
			if errors.Is(err, sql.ErrNoRows) {
				return nil
			}
		} else if !same {
			if oldLease == leaseUID && leaseTransitions <= oldTransitions {
				return nil
			}
			generation++
			_, err = tx.ExecContext(ctx, `UPDATE sandbox_ownership SET generation=$2,lease_uid=$3,
				lease_transitions=$4,holder_identity=$5,updated_at=now() WHERE sandbox_uid=$1`,
				sandboxUID, generation, leaseUID, leaseTransitions, holder)
		}
		if err != nil {
			return err
		}
		if !same {
			if err := notify(ctx, tx); err != nil {
				return err
			}
		}
		result = &Owner{sandboxUID, leaseUID, holder, generation, leaseTransitions}
		return nil
	})
	return result, err
}

func (s *Store) Settle(ctx context.Context, id string, owner Owner, from []string, to, reason string, admission *eventlogpb.EventEntry) (*Dispatch, error) {
	var result *Dispatch
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		// A nil `[]byte` stores an empty bytea, which reads back as an empty admission; only a nil
		// interface stores NULL.
		var payload any
		if admission != nil {
			payload, err = wire(admission)
			if err != nil {
				return err
			}
		}
		result, err = scanDispatch(tx.QueryRowContext(ctx, `UPDATE dispatch SET state=$3, reason=$4, admission=$5, updated_at=now()
			WHERE command_id=$1 AND sandbox_uid=$2 AND state = ANY($6) RETURNING `+dispatchColumns,
			id, owner.SandboxUID, to, nullIfEmpty(reason), payload, pqStringArray(from)))
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if result != nil {
			return notify(ctx, tx)
		}
		return nil
	})
	return result, err
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}
func pqStringArray(values []string) any { return pq.Array(values) }

func (s *Store) SettleOpening(ctx context.Context, id string, owner Owner, from []string, to, reason string) (*Opening, error) {
	var result *Opening
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err = scanOpening(tx.QueryRowContext(ctx, `UPDATE session_opening SET state=$3,reason=$4,updated_at=now()
			WHERE session_id=$1 AND sandbox_uid=$2 AND state = ANY($5) RETURNING `+openingColumns,
			id, owner.SandboxUID, to, nullIfEmpty(reason), pqStringArray(from)))
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if result != nil {
			return notify(ctx, tx)
		}
		return nil
	})
	return result, err
}

// RecoverSandbox requeues uncertain in-flight work after taking the successor's
// owner fence. The runner's command/session IDs make replay safe after a lost reply.
func (s *Store) RecoverSandbox(ctx context.Context, owner Owner) (bool, error) {
	current := false
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		var err error
		current, err = ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		_, err = tx.ExecContext(ctx, `UPDATE dispatch SET state=$2, reason='dispatcher_restarted', updated_at=now()
			WHERE sandbox_uid=$1 AND state=$3`, owner.SandboxUID, QueueState, DeliveringState)
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `UPDATE session_opening SET state=$2, reason='dispatcher_restarted', updated_at=now()
			WHERE sandbox_uid=$1 AND state=$3`, owner.SandboxUID, QueueState, DeliveringState)
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	return current, err
}

// NoteWaiting changes only a still-queued row under the current Sandbox fence.
func (s *Store) NoteWaiting(ctx context.Context, id string, owner Owner, reason string) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err := tx.ExecContext(ctx, `UPDATE dispatch SET reason=$3,updated_at=now()
			WHERE command_id=$1 AND sandbox_uid=$2 AND state=$4 AND reason IS DISTINCT FROM $3`,
			id, owner.SandboxUID, reason, QueueState)
		if err != nil {
			return err
		}
		n, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if n > 0 {
			return notify(ctx, tx)
		}
		return nil
	})
}

func (s *Store) NoteOpeningWaiting(ctx context.Context, id string, owner Owner, reason string) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err := tx.ExecContext(ctx, `UPDATE session_opening SET reason=$3,updated_at=now()
			WHERE session_id=$1 AND sandbox_uid=$2 AND state=$4 AND reason IS DISTINCT FROM $3`,
			id, owner.SandboxUID, reason, QueueState)
		if err != nil {
			return err
		}
		n, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if n > 0 {
			return notify(ctx, tx)
		}
		return nil
	})
}
