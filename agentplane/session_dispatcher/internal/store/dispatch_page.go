package store

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"

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

func (s *Store) ReadDispatchPage(ctx context.Context, sessionID string, caller Caller, limit int, cursor DispatchPageCursor) (*dispatcherpb.ListSessionDispatchesResponse, DispatchPageCursor, error) {
	if cursor.Before == 0 {
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
		name, uid, err := dispatchSessionAddress(ctx, tx, sessionID, caller)
		if err != nil {
			return err
		}
		query := `SELECT position,command_id,operation,state,reason,created_at,updated_at,version
			FROM dispatch WHERE session_id=$1 AND sandbox_name=$2 AND sandbox_uid=$3
			AND ($4::bigint=0 OR position<$4)`
		params := []any{sessionID, name, uid, cursor.Before, limit + 1}
		var deleted bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS (SELECT FROM dispatch_tombstone WHERE version>$1)
				OR (SELECT pruned_through>$1 FROM dispatch_watch_retention WHERE singleton)`, cursor.Frontier).Scan(&deleted); err != nil {
			return err
		}
		if deleted {
			return status.Error(codes.OutOfRange, "dispatch rows were deleted; restart without a page token")
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

func dispatchSessionAddress(ctx context.Context, tx *sql.Tx, sessionID string, caller Caller) (name, uid string, err error) {
	scope, args := OwnerScope(caller, "", 2)
	err = tx.QueryRowContext(ctx, "SELECT sandbox_name,sandbox_uid::text FROM session WHERE session_id=$1 AND "+scope,
		append([]any{sessionID}, args...)...).Scan(&name, &uid)
	if errors.Is(err, sql.ErrNoRows) {
		err = ErrNotFound
	}
	return name, uid, err
}

func (s *Store) BatchGetDispatches(ctx context.Context, sessionID string, caller Caller, ids []string) (*dispatcherpb.BatchGetDispatchesResponse, error) {
	response := &dispatcherpb.BatchGetDispatchesResponse{}
	err := postgres.ReadTransaction(ctx, s.DB, sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead}, func(tx *sql.Tx) error {
		name, uid, err := dispatchSessionAddress(ctx, tx, sessionID, caller)
		if err != nil {
			return err
		}
		// Mixed-version writers may still issue DELETE. Require migration 29's protection
		// before serving this method's retained-record contract.
		var protected bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS (
                SELECT FROM pg_trigger WHERE tgrelid='dispatch'::regclass
                AND tgname='keep_session_command_on_environment_deletion'
                AND tgfoid=to_regprocedure('keep_session_command_on_environment_deletion()')
                AND tgenabled IN ('O','A'))`).Scan(&protected); err != nil {
			return err
		}
		if !protected {
			return status.Error(codes.FailedPrecondition, "retained Dispatch reads are unavailable")
		}
		rows, err := tx.QueryContext(ctx, `SELECT version,command_id,session_id,sandbox_name,sandbox_uid::text,
            operation,state,reason,admission,delivery_blocker,wake_assessment,created_at,updated_at,command,false
            FROM dispatch WHERE session_id=$1 AND sandbox_name=$2 AND sandbox_uid=$3 AND command_id=ANY($4)
            UNION ALL
            SELECT t.version,t.command_id,t.session_id,''::varchar,''::text,t.operation,t.state,t.reason,t.admission,
                t.delivery_blocker,t.wake_assessment,t.created_at,t.updated_at,NULL::bytea,true
            FROM unnest($4::varchar[]) AS requested(command_id)
            CROSS JOIN LATERAL (
                SELECT version,command_id,session_id,operation,state,reason,admission,delivery_blocker,
                    wake_assessment,created_at,updated_at
                FROM dispatch_tombstone
                WHERE command_id=requested.command_id AND session_id=$1
                ORDER BY version DESC LIMIT 1
            ) AS t
            WHERE NOT EXISTS (SELECT FROM dispatch d WHERE d.command_id=requested.command_id AND d.session_id=$1
                AND d.sandbox_name=$2 AND d.sandbox_uid=$3)`, sessionID, name, uid, pq.Array(ids))
		if err != nil {
			return err
		}
		defer rows.Close()
		byID := make(map[string]*dispatcherpb.BatchGetDispatchesResponse_Result, len(ids))
		for _, id := range ids {
			result := &dispatcherpb.BatchGetDispatchesResponse_Result{CommandId: id, Result: &dispatcherpb.BatchGetDispatchesResponse_Result_NotFound{NotFound: &emptypb.Empty{}}}
			byID[id] = result
			response.Results = append(response.Results, result)
		}
		for rows.Next() {
			change, err := scanDispatchChange(rows)
			if err != nil {
				return err
			}
			row := change.Dispatch
			row.ReceiptVersion = &change.Version
			result := byID[row.CommandId]
			if change.Deleted {
				result.Result = &dispatcherpb.BatchGetDispatchesResponse_Result_LegacyDeletionReceipt{LegacyDeletionReceipt: row}
			} else {
				if row.Command == nil || row.Command.GetCommandId() != row.CommandId {
					return status.Error(codes.DataLoss, "stored Dispatch has no matching original command")
				}
				result.Result = &dispatcherpb.BatchGetDispatchesResponse_Result_Dispatch{Dispatch: row}
			}
			// Stop materializing originals when the response reaches the gRPC receiver limit.
			if proto.Size(response) > 4<<20 {
				return status.Error(codes.ResourceExhausted, "Dispatch batch exceeds 4 MiB")
			}
		}
		return rows.Err()
	})
	if err != nil {
		return nil, err
	}
	return response, nil
}
