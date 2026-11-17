package storagecodec

import (
	"database/sql"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// EntryIdentityReadColumns lists the relational event identity columns in scan order.
const EntryIdentityReadColumns = `identity_provider, actions_request_id::text, actions_sequence,
	github_app_id, github_delivery_id::text, github_repository_id, github_event, github_action,
	github_pull_request_number, github_mergeability_sequence, github_snapshot_version, github_observation_sequence,
	message_id::text, message_sender_namespace, message_sender_name, message_sender_identity_name, message_sender_identity_uid`

// EntryIdentityStorage is the nullable physical row shape of an existing relational source key.
type EntryIdentityStorage struct {
	Provider                   sql.NullString
	ActionsRequestID           sql.NullString
	ActionsSequence            sql.NullInt64
	GitHubAppID                sql.NullInt64
	GitHubDeliveryID           sql.NullString
	GitHubRepositoryID         sql.NullInt64
	GitHubEvent                sql.NullString
	GitHubAction               sql.NullString
	GitHubPullRequestNumber    sql.NullInt64
	GitHubMergeabilitySequence sql.NullInt64
	GitHubSnapshotVersion      sql.NullInt64
	GitHubObservationSequence  sql.NullInt64
	MessageID                  sql.NullString
	MessageSenderNamespace     sql.NullString
	MessageSenderName          sql.NullString
	MessageSenderIdentityName  sql.NullString
	MessageSenderIdentityUID   sql.NullString
}

// EntryIdentityDestinations returns scan targets in EntryIdentityReadColumns order.
func EntryIdentityDestinations(r *EntryIdentityStorage) []any {
	return []any{
		&r.Provider, &r.ActionsRequestID, &r.ActionsSequence,
		&r.GitHubAppID, &r.GitHubDeliveryID, &r.GitHubRepositoryID, &r.GitHubEvent, &r.GitHubAction,
		&r.GitHubPullRequestNumber, &r.GitHubMergeabilitySequence, &r.GitHubSnapshotVersion, &r.GitHubObservationSequence,
		&r.MessageID, &r.MessageSenderNamespace, &r.MessageSenderName, &r.MessageSenderIdentityName, &r.MessageSenderIdentityUID,
	}
}

// ActionsIdentityFromStorage reconstructs a common Action identity.
func ActionsIdentityFromStorage(r EntryIdentityStorage) (model.ActionsEvent, error) {
	requestID, err := RequiredIdentityUUID("actions_request_id", r.ActionsRequestID)
	if err != nil {
		return model.ActionsEvent{}, err
	}
	sequence, err := RequiredIdentityInt64("actions_sequence", r.ActionsSequence)
	if err != nil {
		return model.ActionsEvent{}, err
	}
	return model.ActionsEvent{RequestID: requestID, Sequence: sequence}, nil
}

// MessageIdentityFromStorage reconstructs the natural key and durable sender metadata.
func MessageIdentityFromStorage(r EntryIdentityStorage) (model.MessageEvent, error) {
	messageID, err := RequiredIdentityUUID("message_id", r.MessageID)
	if err != nil {
		return model.MessageEvent{}, err
	}
	if !r.MessageSenderNamespace.Valid || !r.MessageSenderName.Valid {
		return model.MessageEvent{}, errors.New("entry has an incomplete message sender")
	}
	event := model.MessageEvent{
		MessageID: messageID,
		Sender:    model.ServiceAccount{Namespace: r.MessageSenderNamespace.String, Name: r.MessageSenderName.String},
	}
	if r.MessageSenderIdentityName.Valid != r.MessageSenderIdentityUID.Valid {
		return model.MessageEvent{}, errors.New("entry has an incomplete message sender Identity")
	}
	if r.MessageSenderIdentityName.Valid {
		event.SenderIdentity = &model.Identity{Name: r.MessageSenderIdentityName.String, UID: r.MessageSenderIdentityUID.String}
	}
	return event, nil
}

func RequiredIdentityUUID(field string, value sql.NullString) (uuid.UUID, error) {
	if !value.Valid {
		return uuid.Nil, fmt.Errorf("entry has no %s", field)
	}
	parsed, err := uuid.Parse(value.String)
	if err != nil {
		return uuid.Nil, fmt.Errorf("entry %s: %w", field, err)
	}
	return parsed, nil
}

func RequiredIdentityInt64(field string, value sql.NullInt64) (int64, error) {
	if !value.Valid {
		return 0, fmt.Errorf("entry has no %s", field)
	}
	return value.Int64, nil
}
