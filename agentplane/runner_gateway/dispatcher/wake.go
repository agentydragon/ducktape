package dispatcher

import (
	"context"
	"database/sql"
	"errors"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/lifecycle"
)

// ReserveWakeAttempt counts distinct guarded wake generations per authenticated
// caller across replicas. A retry of the same generation spends no new budget.
func (s *Store) ReserveWakeAttempt(ctx context.Context, caller lifecycle.ServiceAccount, sandboxUID string, generation int64) (time.Duration, error) {
	if caller.Namespace == "" || caller.Name == "" || caller.UID == "" {
		return 0, errors.New("wake caller namespace, name and UID are required")
	}
	var retryAfter time.Duration
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		_, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,0))`,
			"dispatcher-wake:"+caller.Namespace+":"+caller.Name+":"+caller.UID)
		if err != nil {
			return err
		}
		var now time.Time
		if err := tx.QueryRowContext(ctx, "SELECT clock_timestamp()").Scan(&now); err != nil {
			return err
		}
		windowStart := now.Add(-WakeWindow)
		if _, err := tx.ExecContext(ctx, "DELETE FROM wake_attempt WHERE attempted_at <= $1", windowStart); err != nil {
			return err
		}
		var exists bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM wake_attempt
			WHERE caller_namespace=$1 AND caller_name=$2 AND caller_uid=$3 AND sandbox_uid=$4 AND wake_generation=$5)`,
			caller.Namespace, caller.Name, caller.UID, sandboxUID, generation).Scan(&exists); err != nil {
			return err
		}
		if exists {
			return nil
		}
		var count int
		var oldest sql.NullTime
		if err := tx.QueryRowContext(ctx, `SELECT count(*),min(attempted_at) FROM wake_attempt
			WHERE caller_namespace=$1 AND caller_name=$2 AND caller_uid=$3 AND attempted_at>$4`,
			caller.Namespace, caller.Name, caller.UID, windowStart).Scan(&count, &oldest); err != nil {
			return err
		}
		if count >= WakeLimit {
			if !oldest.Valid {
				return errors.New("wake budget full without attempt")
			}
			retryAfter = oldest.Time.Add(WakeWindow).Sub(now)
			if retryAfter < time.Millisecond {
				retryAfter = time.Millisecond
			}
			return nil
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO wake_attempt
			(caller_namespace,caller_name,caller_uid,sandbox_uid,wake_generation,attempted_at)
			VALUES ($1,$2,$3,$4,$5,$6)`, caller.Namespace, caller.Name, caller.UID, sandboxUID, generation, now)
		return err
	})
	return retryAfter, err
}

// A null incarnation means no wake cycle; the empty string means a wake before
// any incarnation existed. The distinction is retained from the Python schema.
func (s *Store) NoteWaking(ctx context.Context, id string, owner Owner, incarnation *string, opening bool) (bool, error) {
	table, key := wakeTable(opening)
	value := ""
	if incarnation != nil {
		value = *incarnation
	}
	var stillQueued bool
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err := tx.ExecContext(ctx, `UPDATE `+table+` SET wake_from_incarnation=$3,updated_at=now()
			WHERE `+key+`=$1 AND sandbox_uid=$2 AND state=$4 AND wake_from_incarnation IS NULL`,
			id, owner.SandboxUID, value, QueueState)
		if err != nil {
			return err
		}
		n, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if n > 0 {
			stillQueued = true
			return notify(ctx, tx)
		}
		return tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM `+table+` WHERE `+key+`=$1
			AND sandbox_uid=$2 AND state=$3)`, id, owner.SandboxUID, QueueState).Scan(&stillQueued)
	})
	return stillQueued, err
}

func (s *Store) NoteWakeRequestedAt(ctx context.Context, id string, owner Owner, opening bool, requestedAt time.Time) (*time.Time, error) {
	table, key := wakeTable(opening)
	var result *time.Time
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		var requested time.Time
		err = tx.QueryRowContext(ctx, `UPDATE `+table+` SET wake_requested_at=$3,updated_at=now()
			WHERE `+key+`=$1 AND sandbox_uid=$2 AND state=$4 AND wake_requested_at IS NULL
			RETURNING wake_requested_at`, id, owner.SandboxUID, requestedAt, QueueState).Scan(&requested)
		if err == nil {
			result = &requested
			return notify(ctx, tx)
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		var old sql.NullTime
		err = tx.QueryRowContext(ctx, `SELECT wake_requested_at FROM `+table+`
			WHERE `+key+`=$1 AND sandbox_uid=$2 AND state=$3`, id, owner.SandboxUID, QueueState).Scan(&old)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if old.Valid {
			result = &old.Time
		}
		return nil
	})
	return result, err
}

func (s *Store) ClearWakeState(ctx context.Context, id string, owner Owner, opening bool) error {
	table, key := wakeTable(opening)
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err := tx.ExecContext(ctx, `UPDATE `+table+` SET wake_from_incarnation=NULL,
			wake_requested_at=NULL,updated_at=now() WHERE `+key+`=$1 AND sandbox_uid=$2
			AND state=$3 AND (wake_from_incarnation IS NOT NULL OR wake_requested_at IS NOT NULL)`,
			id, owner.SandboxUID, QueueState)
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

// FailExpiredWake returns a positive remaining duration if the PostgreSQL
// deadline has not arrived. A nil row and zero duration means no live request.
func (s *Store) FailExpiredWake(
	ctx context.Context,
	id string,
	owner Owner,
	opening bool,
	reason string,
	blocker *environmentpb.DeliveryBlocker,
	assessment *environmentpb.WakeAssessment,
) (*Dispatch, *Opening, time.Duration, error) {
	if assessment == nil {
		return nil, nil, 0, errors.New("wake assessment is required")
	}
	blockerBytes, err := wireOptional(blocker)
	if err != nil {
		return nil, nil, 0, err
	}
	assessmentBytes, err := wire(assessment)
	if err != nil {
		return nil, nil, 0, err
	}
	table, key := wakeTable(opening)
	var dispatch *Dispatch
	var session *Opening
	var remaining time.Duration
	err = transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		var requested time.Time
		err = tx.QueryRowContext(ctx, `SELECT wake_requested_at FROM `+table+` WHERE `+key+`=$1
			AND sandbox_uid=$2 AND state=$3 AND wake_requested_at IS NOT NULL FOR UPDATE`,
			id, owner.SandboxUID, QueueState).Scan(&requested)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		var now time.Time
		if err := tx.QueryRowContext(ctx, "SELECT clock_timestamp()").Scan(&now); err != nil {
			return err
		}
		remaining = requested.Add(WakeDeadline).Sub(now)
		if remaining > 0 {
			return nil
		}
		remaining = 0
		if opening {
			session, err = scanOpening(tx.QueryRowContext(ctx, `UPDATE session_opening SET state=$3,
				reason=$4,updated_at=$5,delivery_blocker=COALESCE($7,delivery_blocker),wake_assessment=$8
				WHERE session_id=$1 AND sandbox_uid=$2 AND state=$6
				RETURNING `+openingColumns, id, owner.SandboxUID, FailedState, reason, now, QueueState, blockerBytes, assessmentBytes))
		} else {
			dispatch, err = scanDispatch(tx.QueryRowContext(ctx, `UPDATE dispatch SET state=$3,
				reason=$4,updated_at=$5,delivery_blocker=COALESCE($7,delivery_blocker),wake_assessment=$8
				WHERE command_id=$1 AND sandbox_uid=$2 AND state=$6
				RETURNING `+dispatchColumns, id, owner.SandboxUID, FailedState, reason, now, QueueState, blockerBytes, assessmentBytes))
		}
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	return dispatch, session, remaining, err
}

func wakeTable(opening bool) (table, key string) {
	if opening {
		return "session_opening", "session_id"
	}
	return "dispatch", "command_id"
}
