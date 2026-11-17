package entry

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// The closed model column vocabulary indexes existing SQL identifiers only.
var keyColumns = [...]string{
	"actions_request_id", "actions_sequence", "github_app_id", "github_delivery_id",
	"github_repository_id", "github_event", "github_action", "github_pull_request_number",
	"github_mergeability_sequence", "github_snapshot_version", "github_observation_sequence",
	"message_id", "message_sender_namespace", "message_sender_name",
}

func keyFields(key model.EntryKey) ([]string, []any, error) {
	if key.Provider == "" || len(key.Fields) == 0 {
		return nil, nil, errors.New("entry key requires a provider and fields")
	}
	columns := make([]string, 0, len(key.Fields))
	values := make([]any, 0, len(key.Fields))
	seen := make(map[model.EntryColumn]bool, len(key.Fields))
	for _, field := range key.Fields {
		if int(field.Column) >= len(keyColumns) {
			return nil, nil, fmt.Errorf("unknown entry key column %d", field.Column)
		}
		if seen[field.Column] {
			return nil, nil, fmt.Errorf("duplicate entry key column %s", keyColumns[field.Column])
		}
		seen[field.Column] = true
		var value any
		switch scalar := field.Value.(type) {
		case model.EntryUUID:
			value = uuid.UUID(scalar)
		case model.EntryInteger:
			value = int64(scalar)
		case model.EntryText:
			value = string(scalar)
		case model.EntryNull:
			value = nil
		default:
			return nil, nil, fmt.Errorf("unsupported entry key value %T", field.Value)
		}
		columns = append(columns, keyColumns[field.Column])
		values = append(values, value)
	}
	return columns, values, nil
}

// Lookup uses the indexed relational natural key, including for expired payloads.
func Lookup(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID, key model.EntryKey) (int64, bool, error) {
	columns, values, err := keyFields(key)
	if err != nil {
		return 0, false, err
	}
	query := `SELECT cursor FROM entry WHERE inbox_id = $1 AND identity_provider = $2`
	args := []any{inboxID, string(key.Provider)}
	for i, column := range columns {
		if values[i] == nil {
			query += " AND " + column + " IS NULL"
		} else {
			args = append(args, values[i])
			query += fmt.Sprintf(" AND %s = $%d", column, len(args))
		}
	}
	var cursor int64
	err = tx.QueryRowContext(ctx, query, args...).Scan(&cursor)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	return cursor, true, nil
}

// Insert persists a prepared entry without interpreting its source or content.
func Insert(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID, cursor int64, input model.AppendInput, createdAt time.Time) error {
	columns, values, err := keyFields(input.Key)
	if err != nil {
		return err
	}
	var payload any
	if input.Payload.JSON != nil {
		payload = string(input.Payload.JSON)
	}
	args := []any{inboxID, cursor, payload, createdAt, string(input.Key.Provider)}
	args = append(args, values...)
	var identityName, identityUID *string
	if identity := input.Payload.SenderIdentity; identity != nil {
		identityName, identityUID = &identity.Name, &identity.UID
	}
	args = append(args, identityName, identityUID)
	args = append(args, storagecodec.EntryContentArgs(input.Payload.Content)...)
	placeholders := make([]string, len(args))
	for i := range placeholders {
		placeholders[i] = fmt.Sprintf("$%d", i+1)
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO entry (inbox_id, cursor, payload, created_at, identity_provider, `+
		strings.Join(columns, ", ")+`, message_sender_identity_name, message_sender_identity_uid, `+
		storagecodec.EntryContentReadColumns+`) VALUES (`+strings.Join(placeholders, ", ")+`)`, args...)
	return err
}
