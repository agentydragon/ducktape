package dispatcher

import (
	"context"
	"database/sql"
	"fmt"

	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/lib/pq"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type dispatchChange struct {
	version  int64
	dispatch *dispatcherpb.Dispatch
}

type dispatchChanges struct {
	changes []dispatchChange
	through int64
}

// readDispatchChanges returns a stable batch, completing its transaction before the caller
// sends any frames. Sequence allocation is not commit order: SHARE locks drain writers that
// may have allocated lower versions, and block new versions until the checkpoint is read.
func (s *Store) readDispatchChanges(ctx context.Context, filter *dispatcherpb.DispatchFilter, caller Caller, after int64, resuming bool) (dispatchChanges, error) {
	var batch dispatchChanges
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "LOCK TABLE dispatch, dispatch_tombstone IN SHARE MODE"); err != nil {
			return err
		}
		var pruned int64
		if err := tx.QueryRowContext(ctx, `SELECT pruned_through FROM dispatch_watch_retention WHERE singleton`).Scan(&pruned); err != nil {
			return err
		}
		if resuming && after < pruned {
			return status.Error(codes.OutOfRange, "dispatch deletion coverage expired; resync with a fresh snapshot by clearing resume_token")
		}
		if err := tx.QueryRowContext(ctx, `SELECT GREATEST(
			COALESCE((SELECT max(version) FROM dispatch),0),
			COALESCE((SELECT max(version) FROM dispatch_tombstone),0),$1::bigint)`, pruned).Scan(&batch.through); err != nil {
			return err
		}
		if after > batch.through {
			return status.Error(codes.InvalidArgument, "resume_token is ahead of the dispatch checkpoint")
		}
		// Retained Sessions supply the same immutable owner and scope for both sources.
		// Tombstones deliberately store and return no Sandbox address. Get/List still read
		// only live rows; watches also retain terminal outcomes across reconnects.
		scopeColumn, scopeValue := "c.session_id", filter.GetSessionId()
		if filter.GetSandbox() != "" {
			scopeColumn, scopeValue = "o.sandbox_name", filter.GetSandbox()
		}
		args := []any{after, scopeValue}
		predicate := scopeColumn + "=$2"
		if len(filter.GetCommandIds()) > 0 {
			args = append(args, pq.Array(filter.GetCommandIds()))
			predicate += " AND c.command_id=ANY($3)"
		}
		owner, ownerArgs := ownerScope(caller, "o.", len(args)+1)
		args = append(args, ownerArgs...)
		rows, err := tx.QueryContext(ctx, `WITH changes AS (
			SELECT version,command_id,session_id,sandbox_name,sandbox_uid::text,operation,state,reason,
				admission,delivery_blocker,wake_assessment,created_at,updated_at,false AS deleted
			FROM dispatch WHERE version>$1
			UNION ALL
			SELECT version,command_id,session_id,''::varchar,''::text,operation,state,reason,
				admission,delivery_blocker,wake_assessment,created_at,updated_at,true AS deleted
			FROM dispatch_tombstone WHERE version>$1
		)
		SELECT c.version,c.command_id,c.session_id,c.sandbox_name,c.sandbox_uid,c.operation,c.state,c.reason,
			c.admission,c.delivery_blocker,c.wake_assessment,c.created_at,c.updated_at
		FROM changes c JOIN session o ON o.session_id=c.session_id
			AND (c.deleted OR (o.sandbox_name=c.sandbox_name AND o.sandbox_uid::text=c.sandbox_uid))
		WHERE `+predicate+" AND "+owner+" ORDER BY c.version", args...)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			change, err := scanDispatchChange(rows)
			if err != nil {
				return err
			}
			batch.changes = append(batch.changes, change)
		}
		return rows.Err()
	})
	return batch, err
}

func scanDispatchChange(row interface{ Scan(...any) error }) (dispatchChange, error) {
	var change dispatchChange
	var dispatch Dispatch
	var reason sql.NullString
	var admission, blocker, wake []byte
	if err := row.Scan(&change.version, &dispatch.CommandID, &dispatch.SessionID,
		&dispatch.SandboxName, &dispatch.SandboxUID, &dispatch.Operation, &dispatch.State,
		&reason, &admission, &blocker, &wake, &dispatch.CreatedAt, &dispatch.UpdatedAt); err != nil {
		return change, err
	}
	dispatch.Reason = reason.String
	if admission != nil {
		dispatch.Admission = &eventlogpb.EventEntry{}
		if err := proto.Unmarshal(admission, dispatch.Admission); err != nil {
			return change, fmt.Errorf("decode retained dispatch admission: %w", err)
		}
	}
	var err error
	dispatch.DeliveryBlocker, err = unmarshalDeliveryBlocker(blocker)
	if err != nil {
		return change, err
	}
	dispatch.WakeAssessment, err = unmarshalWakeAssessment(wake)
	if err != nil {
		return change, err
	}
	change.dispatch, err = DescribeDispatch(&dispatch)
	return change, err
}
