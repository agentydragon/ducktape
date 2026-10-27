package dispatcher

import (
	"context"
	"database/sql"
	"encoding/base64"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/postgres/go"
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
// retained as an address to a Sandbox UID that no longer exists.
func (s *Store) EndSandbox(ctx context.Context, sandboxUID string) ([]TerminalEvent, error) {
	var events []TerminalEvent
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		// Match worker settlement's lock order: ownership row before delivery rows.
		var generation int64
		err := tx.QueryRowContext(ctx, `SELECT generation FROM sandbox_ownership WHERE sandbox_uid=$1 FOR UPDATE`, sandboxUID).Scan(&generation)
		if err != nil && err != sql.ErrNoRows {
			return err
		}
		positions := make([]int64, 0)
		for _, change := range []struct{ State, Predicate string }{
			{FailedState, "state='QUEUED_IN_DISPATCHER'"},
			{UnknownState, "state='DELIVERING_TO_RUNNER' OR (state='RUNNER_ADMITTED' AND operation='submit_input')"},
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
			rows, err := tx.QueryContext(ctx, `SELECT `+dispatchColumns+` FROM dispatch WHERE position=ANY($1) ORDER BY position`, pq.Array(positions))
			if err != nil {
				return err
			}
			for rows.Next() {
				row, err := scanDispatch(rows)
				if err != nil {
					rows.Close()
					return err
				}
				snapshot, err := DescribeDispatch(row)
				if err != nil {
					rows.Close()
					return err
				}
				events = append(events, TerminalEvent{uuid.NewString(), snapshot})
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
