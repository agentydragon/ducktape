package store

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/dispatchstate"
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
	owner, ownerArgs := OwnerScope(caller, "o.", 8)
	const columns = "d.command_id,d.operation_value,d.state_value,LEFT(d.reason,256),COALESCE(length(d.reason)>256,false),d.admission IS NOT NULL,d.created_at,d.updated_at,d.version"
	// Merge only the top rows of each recent state. The latest same-Session receipt wins when old
	// Dispatchers reused an ID. A newer live row can also supersede a tombstone.
	// Literal sets let PostgreSQL use the matching partial indexes with generic plans.
	activeStates := fmt.Sprintf("(%d,%d)", QueueState, DeliveringState)
	recentStates := fmt.Sprintf("(%d,%d,%d,%d)", AdmittedState, FailedState, CancelledState, UnknownState)
	query := `WITH visible AS (
		SELECT o.session_id,o.sandbox_name,o.sandbox_uid FROM session o
		WHERE o.session_id=$1 AND ` + owner + `
	), active(command_id,operation_value,state_value,reason,reason_truncated,journal_admitted,created_at,updated_at,receipt_version,rank,legacy_deletion_receipt,bucket) AS (
		SELECT ` + columns + `,d.position AS rank,false AS legacy_deletion_receipt,0 AS bucket
		FROM dispatch d JOIN visible o USING (session_id,sandbox_name,sandbox_uid)
		WHERE d.state_value IN ` + activeStates + `
		ORDER BY d.position LIMIT $2
	), recent AS (
		SELECT candidate.*,states.bucket
		FROM visible o CROSS JOIN (VALUES ($4::integer,1),($5::integer,1),
			($6::integer,2),($7::integer,2)) AS states(state_value,bucket)
		CROSS JOIN LATERAL (
			(SELECT ` + columns + `,d.version AS rank,false AS legacy_deletion_receipt
			FROM dispatch d
			WHERE d.session_id=o.session_id AND d.sandbox_name=o.sandbox_name AND d.sandbox_uid=o.sandbox_uid
				AND d.state_value IN ` + recentStates + `
				AND d.state_value=states.state_value ORDER BY d.version DESC LIMIT $3)
			UNION ALL
			(SELECT ` + columns + `,d.version AS rank,true AS legacy_deletion_receipt
			FROM dispatch_tombstone d
			WHERE d.session_id=o.session_id AND d.state_value=states.state_value
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
	SELECT command_id,COALESCE(operation_value,0),COALESCE(state_value,0),reason,reason_truncated,journal_admitted,created_at,updated_at,
		receipt_version,legacy_deletion_receipt,bucket
	FROM (SELECT * FROM active UNION ALL SELECT * FROM recent) workset
	ORDER BY bucket,CASE WHEN bucket=0 THEN rank ELSE -rank END`
	err = postgres.ReadTransaction(ctx, s.DB, sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead}, func(tx *sql.Tx) error {
		if err := validateDispatchCoverage(ctx, tx, after, resuming); err != nil {
			return err
		}
		args := append([]any{sessionID, maxActiveWorkset + 1, maxRecentWorkset + 1, FailedState, UnknownState, AdmittedState, CancelledState}, ownerArgs...)
		rows, err := tx.QueryContext(ctx, query, args...)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			item := &dispatcherpb.DispatchWorksetItem{}
			var state dispatcherpb.DeliveryState
			var reason sql.NullString
			var created, updated time.Time
			var version int64
			var bucket int
			if err := rows.Scan(&item.CommandId, &item.Operation, &state, &reason, &item.ReasonTruncated, &item.JournalAdmitted,
				&created, &updated, &version, &item.LegacyDeletionReceipt, &bucket); err != nil {
				return err
			}
			if err := validateDispatchOperation(item.Operation); err != nil {
				return err
			}
			item.State = dispatchstate.NormalizeDeliveryState(state)
			if reason.Valid {
				item.Reason = &reason.String
			}
			item.CreatedAt, item.UpdatedAt = timestamppb.New(created), timestamppb.New(updated)
			item.ReceiptVersion = &version
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
