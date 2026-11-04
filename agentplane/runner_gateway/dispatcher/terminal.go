package dispatcher

import (
	"context"
	"database/sql"
	"encoding/base64"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	notificationpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/dispatcher_notificationpb"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/google/uuid"
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
// same transaction that removes the deleted Sandbox rows. No queue row can be
// retained as an address to a Sandbox UID that no longer exists. The tombstone
// survives queue cleanup and prevents later admission against stale inventory.
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
		for _, change := range []struct{ State, Predicate string }{
			{FailedState, "state IN ('QUEUED_IN_DISPATCHER','DELIVERING_TO_RUNNER') AND NOT delivery_attempted"},
			{UnknownState, "(state IN ('QUEUED_IN_DISPATCHER','DELIVERING_TO_RUNNER') AND delivery_attempted) OR (state='RUNNER_ADMITTED' AND operation='submit_input')"},
		} {
			if _, err := tx.ExecContext(ctx, `UPDATE dispatch SET state=$2,reason='sandbox_deleted',updated_at=now()
				WHERE sandbox_uid=$1 AND (`+change.Predicate+`)`, sandboxUID, change.State); err != nil {
				return err
			}
		}
		// Snapshot every removed receipt, including outcomes settled before this
		// transaction whose earlier invalidation a connected watcher has not read.
		rows, err := tx.QueryContext(ctx, `SELECT `+qualifiedDispatchColumns+`,
			o.execution_account_namespace,o.execution_account_name,o.execution_account_uid
			FROM dispatch d LEFT JOIN session_opening o ON o.session_id=d.session_id
			AND o.sandbox_uid=d.sandbox_uid AND o.sandbox_name=d.sandbox_name
			WHERE d.sandbox_uid=$1 ORDER BY d.position`, sandboxUID)
		if err != nil {
			return err
		}
		for rows.Next() {
			row, owner, err := scanDispatchWithOwner(rows)
			if err != nil {
				rows.Close()
				return err
			}
			snapshot, err := DescribeDispatch(row)
			if err != nil {
				rows.Close()
				return err
			}
			events = append(events, TerminalEvent{ID: uuid.NewString(), Dispatch: snapshot, Owner: owner})
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
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
		if _, err := tx.ExecContext(ctx, "DELETE FROM session_opening WHERE sandbox_uid=$1", sandboxUID); err != nil {
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
		UNION SELECT sandbox_uid::text FROM session_opening
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
