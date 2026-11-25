package sessionstore

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/environmentcontext"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

const (
	environmentUpdateContentKind content.ContentKind = "sampler_environment_update"
	executionSnapshotContentKind content.ContentKind = "sampler_execution_snapshot"
)

// RecordObject is an immutable sampler value represented in one or more native
// records. Command and intent origins refer to their existing durable objects.
type RecordObject interface{ contentKind() content.ContentKind }

type EnvironmentUpdateObject struct {
	SessionID string                      `json:"session_id"`
	Before    *environmentcontext.Context `json:"before,omitempty"`
	After     environmentcontext.Context  `json:"after"`
}

func (EnvironmentUpdateObject) contentKind() content.ContentKind { return environmentUpdateContentKind }

type ExecutionSnapshotObject struct {
	ConversationID string `json:"conversation_id"`
	modelExecutionSnapshot
}

func (ExecutionSnapshotObject) contentKind() content.ContentKind { return executionSnapshotContentKind }

func putRecordObject(store content.ContentWriter, object RecordObject) (string, error) {
	payload, err := json.Marshal(object)
	if err != nil {
		return "", err
	}
	id, err := store.PutContent(object.contentKind(), payload)
	return string(id), err
}

// ReadRecordObject resolves a generated environment or execution object via
// an owned native context. transcript selects the original transcript view.
func (s *Store) ReadRecordObject(ctx context.Context, owner Owner, transcript bool, index int) (RecordObject, error) {
	var object RecordObject
	err := s.transaction(ctx, func(ctx context.Context, tx *sql.Tx) error {
		conversation, err := lockOwner(ctx, tx, owner)
		if err != nil {
			return err
		}
		state := conversation.Context()
		if transcript {
			state = conversation.Transcript
		}
		origin := state.OriginAt(index)
		if origin.ObjectID == "" {
			return protocol.ErrMissing
		}
		var wantKind content.ContentKind
		switch origin.Kind {
		case protocol.RecordOriginEnvironment:
			wantKind = environmentUpdateContentKind
		case protocol.RecordOriginExecutionSnapshot:
			wantKind = executionSnapshotContentKind
		default:
			return protocol.ErrMissing
		}
		row, err := newContentDB(ctx, tx).GetContent(content.ContentID(origin.ObjectID))
		if errors.Is(err, sql.ErrNoRows) {
			return fmt.Errorf("missing semantic object %q: %w", origin.ObjectID, protocol.ErrInvalid)
		}
		if err != nil {
			return err
		}
		if row.Kind != wantKind {
			return fmt.Errorf("semantic object %q has kind %q, want %q: %w", row.ID, row.Kind, wantKind, protocol.ErrInvalid)
		}
		switch origin.Kind {
		case protocol.RecordOriginEnvironment:
			session, err := sessionForConversation(ctx, tx, conversation.ID)
			if err != nil {
				return err
			}
			var value EnvironmentUpdateObject
			if json.Unmarshal(row.Payload, &value) != nil || value.SessionID != session.ID || value.After.CurrentDate == "" ||
				(origin.Projection == protocol.RecordProjectionEnvironmentDiff && value.Before == nil) {
				return fmt.Errorf("invalid environment object %q: %w", row.ID, protocol.ErrInvalid)
			}
			object = value
		case protocol.RecordOriginExecutionSnapshot:
			var value ExecutionSnapshotObject
			if json.Unmarshal(row.Payload, &value) != nil || value.ConversationID != conversation.ID ||
				value.Cells == nil || value.Processes == nil {
				return fmt.Errorf("invalid execution snapshot object %q: %w", row.ID, protocol.ErrInvalid)
			}
			for _, cell := range value.Cells {
				if cell.CellID == "" || !cell.Status.Valid() {
					return fmt.Errorf("invalid cell in execution snapshot object %q: %w", row.ID, protocol.ErrInvalid)
				}
			}
			for _, process := range value.Processes {
				if !process.Status.Valid() {
					return fmt.Errorf("invalid process in execution snapshot object %q: %w", row.ID, protocol.ErrInvalid)
				}
			}
			object = value
		}
		return nil
	})
	return object, err
}
