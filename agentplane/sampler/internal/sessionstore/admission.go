package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	samplerpb "github.com/agentydragon/ducktape/agentplane/sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

var (
	ErrSessionBlocked        = fmt.Errorf("session is blocked: %w", protocol.ErrPrecondition)
	ErrInterruptTurnInactive = fmt.Errorf("interrupt target is not the active turn: %w", protocol.ErrPrecondition)
	ErrInterruptOwnerMissing = fmt.Errorf("interrupt target has no sampler owner: %w", protocol.ErrPrecondition)
)

// AdmissionState holds the projected session and conversation header read under its admission lock.
// LoadConversation and ProviderRoutes are only valid during the validation callback.
type AdmissionState struct {
	Session           *Session
	Conversation      *Conversation
	HasQueuedCommands bool
	LoadConversation  func() (*Conversation, error)
	ProviderRoutes    func() ([]ProviderRequest, error)
}

func (s *Store) Admit(ctx context.Context, id string, command *commandpb.Command, ownerKey string, validate func(AdmissionState) error) (*eventlogpb.EventEntry, error) {
	if command == nil || command.GetCommandId() == "" || ownerKey == "" {
		return nil, protocol.ErrInvalid
	}
	wire, err := proto.MarshalOptions{Deterministic: true}.Marshal(command)
	if err != nil {
		return nil, err
	}
	var admission *eventlogpb.EventEntry
	err = s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, session, err := lockSessionHeader(ctx, tx, id)
		if err != nil {
			return err
		}
		if session.OwnerKey != ownerKey {
			return ErrForbidden
		}
		var payload []byte
		var sequence uint64
		err = tx.QueryRowContext(ctx, `SELECT payload,admission FROM sampler_command WHERE session_id=$1 AND id=$2`, id, command.CommandId).Scan(&payload, &sequence)
		if err == nil {
			if !bytes.Equal(payload, wire) {
				return ErrConflict
			}
			admission, err = journalEntry(ctx, tx, id, sequence)
			if err != nil {
				return err
			}
			return notifyWork(ctx, tx)
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if session.Blocked {
			return ErrSessionBlocked
		}
		if validate != nil {
			// Validate against the state after earlier commands, under the admission lock.
			projected := *session
			hasQueuedCommands := false
			rows, err := tx.QueryContext(ctx, `SELECT payload FROM sampler_command
                WHERE session_id=$1 AND outcome=$2 ORDER BY admission`, id,
				int32(samplerpb.CommandOutcome_COMMAND_OUTCOME_ACCEPTED))
			if err != nil {
				return err
			}
			defer rows.Close()
			for rows.Next() {
				hasQueuedCommands = true
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
			conversationLoaded := false
			loadConversation := func() (*Conversation, error) {
				if !conversationLoaded {
					if err := loadConversationItems(ctx, tx, conversation); err != nil {
						return nil, err
					}
					conversationLoaded = true
				}
				return conversation, nil
			}
			if err := validate(AdmissionState{
				Session:           &projected,
				Conversation:      conversation,
				HasQueuedCommands: hasQueuedCommands,
				LoadConversation:  loadConversation,
				ProviderRoutes: func() ([]ProviderRequest, error) {
					return providerRoutes(ctx, tx, conversation.ID)
				},
			}); err != nil {
				return err
			}
		}
		if interrupt := command.GetInterruptTurn(); interrupt != nil {
			if session.ActiveTurn == nil || *session.ActiveTurn != interrupt.GetTurnId() {
				return ErrInterruptTurnInactive
			}
			if conversation.Owner == nil {
				return ErrInterruptOwnerMissing
			}
			if _, err := tx.ExecContext(ctx, `INSERT INTO sampler_interrupt(id,conversation_id,generation,target_turn,result)
				VALUES($1,$2,$3,$4,NULL)`, uuid.New(), conversation.ID, conversation.Generation, interrupt.GetTurnId()); err != nil {
				return err
			}
		}
		admission, err = s.appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{
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
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	var outcome int32
	var failure sql.NullString
	var sequence uint64
	if err := s.db.QueryRowContext(ctx, `SELECT admission,outcome,failure_reason FROM sampler_command WHERE session_id=$1 AND id=$2`, id, commandID).Scan(&sequence, &outcome, &failure); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, protocol.ErrMissing
		}
		return nil, err
	}
	entry, err := journalEntry(ctx, s.db, id, sequence)
	if err != nil {
		return nil, err
	}
	response := &samplerpb.GetCommandResponse{Admission: entry, Outcome: samplerpb.CommandOutcome(outcome)}
	if failure.Valid {
		response.FailureReason = failure.String
	}
	return response, nil
}

// Checkpoint is the last snapshot whose entries and metadata completed export to target.
func (s *Store) Checkpoint(ctx context.Context, id, target string) (*uint64, error) {
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
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
	ctx, cancel := context.WithTimeout(ctx, s.operationTimeout)
	defer cancel()
	_, err := s.db.ExecContext(ctx, `INSERT INTO sampler_export_checkpoint(session_id,destination,cursor) VALUES($1,$2,$3)
		ON CONFLICT(session_id,destination) DO UPDATE SET cursor=GREATEST(sampler_export_checkpoint.cursor,EXCLUDED.cursor)`, id, target, cursor)
	var violation *pq.Error
	if errors.As(err, &violation) && violation.Constraint == "sampler_export_checkpoint_session_id_fkey" && violation.Code == "23503" {
		return protocol.ErrMissing
	}
	return err
}
