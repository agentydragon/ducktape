package store

import (
	"context"
	"database/sql"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	maxActiveWorkset = 1024
	maxRecentWorkset = 20
	activeBucket     = 0
	issueBucket      = 1
	admittedBucket   = 2
)

// ReadDispatchWorkset returns a replacement snapshot. Its checkpoint only
// tracks legacy tombstone coverage. A row committed beyond this checkpoint can
// be shown early and its Session notification triggers another snapshot.
func (s *Store) ReadDispatchWorkset(ctx context.Context, sessionID string, caller Caller, after int64, resuming bool) (*dispatcherpb.DispatchWorkset, int64, error) {
	through, err := s.dispatchWatchFrontier(ctx, after, resuming)
	if err != nil {
		return nil, 0, err
	}
	result := &dispatcherpb.DispatchWorkset{SessionId: sessionID}
	owner, ownerArgs := OwnerScope(caller, "o.", 6)
	const columns = "d.command_id,d.operation,d.state,LEFT(d.reason,256),COALESCE(length(d.reason)>256,false),d.admission IS NOT NULL,d.created_at,d.updated_at"
	// Merge only the top rows of each recent state. The latest same-Session receipt wins when old
	// Dispatchers reused an ID. A newer live row can also supersede a tombstone.
	query := `WITH visible AS (
		SELECT o.session_id,o.sandbox_name,o.sandbox_uid FROM session o
		WHERE o.session_id=$1 AND ` + owner + `
	), active(command_id,operation,state,reason,reason_truncated,journal_admitted,created_at,updated_at,rank,bucket) AS (
		SELECT ` + columns + `,d.position AS rank,0 AS bucket
		FROM dispatch d JOIN visible o USING (session_id,sandbox_name,sandbox_uid)
		WHERE d.state IN ('QUEUED_IN_DISPATCHER','DELIVERING_TO_RUNNER')
		ORDER BY d.position LIMIT $2
	), recent AS (
		SELECT candidate.*,states.bucket
		FROM visible o CROSS JOIN (VALUES ($4::varchar,1),($5::varchar,1),
			('RUNNER_ADMITTED',2),('CANCELLED_IN_DISPATCHER',2)) AS states(state,bucket)
		CROSS JOIN LATERAL (
			(SELECT ` + columns + `,d.version AS rank
			FROM dispatch d
			WHERE d.session_id=o.session_id AND d.sandbox_name=o.sandbox_name AND d.sandbox_uid=o.sandbox_uid
				AND d.state IN ('FAILED','UNKNOWN','RUNNER_ADMITTED','CANCELLED_IN_DISPATCHER')
				AND d.state=states.state ORDER BY d.version DESC LIMIT $3)
			UNION ALL
			(SELECT ` + columns + `,d.version AS rank
			FROM dispatch_tombstone d
			WHERE d.session_id=o.session_id AND d.state=states.state
				AND NOT EXISTS (
					SELECT 1 FROM dispatch live
					WHERE live.command_id=d.command_id AND live.session_id=o.session_id
						AND live.sandbox_name=o.sandbox_name AND live.sandbox_uid=o.sandbox_uid)
				AND NOT EXISTS (
					SELECT 1 FROM dispatch_tombstone newer
					WHERE newer.command_id=d.command_id AND newer.session_id=d.session_id AND newer.version>d.version)
			ORDER BY d.version DESC LIMIT $3)
		) candidate
	)
	SELECT command_id,operation,state,reason,reason_truncated,journal_admitted,created_at,updated_at,bucket
	FROM (SELECT * FROM active UNION ALL SELECT * FROM recent) workset
	ORDER BY bucket,CASE WHEN bucket=0 THEN rank ELSE -rank END`
	err = postgres.ReadTransaction(ctx, s.DB, sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead}, func(tx *sql.Tx) error {
		if err := validateDispatchCoverage(ctx, tx, after, resuming); err != nil {
			return err
		}
		args := append([]any{sessionID, maxActiveWorkset + 1, maxRecentWorkset + 1, FailedState, UnknownState}, ownerArgs...)
		rows, err := tx.QueryContext(ctx, query, args...)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			item := &dispatcherpb.DispatchWorksetItem{}
			var operation, state string
			var reason sql.NullString
			var created, updated time.Time
			var bucket int
			if err := rows.Scan(&item.CommandId, &operation, &state, &reason, &item.ReasonTruncated, &item.JournalAdmitted,
				&created, &updated, &bucket); err != nil {
				return err
			}
			item.Operation, err = dispatchOperation(operation)
			if err != nil {
				return err
			}
			item.State = dispatchState(state)
			if reason.Valid {
				item.Reason = &reason.String
			}
			item.CreatedAt, item.UpdatedAt = timestamppb.New(created), timestamppb.New(updated)
			switch bucket {
			case activeBucket:
				appendWorksetItem(item, &result.Active, &result.MoreActiveAvailable, maxActiveWorkset)
			case issueBucket:
				appendWorksetItem(item, &result.RecentIssues, &result.MoreRecentIssuesAvailable, maxRecentWorkset)
			case admittedBucket:
				appendWorksetItem(item, &result.RecentAdmittedOrCancelled, &result.MoreRecentAdmittedOrCancelledAvailable, maxRecentWorkset)
			}
		}
		return rows.Err()
	})
	return result, through, err
}

func appendWorksetItem(item *dispatcherpb.DispatchWorksetItem, items *[]*dispatcherpb.DispatchWorksetItem, more *bool, limit int) {
	if len(*items) == limit {
		*more = true
		return
	}
	*items = append(*items, item)
}
