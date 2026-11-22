package store

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/lib/pq"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// A continuation only needs the last immutable submission position. All command
// writers for one Session take sessionLock before allocating their positions.
type DispatchPageCursor struct {
	Before, Frontier int64
}

func (s *Store) ReadDispatchPage(ctx context.Context, sessionID string, caller Caller, limit int, cursor DispatchPageCursor, verifyIDs []string) (*dispatcherpb.ListSessionDispatchesResponse, DispatchPageCursor, error) {
	if cursor.Before == 0 && len(verifyIDs) == 0 {
		// Drain writers that may already have allocated an earlier deletion version.
		// Subsequent physical deletions must advance the tombstone maximum or prune floor.
		var err error
		cursor.Frontier, err = s.dispatchWatchFrontier(ctx, 0, false)
		if err != nil {
			return nil, DispatchPageCursor{}, err
		}
	}
	result := &dispatcherpb.ListSessionDispatchesResponse{Coverage: dispatcherpb.ListSessionDispatchesResponse_RETAINED_DISPATCH_ROWS}
	var next DispatchPageCursor
	err := postgres.ReadTransaction(ctx, s.DB, sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead}, func(tx *sql.Tx) error {
		scope, args := OwnerScope(caller, "", 2)
		var name, uid string
		err := tx.QueryRowContext(ctx, "SELECT sandbox_name,sandbox_uid::text FROM session WHERE session_id=$1 AND "+scope,
			append([]any{sessionID}, args...)...).Scan(&name, &uid)
		if errors.Is(err, sql.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		query := `SELECT position,command_id,operation,state,reason,created_at,updated_at,version
			FROM dispatch WHERE session_id=$1 AND sandbox_name=$2 AND sandbox_uid=$3
			AND ($4::bigint=0 OR position<$4)`
		params := []any{sessionID, name, uid, cursor.Before, limit + 1}
		if len(verifyIDs) > 0 {
			// A mixed-version replica can still issue its old DELETE. The database trigger from
			// migration 29 must protect the exact rows this transaction can enumerate.
			var protected bool
			if err := tx.QueryRowContext(ctx, `SELECT EXISTS (
				SELECT FROM pg_trigger WHERE tgrelid='dispatch'::regclass
				AND tgname='keep_session_command_on_environment_deletion'
				AND tgfoid=to_regprocedure('keep_session_command_on_environment_deletion()')
				AND tgenabled IN ('O','A'))`).Scan(&protected); err != nil {
				return err
			}
			result.RetainsCommandsAfterEnvironmentDeletion = &protected
			query += " AND command_id=ANY($6)"
			params = append(params, pq.Array(verifyIDs))
		} else {
			var deleted bool
			if err := tx.QueryRowContext(ctx, `SELECT EXISTS (SELECT FROM dispatch_tombstone WHERE version>$1)
				OR (SELECT pruned_through>$1 FROM dispatch_watch_retention WHERE singleton)`, cursor.Frontier).Scan(&deleted); err != nil {
				return err
			}
			if deleted {
				return status.Error(codes.OutOfRange, "dispatch rows were deleted; restart without a page token")
			}
		}
		rows, err := tx.QueryContext(ctx, query+" ORDER BY position DESC LIMIT $5", params...)
		if err != nil {
			return err
		}
		defer rows.Close()
		var last int64
		for rows.Next() {
			if len(result.Dispatches) == limit {
				next = DispatchPageCursor{Before: last, Frontier: cursor.Frontier}
				break
			}
			summary := &dispatcherpb.Dispatch{SessionId: sessionID, Sandbox: name, SandboxUid: uid}
			var operation, state string
			var created, updated time.Time
			if err := rows.Scan(&last, &summary.CommandId, &operation, &state, &summary.Reason, &created, &updated, &summary.ReceiptVersion); err != nil {
				return err
			}
			summary.Operation, err = dispatchOperation(operation)
			if err != nil {
				return err
			}
			summary.State = dispatchState(state)
			summary.CreatedAt, summary.UpdatedAt = timestamppb.New(created), timestamppb.New(updated)
			result.Dispatches = append(result.Dispatches, summary)
		}
		return rows.Err()
	})
	return result, next, err
}
