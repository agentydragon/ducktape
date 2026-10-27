package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/google/uuid"
)

// Every writer that touches a request and its Execution locks the request first. A writer that took the Execution
// first could deadlock with one that holds the request and waits for the Execution.

// executionLock is an Execution's row, locked for update.
type executionLock struct {
	State ExecutionState
	// Lease is nil until the Execution is claimed.
	Lease *Lease
}

// lockExecution locks the Execution of the request, or returns ErrNotFound.
func lockExecution(ctx context.Context, q querier, id uuid.UUID) (executionLock, error) {
	var lock executionLock
	var state string
	var executorID *string
	var token uuid.NullUUID
	err := q.QueryRowContext(ctx,
		`SELECT state, executor_id, lease_token FROM action_execution WHERE request_id = $1 FOR UPDATE`, id).
		Scan(&state, &executorID, &token)
	if errors.Is(err, sql.ErrNoRows) {
		return executionLock{}, ErrNotFound
	}
	if err != nil {
		return executionLock{}, err
	}
	lock.State = ExecutionState(state)
	if !lock.State.valid() {
		return executionLock{}, fmt.Errorf("execution of request %s has unknown state %q", id, state)
	}
	if executorID != nil && token.Valid {
		lock.Lease = &Lease{ExecutorID: *executorID, Token: token.UUID}
	}
	return lock, nil
}

func (l executionLock) ownedBy(lease Lease) bool { return l.Lease != nil && *l.Lease == lease }

// PendingDispatches returns the requests whose Execution awaits dispatch, oldest first.
func (s *Store) PendingDispatches(ctx context.Context) ([]uuid.UUID, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT request_id FROM action_execution WHERE state = $1 ORDER BY created_at, request_id`, ExecutionPendingDispatch)
	if err != nil {
		return nil, err
	}
	return scanIDs(rows)
}

func scanIDs(rows *sql.Rows) ([]uuid.UUID, error) {
	defer rows.Close()
	var ids []uuid.UUID
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// ClaimExecution reserves the one dispatch of an allowed request for the executor and grants its first lease
// window. The claim is nil, and nothing is claimed, when the Execution no longer awaits dispatch (another claimer
// won, or the request was cancelled) or when canDispatch refuses its Action.
//
// canDispatch runs in the claiming transaction: it must not block or use the database.
func (s *Store) ClaimExecution(ctx context.Context, id uuid.UUID, executorID string, leaseDuration time.Duration, canDispatch func(ActionIdentity) bool) (*ExecutionClaim, error) {
	if executorID == "" || canDispatch == nil {
		return nil, invalid("a claim needs an executor and a dispatch filter")
	}
	var claim *ExecutionClaim
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		row, err := getRequest(ctx, tx, id, true)
		if err != nil {
			return err
		}
		execution, err := lockExecution(ctx, tx, id)
		if err != nil {
			return err
		}
		if execution.State != ExecutionPendingDispatch {
			return nil
		}
		if row.State != action_servicepb.ActionState_ACTION_STATE_ALLOWED {
			return &ConflictError{Reason: ConflictNotDispatchable, Message: fmt.Sprintf("cannot dispatch request in %s", storedState(row.State))}
		}
		if !canDispatch(row.Action) {
			return nil
		}
		at := now()
		started := &ExecutionClaim{
			RequestID: id,
			Lease:     Lease{ExecutorID: executorID, Token: uuid.New()},
			ExpiresAt: at.Add(leaseDuration).Truncate(time.Microsecond),
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE action_execution
			SET state = $2, executor_id = $3, lease_token = $4, lease_expires_at = $5, heartbeat_at = $6
			WHERE request_id = $1`,
			id, ExecutionDispatching, executorID, started.Token, started.ExpiresAt, at); err != nil {
			return err
		}
		if err := advance(ctx, tx, &row, action_servicepb.ActionState_ACTION_STATE_DISPATCHING, at, nil); err != nil {
			return err
		}
		claim = started
		return nil
	})
	return claim, err
}

// HeartbeatExecution renews the lease. False means the caller is not, or is no longer, the lease's owner of an
// Execution in flight.
func (s *Store) HeartbeatExecution(ctx context.Context, id uuid.UUID, lease Lease, leaseDuration time.Duration) (bool, error) {
	var renewed bool
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		execution, err := lockExecution(ctx, tx, id)
		if errors.Is(err, ErrNotFound) {
			return nil
		}
		if err != nil {
			return err
		}
		if !execution.ownedBy(lease) || execution.State != ExecutionDispatching && execution.State != ExecutionRunning {
			return nil
		}
		at := now()
		if _, err := tx.ExecContext(ctx,
			`UPDATE action_execution SET lease_expires_at = $2, heartbeat_at = $3 WHERE request_id = $1`,
			id, at.Add(leaseDuration).Truncate(time.Microsecond), at); err != nil {
			return err
		}
		renewed = true
		return nil
	})
	return renewed, err
}

// MarkRunning crosses the no-replay boundary immediately before the executor is invoked, and returns what to
// invoke it with. From here on an Execution whose lease lapses is unknown, never retried.
func (s *Store) MarkRunning(ctx context.Context, id uuid.UUID) (ExecutionRequest, error) {
	var payload ExecutionRequest
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		row, err := getRequest(ctx, tx, id, true)
		if err != nil {
			return err
		}
		execution, err := lockExecution(ctx, tx, id)
		if err != nil {
			return err
		}
		if row.State != action_servicepb.ActionState_ACTION_STATE_DISPATCHING || execution.State != ExecutionDispatching {
			return &ConflictError{
				Reason:  ConflictNotStartable,
				Message: fmt.Sprintf("cannot start execution in %s/%s", storedState(row.State), execution.State),
			}
		}
		at := now()
		if _, err := tx.ExecContext(ctx,
			`UPDATE action_execution SET state = $2, started_at = $3 WHERE request_id = $1`,
			id, ExecutionRunning, at); err != nil {
			return err
		}
		if err := advance(ctx, tx, &row, action_servicepb.ActionState_ACTION_STATE_RUNNING, at, nil); err != nil {
			return err
		}
		payload = ExecutionRequest{RequestID: row.ID, Action: row.Action, Arguments: row.Arguments, Caller: row.Caller}
		return nil
	})
	return payload, err
}

// FinishExecution records the terminal outcome of the attempt that holds the lease.
//
// An outcome that arrives after the lease lapsed and the sweep ended the Execution as unknown is accepted as a
// reconciliation, still gated on the same lease: it never starts a second effect, it records the truth about the
// one attempt made.
func (s *Store) FinishExecution(ctx context.Context, id uuid.UUID, lease Lease, outcome ExecutionOutcome) error {
	if err := outcome.Validate(); err != nil {
		return err
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		row, err := getRequest(ctx, tx, id, true)
		if err != nil {
			return err
		}
		execution, err := lockExecution(ctx, tx, id)
		if err != nil {
			return err
		}
		if !execution.ownedBy(lease) {
			return &ConflictError{
				Reason:  ConflictLeaseNotOwned,
				Message: "caller is not the recognized owner of this execution's lease",
			}
		}
		at := now()
		var completedAt, reconciledAt *time.Time
		switch execution.State {
		case ExecutionDispatching, ExecutionRunning:
			completedAt = &at
		case ExecutionUnknown:
			reconciledAt = &at
		default:
			return &ConflictError{Reason: ConflictNotFinishable, Message: fmt.Sprintf("cannot finish execution in %s", execution.State)}
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE action_execution
			SET state = $2, result = $3::jsonb, error = $4::jsonb, completed_at = COALESCE($5, completed_at),
				reconciled_at = COALESCE($6, reconciled_at)
			WHERE request_id = $1`,
			id, outcome.State, jsonParam(jsonValue(outcome.Result)), jsonParam(jsonValue(outcome.Error)), completedAt,
			reconciledAt); err != nil {
			return err
		}
		return advance(ctx, tx, &row, terminalActionStates[outcome.State], at, nil)
	})
}

// ExpireStaleLeases ends the Executions in flight whose lease lapsed as unknown, and returns their requests in ID
// order. It never replays them: the effect may have happened.
//
// The recorded reason tells an operator whether the executor is gone (UnknownExecutorLost: its own health heartbeat
// is also older than executorHealthTimeout) or only this attempt stopped renewing (UnknownLeaseExpired). Both are
// equally final.
func (s *Store) ExpireStaleLeases(ctx context.Context, executorHealthTimeout time.Duration) ([]uuid.UUID, error) {
	var expired []uuid.UUID
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		at := now()
		rows, err := tx.QueryContext(ctx, `
			SELECT request_id FROM action_execution
			WHERE state IN ('dispatching', 'running') AND lease_expires_at < $1 ORDER BY request_id`, at)
		if err != nil {
			return err
		}
		candidates, err := scanIDs(rows)
		if err != nil {
			return err
		}
		for _, id := range candidates {
			row, err := getRequest(ctx, tx, id, true)
			if err != nil {
				return err
			}
			// The Execution may have finished or renewed while this waited for the request.
			var executorID *string
			err = tx.QueryRowContext(ctx, `
				SELECT executor_id FROM action_execution
				WHERE request_id = $1 AND state IN ('dispatching', 'running') AND lease_expires_at < $2 FOR UPDATE`,
				id, at).Scan(&executorID)
			if errors.Is(err, sql.ErrNoRows) {
				continue
			}
			if err != nil {
				return err
			}
			silent, err := executorSilentSince(ctx, tx, executorID, at.Add(-executorHealthTimeout))
			if err != nil {
				return err
			}
			reason := UnknownLeaseExpired
			if silent {
				reason = UnknownExecutorLost
			}
			if _, err := tx.ExecContext(ctx,
				`UPDATE action_execution SET state = $2, error = $3::jsonb, completed_at = $4 WHERE request_id = $1`,
				id, ExecutionUnknown, unknownOutcomeError(reason), at); err != nil {
				return err
			}
			if err := advance(ctx, tx, &row, action_servicepb.ActionState_ACTION_STATE_EXECUTION_UNKNOWN, at, nil); err != nil {
				return err
			}
			expired = append(expired, id)
		}
		return nil
	})
	return expired, err
}

// executorSilentSince reports whether the executor has not recorded a heartbeat since the time, or ever.
func executorSilentSince(ctx context.Context, q querier, executorID *string, since time.Time) (bool, error) {
	if executorID == nil {
		return true, nil
	}
	var heartbeatAt time.Time
	err := q.QueryRowContext(ctx,
		`SELECT heartbeat_at FROM action_executor_heartbeat WHERE executor_id = $1`, *executorID).Scan(&heartbeatAt)
	if errors.Is(err, sql.ErrNoRows) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	return heartbeatAt.Before(since), nil
}

// RecordExecutorHeartbeat notes that the executor is alive, apart from any one attempt's lease.
func (s *Store) RecordExecutorHeartbeat(ctx context.Context, executorID string) error {
	if executorID == "" {
		return invalid("an executor heartbeat needs an executor")
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO action_executor_heartbeat (executor_id, started_at, heartbeat_at) VALUES ($1, $2, $2)
		ON CONFLICT (executor_id) DO UPDATE SET heartbeat_at = EXCLUDED.heartbeat_at`, executorID, now())
	return err
}
