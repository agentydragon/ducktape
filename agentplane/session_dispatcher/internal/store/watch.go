package store

import (
	"context"
	"database/sql"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/lib/pq"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type DispatchChange struct {
	Version  int64
	Dispatch *dispatcherpb.Dispatch
}

type DispatchChanges struct {
	Changes []DispatchChange
	Through int64
}

// ReadDispatchChanges fences sequence allocation briefly, then reads the bounded batch
// without blocking writers. Later updates may coalesce a row into the next batch.
func (s *Store) ReadDispatchChanges(ctx context.Context, filter *dispatcherpb.DispatchFilter, caller Caller, after int64, resuming bool) (DispatchChanges, error) {
	through, err := s.dispatchWatchFrontier(ctx, after, resuming)
	if err != nil {
		return DispatchChanges{}, err
	}
	return s.readDispatchChangesThrough(ctx, filter, caller, after, through, resuming)
}

// dispatchWatchFrontier drains writers that could have allocated lower versions before
// checkpointing. Only indexed maxima and the singleton retention floor are read under SHARE;
// the transaction commits before the receipt query or any stream send begins.
func (s *Store) dispatchWatchFrontier(ctx context.Context, after int64, resuming bool) (int64, error) {
	var through int64
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "LOCK TABLE dispatch, dispatch_tombstone IN SHARE MODE"); err != nil {
			return err
		}
		if err := validateDispatchCoverage(ctx, tx, after, resuming); err != nil {
			return err
		}
		if err := tx.QueryRowContext(ctx, `SELECT GREATEST(
			COALESCE((SELECT max(version) FROM dispatch),0),
			COALESCE((SELECT max(version) FROM dispatch_tombstone),0),
			(SELECT pruned_through FROM dispatch_watch_retention WHERE singleton))`).Scan(&through); err != nil {
			return err
		}
		if after > through {
			return status.Error(codes.InvalidArgument, "resume_token is ahead of the dispatch checkpoint")
		}
		return nil
	})
	return through, err
}

func validateDispatchCoverage(ctx context.Context, tx *sql.Tx, after int64, resuming bool) error {
	var pruned int64
	if err := tx.QueryRowContext(ctx, `SELECT pruned_through FROM dispatch_watch_retention WHERE singleton`).Scan(&pruned); err != nil {
		return err
	}
	if resuming && after < pruned {
		return status.Error(codes.OutOfRange, "dispatch deletion coverage expired; resync with a fresh snapshot by clearing resume_token")
	}
	return nil
}

func (s *Store) readDispatchChangesThrough(ctx context.Context, filter *dispatcherpb.DispatchFilter, caller Caller, after, through int64, resuming bool) (DispatchChanges, error) {
	batch := DispatchChanges{Through: through}
	// Pruning can commit after the frontier lock is released. Check coverage and read
	// receipts in one MVCC snapshot, so a concurrent prune cannot silently erase a
	// deletion from this batch. ACCESS SHARE permits ordinary dispatch writes/pruning.
	err := postgres.ReadTransaction(ctx, s.DB, sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead}, func(tx *sql.Tx) error {
		if err := validateDispatchCoverage(ctx, tx, after, resuming); err != nil {
			return err
		}
		// Retained Sessions supply the same immutable owner and scope for both sources.
		// Tombstones deliberately store and return no Sandbox address. Get/List still read
		// only live rows; watches also retain terminal outcomes across reconnects.
		scopeColumn, scopeValue := "c.session_id", filter.GetSessionId()
		if filter.GetSandbox() != "" {
			scopeColumn, scopeValue = "o.sandbox_name", filter.GetSandbox()
		}
		args := []any{after, through, scopeValue, filter.GetIncludeCommand()}
		predicate := scopeColumn + "=$3"
		if len(filter.GetCommandIds()) > 0 {
			args = append(args, pq.Array(filter.GetCommandIds()))
			predicate += " AND c.command_id=ANY($5)"
		}
		owner, ownerArgs := OwnerScope(caller, "o.", len(args)+1)
		args = append(args, ownerArgs...)
		rows, err := tx.QueryContext(ctx, `WITH changes AS (
			SELECT version,command_id,session_id,sandbox_name,sandbox_uid::text,operation,state,reason,
				admission,delivery_blocker,wake_assessment,created_at,updated_at,false AS deleted,CASE WHEN $4 THEN command END AS command
			FROM dispatch WHERE version>$1 AND version<=$2
			UNION ALL
			SELECT version,command_id,session_id,''::varchar,''::text,operation,state,reason,
				admission,delivery_blocker,wake_assessment,created_at,updated_at,true AS deleted,NULL::bytea AS command
			FROM dispatch_tombstone WHERE version>$1 AND version<=$2
		)
		SELECT c.version,c.command_id,c.session_id,c.sandbox_name,c.sandbox_uid,c.operation,c.state,c.reason,
			c.admission,c.delivery_blocker,c.wake_assessment,c.created_at,c.updated_at,c.command
		FROM changes c JOIN session o ON o.session_id=c.session_id
			AND (c.deleted OR (o.sandbox_name=c.sandbox_name AND o.sandbox_uid::text=c.sandbox_uid))
		WHERE `+predicate+" AND "+owner+" ORDER BY c.version", args...)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			change, err := scanDispatchChange(rows, filter.GetIncludeReceiptVersion())
			if err != nil {
				return err
			}
			batch.Changes = append(batch.Changes, change)
		}
		return rows.Err()
	})
	return batch, err
}

func scanDispatchChange(row interface{ Scan(...any) error }, includeReceiptVersion bool) (DispatchChange, error) {
	var change DispatchChange
	var dispatch Dispatch
	var reason sql.NullString
	var admission, blocker, wake, command []byte
	if err := row.Scan(&change.Version, &dispatch.CommandID, &dispatch.SessionID,
		&dispatch.SandboxName, &dispatch.SandboxUID, &dispatch.Operation, &dispatch.State,
		&reason, &admission, &blocker, &wake, &dispatch.CreatedAt, &dispatch.UpdatedAt, &command); err != nil {
		return change, err
	}
	dispatch.Reason = reason.String
	if command != nil {
		dispatch.Command = &commandpb.Command{}
		if err := proto.Unmarshal(command, dispatch.Command); err != nil {
			return change, fmt.Errorf("decode stored command: %w", err)
		}
	}
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
	change.Dispatch, err = DescribeDispatch(&dispatch, true)
	if err == nil && includeReceiptVersion {
		change.Dispatch.ReceiptVersion = proto.Int64(change.Version)
	}
	return change, err
}
