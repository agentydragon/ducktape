package store

import (
	"context"
	"database/sql"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

// Approvals returns every request awaiting a Decision and each request of ids, whatever its state, oldest first: what
// an operator's notification shows, and where the requests it showed went since.
func (s *Store) Approvals(ctx context.Context, ids []uuid.UUID) ([]ActionRequest, error) {
	values := make(pq.StringArray, len(ids))
	for i, id := range ids {
		values[i] = id.String()
	}
	var requests []ActionRequest
	err := s.read(ctx, func(tx *sql.Tx) error {
		rows, err := scanRequests(ctx, tx, `SELECT `+requestColumns+` FROM action_request
			WHERE state = $1 OR id = ANY($2::uuid[]) ORDER BY created_at, id`, action.StateDecisionPending, values)
		if err != nil {
			return err
		}
		requests, err = attach(ctx, tx, rows)
		return err
	})
	return requests, err
}
