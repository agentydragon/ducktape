package centralsampler

import (
	"bytes"
	"context"
	"database/sql"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func (s *Store) Admit(ctx context.Context, id string, command *commandpb.Command, ownerUID string, validate func(*Session) error) (*eventlogpb.EventEntry, error) {
	if command == nil || command.GetCommandId() == "" || ownerUID == "" {
		return nil, protocol.ErrInvalid
	}
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(command)
	if err != nil {
		return nil, err
	}
	var admission *eventlogpb.EventEntry
	err = s.transaction(ctx, func(tx *sql.Tx) error {
		conversation, session, err := lockSession(ctx, tx, id)
		if err != nil {
			return err
		}
		if session.AccountUID != ownerUID {
			return ErrForbidden
		}
		var payload []byte
		var sequence uint64
		err = tx.QueryRowContext(ctx, `SELECT payload,admission FROM sampler_command WHERE session_id=$1 AND id=$2`, id, command.CommandId).Scan(&payload, &sequence)
		if err == nil {
			if !bytes.Equal(payload, wire) {
				return ErrConflict
			}
			var entryBytes []byte
			if err := tx.QueryRowContext(ctx, `SELECT entry FROM sampler_event WHERE session_id=$1 AND sequence=$2`, id, sequence).Scan(&entryBytes); err != nil {
				return err
			}
			admission = &eventlogpb.EventEntry{}
			if err := decodeStoredEvent(entryBytes, admission); err != nil {
				return err
			}
			return notifyWork(ctx, tx)
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if session.Blocked {
			return protocol.ErrPrecondition
		}
		if validate != nil {
			// Validate against the state after earlier commands, under the admission lock.
			projected := *session
			rows, err := tx.QueryContext(ctx, `SELECT payload FROM sampler_command
                WHERE session_id=$1 AND outcome=$2 ORDER BY admission`, id,
				int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
			if err != nil {
				return err
			}
			defer rows.Close()
			for rows.Next() {
				var wire []byte
				if err := rows.Scan(&wire); err != nil {
					return err
				}
				queued := &commandpb.Command{}
				if err := proto.Unmarshal(wire, queued); err != nil {
					return err
				}
				if model := queued.GetChangeModel(); model != nil {
					projected.Model = model.GetModel()
				}
				if effort := queued.GetChangeReasoningEffort(); effort != nil {
					projected.Effort = effort.GetEffort()
				}
			}
			if err := rows.Err(); err != nil {
				return err
			}
			rows.Close()
			if err := validate(&projected); err != nil {
				return err
			}
		}
		if interrupt := command.GetInterruptTurn(); interrupt != nil {
			if session.ActiveTurn == nil || *session.ActiveTurn != interrupt.GetTurnId() || conversation.Owner == nil {
				return protocol.ErrPrecondition
			}
			if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_interrupt(id,conversation_id,generation,target_turn,result)
				VALUES($1,$2,$3,$4,NULL)`, uuid.New(), conversation.ID, conversation.Generation, interrupt.GetTurnId()); err != nil {
				return err
			}
		}
		admission, err = appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{
			CommandAdmitted: &eventpb.CommandAdmitted{Command: command},
		}})
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `INSERT INTO sampler_command(session_id,id,payload,admission,outcome,failure_reason)
			VALUES($1,$2,$3,$4,$5,NULL)`, id, command.CommandId, wire, admission.Cursor,
			int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
		if err != nil {
			return err
		}
		return notifyWork(ctx, tx)
	})
	return admission, err
}

func (s *Store) GetCommand(ctx context.Context, id, commandID string) (*samplerpb.GetCommandResponse, error) {
	var wire []byte
	var outcome int32
	var failure sql.NullString
	var sequence uint64
	if err := s.db.QueryRowContext(ctx, `SELECT admission,outcome,failure_reason FROM sampler_command WHERE session_id=$1 AND id=$2`, id, commandID).Scan(&sequence, &outcome, &failure); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, protocol.ErrMissing
		}
		return nil, err
	}
	if err := s.db.QueryRowContext(ctx, `SELECT entry FROM sampler_event WHERE session_id=$1 AND sequence=$2`, id, sequence).Scan(&wire); err != nil {
		return nil, err
	}
	entry := &eventlogpb.EventEntry{}
	if err := decodeStoredEvent(wire, entry); err != nil {
		return nil, err
	}
	response := &samplerpb.GetCommandResponse{Admission: entry, Outcome: samplerpb.CommandOutcome(outcome)}
	if failure.Valid {
		response.FailureReason = failure.String
	}
	return response, nil
}

func (s *Store) Events(ctx context.Context, id string, after uint64, limit int) ([]*eventlogpb.EventEntry, error) {
	if limit <= 0 || limit > 4096 {
		return nil, protocol.ErrInvalid
	}
	rows, err := s.db.QueryContext(ctx, `SELECT entry FROM sampler_event WHERE session_id=$1 AND sequence>$2 ORDER BY sequence LIMIT $3`, id, after, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var entries []*eventlogpb.EventEntry
	for rows.Next() {
		var wire []byte
		if err := rows.Scan(&wire); err != nil {
			return nil, err
		}
		entry := &eventlogpb.EventEntry{}
		if err := decodeStoredEvent(wire, entry); err != nil {
			return nil, err
		}
		entries = append(entries, entry)
	}
	return entries, rows.Err()
}

// Checkpoint is the last snapshot whose entries and metadata completed export to target.
func (s *Store) Checkpoint(ctx context.Context, id, target string) (*uint64, error) {
	var cursor int64
	err := s.db.QueryRowContext(ctx, `SELECT cursor FROM sampler_export_checkpoint WHERE session_id=$1 AND destination=$2`, id, target).Scan(&cursor)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	value := uint64(cursor)
	return &value, nil
}

func (s *Store) CommitCheckpoint(ctx context.Context, id, target string, cursor uint64) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
		if _, _, err := lockSession(ctx, tx, id); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `INSERT INTO sampler_export_checkpoint(session_id,destination,cursor) VALUES($1,$2,$3)
			ON CONFLICT(session_id,destination) DO UPDATE SET cursor=GREATEST(sampler_export_checkpoint.cursor,EXCLUDED.cursor)`, id, target, cursor)
		return err
	})
}
