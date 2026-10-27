package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/google/uuid"
)

// Cancel withdraws a request that no executor has claimed. It confers no authority beyond the owner's: any other
// principal, an operator included, does not find the request. The outcome says why a request that cannot be
// withdrawn was left as it was.
func (s *Store) Cancel(ctx context.Context, id uuid.UUID, principal Principal) (CancellationResult, error) {
	var result CancellationResult
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		row, err := getRequest(ctx, tx, id, true)
		if err != nil {
			return err
		}
		if caller, ok := principal.(CallerPrincipal); !ok || row.Caller != caller.Account {
			return ErrNotFound
		}
		switch row.State {
		case action.StateCancelled:
			result.Outcome = OutcomeAlreadyCancelled
		case action.StateDenied, action.StateSucceeded, action.StateFailed:
			result.Outcome = OutcomeAlreadyFinished
		case action.StateDispatching, action.StateRunning, action.StateExecutionUnknown:
			result.Outcome = OutcomeTooLate
		default:
			at := now()
			if row.State == action.StateAllowed {
				if err := cancelPendingDispatch(ctx, tx, id, at); err != nil {
					return err
				}
			}
			if err := advance(ctx, tx, &row, action.StateCancelled, at, principal); err != nil {
				return err
			}
			result.Outcome = OutcomeCancelled
		}
		views, err := attach(ctx, tx, []requestRow{row})
		if err != nil {
			return err
		}
		result.Request = views[0]
		return nil
	})
	return result, err
}

// cancelPendingDispatch ends the Execution an allowed request awaits dispatch of, so nothing claims it. An allowed
// request whose Execution does not await dispatch is a fault no store call leaves behind.
func cancelPendingDispatch(ctx context.Context, tx *sql.Tx, id uuid.UUID, at time.Time) error {
	execution, err := lockExecution(ctx, tx, id)
	if errors.Is(err, ErrNotFound) || err == nil && execution.State != ExecutionPendingDispatch {
		return fmt.Errorf("allowed request %s has no Execution awaiting dispatch", id)
	}
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx,
		`UPDATE action_execution SET state = $2, completed_at = $3 WHERE request_id = $1`, id, ExecutionCancelled, at)
	return err
}
