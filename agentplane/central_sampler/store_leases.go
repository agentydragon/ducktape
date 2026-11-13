package centralsampler

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

// Work scans select on durable pending work only. The conversation's `stopped` flag stays set
// after a rejected turn until the next command is processed, so filtering on it hid queued input
// whose direct wake reached a retiring driver or another replica.
const sessionWork = `(ss.setup_state=$2
		OR EXISTS(SELECT 1 FROM sampler_command cmd WHERE cmd.session_id=ss.id AND cmd.outcome=$3)
		OR EXISTS(SELECT 1 FROM sampler_cell cell WHERE cell.conversation_id=c.id AND cell.status='live')
		OR EXISTS(SELECT 1 FROM sampler_process proc WHERE proc.conversation_id=c.id AND proc.status NOT IN ('exited','exited_unread','abandoned'))
		OR EXISTS(SELECT 1 FROM sampler_intent intent WHERE intent.conversation_id=c.id AND intent.result IS NULL))`

// PendingSessionIDs pages in ID order, after the given ID, through Sessions whose export to target is behind.
func (s *Store) PendingSessionIDs(ctx context.Context, target, after string, limit int) ([]string, error) {
	if limit <= 0 || limit > 4096 {
		return nil, protocol.ErrInvalid
	}
	rows, err := s.db.QueryContext(ctx, `SELECT ss.id FROM sampler_session ss
		LEFT JOIN sampler_export_checkpoint cp ON cp.session_id=ss.id AND cp.destination=$1
		WHERE ss.id>$2 AND (cp.cursor IS NULL OR cp.cursor<ss.cursor)
		ORDER BY ss.id LIMIT $3`, target, after, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// RecoverableSessions lists unowned Sessions with pending work. A blocked Session is included
// only when its recorded JavaScript host is not instanceID: the owner that claims it settles the
// lost work in ReconcileCodeHostLoss and unblocks it, so a wedge never outlives the host that
// caused it, while a block recorded on the host still serving the Session waits for its operator.
func (s *Store) RecoverableSessions(ctx context.Context, exclude []string, limit int, instanceID string) ([]string, error) {
	if limit <= 0 {
		return nil, protocol.ErrInvalid
	}
	if _, err := uuid.Parse(instanceID); err != nil {
		return nil, protocol.ErrInvalid
	}
	if exclude == nil {
		exclude = []string{}
	}
	rows, err := s.db.QueryContext(ctx, `SELECT ss.id FROM sampler_session ss JOIN sampler_conversation c ON c.id=ss.conversation_id
		WHERE (ss.blocked=FALSE OR c.code_host_instance IS NOT NULL AND c.code_host_instance<>$5::uuid)
		AND (c.owner IS NULL OR c.lease_until IS NULL OR c.lease_until<=clock_timestamp())
		AND NOT (ss.id=ANY($1::varchar[])) AND `+sessionWork+`
		ORDER BY ss.id LIMIT $4`, pq.Array(exclude), int32(sessionpb.SetupState_SETUP_STATE_RUNNING),
		int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED), limit, instanceID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// OwnedWork returns pending work for the leases held by this runtime, including idle residents.
func (s *Store) OwnedWork(ctx context.Context, tokens []string) ([]string, error) {
	if len(tokens) == 0 {
		return nil, nil
	}
	rows, err := s.db.QueryContext(ctx, `SELECT ss.id FROM sampler_session ss JOIN sampler_conversation c ON c.id=ss.conversation_id
		WHERE ss.blocked=FALSE AND c.owner=ANY($1::uuid[])
		AND c.lease_until>clock_timestamp() AND `+sessionWork+` ORDER BY ss.id`, pq.Array(tokens),
		int32(sessionpb.SetupState_SETUP_STATE_RUNNING), int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

func (s *Store) Claim(ctx context.Context, conversationID string, duration time.Duration) (Owner, error) {
	if duration <= 0 {
		return Owner{}, protocol.ErrInvalid
	}
	var owner Owner
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		held, err := ownership.Acquire(ctx, tx, []any{conversationID}, duration)
		if errors.As(err, new(*lease.HeldError)) {
			return &LostOwnershipError{Reason: "conversation already has a live sampler"}
		}
		if errors.Is(err, sql.ErrNoRows) {
			return protocol.ErrMissing
		}
		if err != nil {
			return err
		}
		owner = Owner{ConversationID: conversationID, Generation: int(held.Generation), Token: held.Holder.String()}
		return nil
	})
	return owner, err
}

func (s *Store) Renew(ctx context.Context, owner Owner, duration time.Duration) error {
	if duration <= 0 {
		return protocol.ErrInvalid
	}
	held, err := owner.lease()
	if err != nil {
		return err
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		_, err := ownership.Renew(ctx, tx, held, duration)
		if errors.Is(err, lease.ErrLost) {
			return &LostOwnershipError{}
		}
		return err
	})
}

func (s *Store) Release(ctx context.Context, owner Owner) error {
	held, err := owner.lease()
	if err != nil {
		return err
	}
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if err := fenceOwner(ctx, tx, owner); err != nil {
			return err
		}
		if err := ownership.Release(ctx, tx, held); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `UPDATE sampler_interrupt SET result=$3 WHERE conversation_id=$1 AND generation=$2 AND result IS NULL`,
			owner.ConversationID, owner.Generation, `{"status":"not_applied","reason":"owner completed before observing request"}`)
		if err != nil {
			return err
		}
		return notifyWork(ctx, tx)
	})
}
