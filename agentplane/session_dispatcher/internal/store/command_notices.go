package store

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/lib/pq"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// noticeSession checks retained Session read authority in the transaction that reads or writes attention.
func noticeSession(ctx context.Context, tx *sql.Tx, id string, caller Caller, lock bool) (name, uid string, err error) {
	scope, args := OwnerScope(caller, "", 2)
	query := "SELECT sandbox_name,sandbox_uid::text FROM session WHERE session_id=$1 AND " + scope
	if lock {
		query += " FOR SHARE"
	}
	err = tx.QueryRowContext(ctx, query, append([]any{id}, args...)...).Scan(&name, &uid)
	if errors.Is(err, sql.ErrNoRows) {
		err = ErrNotFound
	}
	return name, uid, err
}

func (s *Store) AcknowledgeCommandNotice(ctx context.Context, id string, caller Caller, n *dispatcherpb.CommandNoticeRevision) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		name, uid, err := noticeSession(ctx, tx, id, caller, true)
		if err != nil {
			return err
		}
		// A live receipt supersedes a historical deletion for the same Session/command.
		// Otherwise the latest same-Session deletion receipt is the one visible in the workset.
		// Read its revision and state in a single snapshot; even a concurrent change can
		// only acknowledge that exact snapshot, never the next one.
		res, err := tx.ExecContext(ctx, `WITH receipt AS (
			(SELECT version,state,0 AS priority FROM dispatch WHERE session_id=$1 AND command_id=$2
				AND sandbox_name=$4 AND sandbox_uid=$5)
			UNION ALL
			(SELECT version,state,1 AS priority FROM dispatch_tombstone WHERE session_id=$1 AND command_id=$2
				ORDER BY version DESC LIMIT 1)
		), current_receipt AS (SELECT version,state FROM receipt ORDER BY priority LIMIT 1)
		INSERT INTO command_notice_acknowledgement(session_id,command_id,receipt_version)
		SELECT $1,$2,version FROM current_receipt WHERE version=$3 AND state IN ('FAILED','UNKNOWN')
		ON CONFLICT (session_id,receipt_version) DO NOTHING`, id, n.CommandId, n.ReceiptVersion, name, uid)
		if err != nil {
			return err
		}
		count, err := res.RowsAffected()
		if err != nil || count == 1 {
			return err
		}
		// A repeated request remains idempotent even after a superseding update or tombstone pruning.
		var recorded bool
		if err := tx.QueryRowContext(ctx, `SELECT EXISTS(SELECT FROM command_notice_acknowledgement
			WHERE session_id=$1 AND command_id=$2 AND receipt_version=$3)`, id, n.CommandId, n.ReceiptVersion).Scan(&recorded); err != nil {
			return err
		}
		if !recorded {
			return status.Error(codes.FailedPrecondition, "no matching current failed or unknown receipt revision")
		}
		return nil
	})
}

// NoticeAcknowledgements rechecks Session scope on each bounded lookup or history page.
// An acknowledgement row has no delivery state, command body, reason or journal evidence.
func (s *Store) NoticeAcknowledgements(ctx context.Context, id string, caller Caller, selected []*dispatcherpb.CommandNoticeRevision, before int64, limit int) (*dispatcherpb.CommandNoticeAcknowledgements, int64, error) {
	if limit < 1 || limit > 100 || len(selected) > 100 {
		return nil, 0, status.Error(codes.InvalidArgument, "notice lookup size must be from 1 to 100")
	}
	result := &dispatcherpb.CommandNoticeAcknowledgements{}
	var next int64
	err := postgres.ReadTransaction(ctx, s.DB, sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead}, func(tx *sql.Tx) error {
		if _, _, err := noticeSession(ctx, tx, id, caller, false); err != nil {
			return err
		}
		query := `SELECT acknowledgement_id,command_id,receipt_version,acknowledged_at
			FROM command_notice_acknowledgement WHERE session_id=$1`
		args := []any{id}
		if len(selected) > 0 {
			commands := make([]string, len(selected))
			versions := make([]int64, len(selected))
			for i, n := range selected {
				commands[i] = n.CommandId
				versions[i] = n.ReceiptVersion
			}
			query += " AND (command_id,receipt_version) IN (SELECT * FROM unnest($2::text[],$3::bigint[]))"
			args = append(args, pq.Array(commands), pq.Array(versions))
		} else {
			args = append(args, limit+1)
			if before != 0 {
				query += " AND acknowledgement_id<$3"
				args = append(args, before)
			}
			query += " ORDER BY acknowledgement_id DESC LIMIT $2"
		}
		rows, err := tx.QueryContext(ctx, query, args...)
		if err != nil {
			return err
		}
		defer rows.Close()
		var last int64
		for rows.Next() {
			if len(result.Acknowledgements) == limit {
				next = last
				break
			}
			n := &dispatcherpb.CommandNoticeRevision{}
			var at time.Time
			if err := rows.Scan(&last, &n.CommandId, &n.ReceiptVersion, &at); err != nil {
				return err
			}
			result.Acknowledgements = append(result.Acknowledgements, &dispatcherpb.CommandNoticeAcknowledgement{Notice: n, AcknowledgedAt: timestamppb.New(at)})
		}
		return rows.Err()
	})
	return result, next, err
}
