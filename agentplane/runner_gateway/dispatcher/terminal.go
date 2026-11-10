package dispatcher

import (
	"context"
	"database/sql"
	"encoding/base64"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	notificationpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/dispatcher_notificationpb"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

const (
	TerminalChannel        = "agentplane_dispatcher_terminal"
	terminalChunkBytes     = 4000
	terminalMaxParts       = 2048
	terminalMaxNotifyBytes = 7900
)

type TerminalEvent struct {
	ID       string
	Dispatch *dispatcherpb.Dispatch
	Owner    *ServiceAccountIdentity
	// OwnerIdentity is the Session's execution Identity, nil when it has none.
	OwnerIdentity *identitypb.Identity
}

func terminalPayloads(event TerminalEvent) ([]string, error) {
	serialized, err := wire(event.Dispatch)
	if err != nil {
		return nil, err
	}
	partCount := (len(serialized) + terminalChunkBytes - 1) / terminalChunkBytes
	if partCount == 0 {
		partCount = 1
	}
	if partCount > terminalMaxParts {
		return nil, fmt.Errorf("terminal snapshot needs %d parts", partCount)
	}
	result := make([]string, 0, partCount)
	for i := 0; i < partCount; i++ {
		start := i * terminalChunkBytes
		end := start + terminalChunkBytes
		if end > len(serialized) {
			end = len(serialized)
		}
		body, err := proto.Marshal(&notificationpb.TerminalNotificationPart{
			EventId: event.ID, Part: uint32(i), PartCount: uint32(partCount), Data: serialized[start:end],
			ExecutionAccountNamespace: executionAccountField(event.Owner, "namespace"),
			ExecutionAccountName:      executionAccountField(event.Owner, "name"),
			ExecutionAccountUid:       executionAccountField(event.Owner, "uid"),
			ExecutionIdentityName:     event.OwnerIdentity.GetName(),
			ExecutionIdentityUid:      event.OwnerIdentity.GetUid(),
		})
		if err != nil {
			return nil, err
		}
		payload := base64.StdEncoding.EncodeToString(body)
		if len(payload) > terminalMaxNotifyBytes {
			return nil, fmt.Errorf("terminal notify part exceeds %d bytes", terminalMaxNotifyBytes)
		}
		result = append(result, payload)
	}
	return result, nil
}

// EndSandbox makes terminal snapshots visible to active Watch streams in the
// same transaction that removes the deleted Sandbox work. The Session stays as
// a durable record with its exact placement, while the tombstone prevents later
// admission against stale inventory or a same-name replacement.
func (s *Store) EndSandbox(ctx context.Context, sandboxUID string) ([]TerminalEvent, error) {
	var events []TerminalEvent
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "sandbox:"+sandboxUID); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, "INSERT INTO deleted_sandbox (sandbox_uid) VALUES ($1) ON CONFLICT DO NOTHING", sandboxUID); err != nil {
			return err
		}
		// Match worker settlement's lock order: ownership row before delivery rows.
		var generation int64
		err := tx.QueryRowContext(ctx, `SELECT generation FROM sandbox_ownership WHERE sandbox_uid=$1 FOR UPDATE`, sandboxUID).Scan(&generation)
		if err != nil && err != sql.ErrNoRows {
			return err
		}
		positions := make([]int64, 0)
		for _, change := range []struct{ State, Predicate string }{
			{FailedState, "state IN ('QUEUED_IN_DISPATCHER','DELIVERING_TO_RUNNER') AND NOT delivery_attempted"},
			{UnknownState, "(state IN ('QUEUED_IN_DISPATCHER','DELIVERING_TO_RUNNER') AND delivery_attempted) OR (state='RUNNER_ADMITTED' AND operation='submit_input')"},
		} {
			rows, err := tx.QueryContext(ctx, `UPDATE dispatch SET state=$2,reason='sandbox_deleted',updated_at=now()
				WHERE sandbox_uid=$1 AND (`+change.Predicate+`) RETURNING position`, sandboxUID, change.State)
			if err != nil {
				return err
			}
			for rows.Next() {
				var position int64
				if err := rows.Scan(&position); err != nil {
					rows.Close()
					return err
				}
				positions = append(positions, position)
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return err
			}
		}
		if len(positions) > 0 {
			rows, err := tx.QueryContext(ctx, `SELECT `+qualifiedDispatchColumns+`,
				o.execution_account_namespace,o.execution_account_name,o.execution_account_uid,
				o.execution_identity_name,o.execution_identity_uid::text
				FROM dispatch d LEFT JOIN session o ON o.session_id=d.session_id
				AND o.sandbox_uid=d.sandbox_uid AND o.sandbox_name=d.sandbox_name
				WHERE d.position=ANY($1) ORDER BY d.position`, pq.Array(positions))
			if err != nil {
				return err
			}
			for rows.Next() {
				row, owner, ownerIdentity, err := scanDispatchWithOwner(rows)
				if err != nil {
					rows.Close()
					return err
				}
				snapshot, err := DescribeDispatch(row)
				if err != nil {
					rows.Close()
					return err
				}
				events = append(events, TerminalEvent{ID: uuid.NewString(), Dispatch: snapshot, Owner: owner, OwnerIdentity: ownerIdentity})
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return err
			}
		}
		for _, event := range events {
			payloads, err := terminalPayloads(event)
			if err != nil {
				return err
			}
			for _, payload := range payloads {
				if err := postgres.Notify(ctx, tx, TerminalChannel, payload); err != nil {
					return err
				}
			}
		}
		deleted, err := tx.ExecContext(ctx, "DELETE FROM dispatch WHERE sandbox_uid=$1", sandboxUID)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE session SET
			environment_deleted_at=COALESCE(environment_deleted_at, now()),
			state=CASE state WHEN 'WAITING' THEN 'FAILED' WHEN 'IN_PROGRESS' THEN 'UNKNOWN' ELSE state END,
			reason=CASE WHEN state IN ('WAITING','IN_PROGRESS') THEN 'sandbox_deleted' ELSE reason END,
			updated_at=CASE WHEN environment_deleted_at IS NULL THEN now() ELSE updated_at END
			WHERE sandbox_uid=$1 AND environment_deleted_at IS NULL`, sandboxUID); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, "DELETE FROM sandbox_ownership WHERE sandbox_uid=$1", sandboxUID); err != nil {
			return err
		}
		count, err := deleted.RowsAffected()
		if err != nil {
			return err
		}
		if count > 0 {
			return notify(ctx, tx)
		}
		return nil
	})
	return events, err
}

func executionAccountField(owner *ServiceAccountIdentity, field string) string {
	if owner == nil {
		return ""
	}
	switch field {
	case "namespace":
		return owner.GetAccount().GetNamespace()
	case "name":
		return owner.GetAccount().GetName()
	case "uid":
		return owner.GetUid()
	default:
		return ""
	}
}

func (s *Store) ListSandboxUIDs(ctx context.Context) (map[string]struct{}, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT sandbox_uid::text FROM dispatch
		UNION SELECT sandbox_uid::text FROM session WHERE environment_deleted_at IS NULL
		UNION SELECT sandbox_uid::text FROM sandbox_ownership`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]struct{}{}
	for rows.Next() {
		var uid string
		if err := rows.Scan(&uid); err != nil {
			return nil, err
		}
		result[uid] = struct{}{}
	}
	return result, rows.Err()
}
