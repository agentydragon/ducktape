package store

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/wake"
)

// ReserveWakeAttempt counts distinct guarded wake generations per authenticated
// caller across replicas. A retry of the same generation spends no new budget.
func (s *Store) ReserveWakeAttempt(ctx context.Context, caller wake.WakeBudgetKey, sandboxUID string, generation int64) (time.Duration, error) {
	var table, lockKey, predicate, actorColumns, actorParameters string
	var actorArgs []any
	// Both statements bind the Sandbox, generation, and timestamp before the actor fields.
	if caller.ServiceSPIFFEID != "" {
		if caller.Namespace != "" || caller.Name != "" || caller.UID != "" {
			return 0, errors.New("service wake budget cannot contain account fields")
		}
		if err := auth.ValidateServiceCallers([]string{caller.ServiceSPIFFEID}); err != nil {
			return 0, err
		}
		table = "service_wake_attempt"
		lockKey = "dispatcher-service-wake:" + caller.ServiceSPIFFEID
		predicate = "service_spiffe_id=$4"
		actorColumns, actorParameters = "service_spiffe_id", "$4"
		actorArgs = []any{caller.ServiceSPIFFEID}
	} else {
		if caller.Namespace == "" || caller.Name == "" || caller.UID == "" {
			return 0, errors.New("wake caller namespace, name and UID are required")
		}
		table = "wake_attempt"
		// Keep sharing a lock with older writers that stored attempts without a UID.
		lockKey = "dispatcher-wake:" + caller.Namespace + ":" + caller.Name
		predicate = "caller_namespace=$4 AND caller_name=$5 AND caller_uid IN ($6,'')"
		actorColumns, actorParameters = "caller_namespace,caller_name,caller_uid", "$4,$5,$6"
		actorArgs = []any{caller.Namespace, caller.Name, caller.UID}
	}

	var retryAfter time.Duration
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", lockKey); err != nil {
			return err
		}
		var now time.Time
		if err := tx.QueryRowContext(ctx, "SELECT clock_timestamp()").Scan(&now); err != nil {
			return err
		}
		windowStart := now.Add(-WakeWindow)
		if _, err := tx.ExecContext(ctx, "DELETE FROM "+table+" WHERE attempted_at <= $1", windowStart); err != nil {
			return err
		}

		var count int
		var oldest sql.NullTime
		var duplicate bool
		if err := tx.QueryRowContext(ctx, `SELECT count(*), min(attempted_at),
			count(*) FILTER (WHERE sandbox_uid=$1 AND wake_generation=$2) > 0
			FROM `+table+` WHERE attempted_at>$3 AND `+predicate,
			append([]any{sandboxUID, generation, windowStart}, actorArgs...)...).Scan(&count, &oldest, &duplicate); err != nil {
			return err
		}
		if duplicate {
			return nil
		}
		if count >= WakeLimit {
			retryAfter = max(oldest.Time.Add(WakeWindow).Sub(now), time.Millisecond)
			return nil
		}
		_, err := tx.ExecContext(ctx, `INSERT INTO `+table+` (sandbox_uid,wake_generation,attempted_at,`+actorColumns+`)
			VALUES ($1,$2,$3,`+actorParameters+`)`, append([]any{sandboxUID, generation, now}, actorArgs...)...)
		return err
	})
	return retryAfter, err
}

// A null incarnation means no wake cycle; the empty string means a wake before
// any incarnation existed. The distinction is retained from the Python schema.
func (s *Store) NoteWaking(ctx context.Context, id string, owner SessionOwner, incarnation *string, isSession bool, assessment *environmentpb.WakeAssessment) (bool, error) {
	table, key := wakeTable(isSession)
	waiting := waitingState(isSession)
	value := ""
	if incarnation != nil {
		value = *incarnation
	}
	assessmentBytes, err := Wire(assessment)
	if err != nil {
		return false, err
	}
	var stillQueued bool
	err = transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		// Persist uncertainty before the external wake call; cancellation must retain it.
		result, err := tx.ExecContext(ctx, `UPDATE `+table+` SET wake_from_incarnation=COALESCE(wake_from_incarnation,$3),
			wake_assessment=$5,updated_at=now() WHERE `+key+`=$1 AND session_id=$2 AND state_value=$4`,
			id, owner.SessionID, value, waiting, assessmentBytes)
		if err != nil {
			return err
		}
		n, err := result.RowsAffected()
		if err != nil || n == 0 {
			return err
		}
		stillQueued = true
		return notify(ctx, tx, owner.SessionID)
	})
	return stillQueued, err
}

func (s *Store) NoteWakeRequestedAt(ctx context.Context, id string, owner SessionOwner, isSession bool, requestedAt time.Time) (*time.Time, error) {
	table, key := wakeTable(isSession)
	waiting := waitingState(isSession)
	var result *time.Time
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		var requested time.Time
		err = tx.QueryRowContext(ctx, `UPDATE `+table+` SET wake_requested_at=$3,updated_at=now()
			WHERE `+key+`=$1 AND session_id=$2 AND state_value=$4 AND wake_requested_at IS NULL
			RETURNING wake_requested_at`, id, owner.SessionID, requestedAt, waiting).Scan(&requested)
		if err == nil {
			result = &requested
			return notify(ctx, tx, owner.SessionID)
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		var old sql.NullTime
		err = tx.QueryRowContext(ctx, `SELECT wake_requested_at FROM `+table+`
			WHERE `+key+`=$1 AND session_id=$2 AND state_value=$3`, id, owner.SessionID, waiting).Scan(&old)
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

func (s *Store) ClearWakeState(ctx context.Context, id string, owner SessionOwner, isSession bool) error {
	table, key := wakeTable(isSession)
	waiting := waitingState(isSession)
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err := tx.ExecContext(ctx, `UPDATE `+table+` SET wake_from_incarnation=NULL,
			wake_requested_at=NULL,updated_at=now() WHERE `+key+`=$1 AND session_id=$2
			AND state_value=$3 AND (wake_from_incarnation IS NOT NULL OR wake_requested_at IS NOT NULL)`,
			id, owner.SessionID, waiting)
		if err != nil {
			return err
		}
		n, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if n > 0 {
			return notify(ctx, tx, owner.SessionID)
		}
		return nil
	})
}

// FailExpiredWake returns a positive remaining duration if the PostgreSQL
// deadline has not arrived. A nil row and zero duration means no live request.
func (s *Store) FailExpiredWake(
	ctx context.Context,
	id string,
	owner SessionOwner,
	isSession bool,
	reason string,
	blocker *environmentpb.DeliveryBlocker,
	assessment *environmentpb.WakeAssessment,
) (*Dispatch, *Session, time.Duration, error) {
	if assessment == nil {
		return nil, nil, 0, errors.New("wake assessment is required")
	}
	blockerBytes, err := wireOptional(blocker)
	if err != nil {
		return nil, nil, 0, err
	}
	assessmentBytes, err := Wire(assessment)
	if err != nil {
		return nil, nil, 0, err
	}
	table, key := wakeTable(isSession)
	waiting := waitingState(isSession)
	var dispatch *Dispatch
	var session *Session
	var remaining time.Duration
	err = transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		var requested time.Time
		err = tx.QueryRowContext(ctx, `SELECT wake_requested_at FROM `+table+` WHERE `+key+`=$1
			AND session_id=$2 AND state_value=$3 AND wake_requested_at IS NOT NULL FOR UPDATE`,
			id, owner.SessionID, waiting).Scan(&requested)
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
		if isSession {
			session, err = scanSession(tx.QueryRowContext(ctx, `UPDATE session SET state_value=$3,
				reason=$4,updated_at=$5,delivery_blocker=COALESCE($7,delivery_blocker),wake_assessment=$8
				WHERE session_id=$1 AND session_id=$2 AND state_value=$6
				RETURNING `+sessionColumns, id, owner.SessionID, SessionFailedState, reason, now, waiting, blockerBytes, assessmentBytes))
		} else {
			dispatch, err = scanDispatch(tx.QueryRowContext(ctx, `UPDATE dispatch AS d SET state_value=CASE WHEN delivery_attempted THEN $9::integer ELSE $3::integer END,
				reason=$4,updated_at=$5,delivery_blocker=COALESCE($7,delivery_blocker),wake_assessment=$8
				WHERE command_id=$1 AND session_id=$2 AND state_value=$6
				RETURNING `+dispatchColumns, id, owner.SessionID, FailedState, reason, now, QueueState, blockerBytes, assessmentBytes, UnknownState))
		}
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		return notify(ctx, tx, owner.SessionID)
	})
	return dispatch, session, remaining, err
}

func wakeTable(isSession bool) (table, key string) {
	if isSession {
		return "session", "session_id"
	}
	return "dispatch", "command_id"
}

func waitingState(isSession bool) int32 {
	if isSession {
		return int32(SessionWaitingState)
	}
	return int32(QueueState)
}
