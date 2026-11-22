package store

import (
	"context"
	"database/sql"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/postgres"
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
	Changes      []DispatchChange
	RecentIssues *dispatcherpb.RecentDispatchIssues
	Through      int64
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
		args := []any{after, through}
		versionRange := "version>$1 AND version<=$2"
		orderAndLimit := " ORDER BY c.version"
		evidence := "admission,delivery_blocker,wake_assessment"
		recentLimit := int(filter.GetRecentIssueLimit())
		if recentLimit != 0 {
			// A replacement describes the current MVCC snapshot. A commit above the frontier
			// may appear early here and will trigger another read when its version is covered.
			args = nil
			versionRange = "TRUE"
			evidence = "NULL::bytea AS admission,NULL::bytea AS delivery_blocker,NULL::bytea AS wake_assessment"
			batch.RecentIssues = &dispatcherpb.RecentDispatchIssues{}
		}
		args = append(args, scopeValue)
		predicate := fmt.Sprintf("%s=$%d", scopeColumn, len(args))
		if len(filter.GetCommandIds()) > 0 {
			args = append(args, pq.Array(filter.GetCommandIds()))
			predicate += " AND c.command_id=ANY($4)"
		}
		owner, ownerArgs := OwnerScope(caller, "o.", len(args)+1)
		args = append(args, ownerArgs...)
		if recentLimit != 0 {
			predicate += fmt.Sprintf(" AND c.state IN ($%d,$%d)", len(args)+1, len(args)+2)
			args = append(args, FailedState, UnknownState, recentLimit+1)
			orderAndLimit = fmt.Sprintf(" ORDER BY c.version DESC LIMIT $%d", len(args))
		}
		rows, err := tx.QueryContext(ctx, `WITH changes AS (
			SELECT version,command_id,session_id,sandbox_name,sandbox_uid::text,operation,state,reason,
				`+evidence+`,created_at,updated_at,false AS deleted
			FROM dispatch WHERE `+versionRange+`
			UNION ALL
			SELECT version,command_id,session_id,''::varchar,''::text,operation,state,reason,
				`+evidence+`,created_at,updated_at,true AS deleted
			FROM dispatch_tombstone WHERE `+versionRange+`
		)
		SELECT c.version,c.command_id,c.session_id,c.sandbox_name,c.sandbox_uid,c.operation,c.state,c.reason,
			c.admission,c.delivery_blocker,c.wake_assessment,c.created_at,c.updated_at
		FROM changes c JOIN session o ON o.session_id=c.session_id
			AND (c.deleted OR (o.sandbox_name=c.sandbox_name AND o.sandbox_uid::text=c.sandbox_uid))
		WHERE `+predicate+" AND "+owner+orderAndLimit, args...)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			if batch.RecentIssues != nil && len(batch.RecentIssues.Receipts) == recentLimit {
				batch.RecentIssues.MoreAvailable = true
				break
			}
			change, err := scanDispatchChange(rows)
			if err != nil {
				return err
			}
			if batch.RecentIssues != nil {
				row := change.Dispatch
				batch.RecentIssues.Receipts = append(batch.RecentIssues.Receipts, &dispatcherpb.DispatchIssue{
					CommandId: row.GetCommandId(), SessionId: row.GetSessionId(), Operation: row.GetOperation(),
					State: row.GetState(), Reason: row.Reason,
				})
			} else {
				batch.Changes = append(batch.Changes, change)
			}
		}
		return rows.Err()
	})
	return batch, err
}

func scanDispatchChange(row interface{ Scan(...any) error }) (DispatchChange, error) {
	var change DispatchChange
	var dispatch Dispatch
	var reason sql.NullString
	var admission, blocker, wake []byte
	if err := row.Scan(&change.Version, &dispatch.CommandID, &dispatch.SessionID,
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
	change.Dispatch, err = DescribeDispatch(&dispatch)
	return change, err
}
