package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/google/uuid"
)

// eventIdentityStorage is an entry's relational source identity. Writers also retain its legacy JSON form for
// compatibility with older readers during rollout; this release uses the indexed relational key for deduplication.
type eventIdentityStorage struct {
	provider                   string
	actionsRequestID           *uuid.UUID
	actionsSequence            *int64
	githubAppID                *int64
	githubDeliveryID           *uuid.UUID
	githubRepositoryID         *int64
	githubEvent                *string
	githubAction               *string
	githubPullRequestNumber    *int64
	githubMergeabilitySequence *int64
	githubSnapshotVersion      *int64
	messageID                  *uuid.UUID
	messageSenderNamespace     *string
	messageSenderName          *string
}

const eventIdentityReadColumns = `identity_provider, actions_request_id::text, actions_sequence,
	github_app_id, github_delivery_id::text, github_repository_id, github_event, github_action,
	github_pull_request_number, github_mergeability_sequence, github_snapshot_version,
	message_id::text, message_sender_namespace, message_sender_name`

// eventIdentityRead is one relational source key read from entry. The JSONB identity stays dual-written for old
// readers during rollout, but new readers and deduplication no longer depend on its serialization.
type eventIdentityRead struct {
	provider                   sql.NullString
	actionsRequestID           sql.NullString
	actionsSequence            sql.NullInt64
	githubAppID                sql.NullInt64
	githubDeliveryID           sql.NullString
	githubRepositoryID         sql.NullInt64
	githubEvent                sql.NullString
	githubAction               sql.NullString
	githubPullRequestNumber    sql.NullInt64
	githubMergeabilitySequence sql.NullInt64
	githubSnapshotVersion      sql.NullInt64
	messageID                  sql.NullString
	messageSenderNamespace     sql.NullString
	messageSenderName          sql.NullString
}

func (r *eventIdentityRead) destinations() []any {
	return []any{
		&r.provider, &r.actionsRequestID, &r.actionsSequence,
		&r.githubAppID, &r.githubDeliveryID, &r.githubRepositoryID, &r.githubEvent, &r.githubAction,
		&r.githubPullRequestNumber, &r.githubMergeabilitySequence, &r.githubSnapshotVersion,
		&r.messageID, &r.messageSenderNamespace, &r.messageSenderName,
	}
}

func (r eventIdentityRead) event() (EventIdentity, error) {
	if !r.provider.Valid {
		return nil, errors.New("entry has no relational event identity")
	}
	switch r.provider.String {
	case "actions":
		requestID, err := requiredIdentityUUID("actions_request_id", r.actionsRequestID)
		if err != nil {
			return nil, err
		}
		sequence, err := requiredIdentityInt64("actions_sequence", r.actionsSequence)
		if err != nil {
			return nil, err
		}
		return ActionsEvent{RequestID: requestID, Sequence: sequence}, nil
	case "github":
		appID, err := requiredIdentityInt64("github_app_id", r.githubAppID)
		if err != nil {
			return nil, err
		}
		deliveryID, err := requiredIdentityUUID("github_delivery_id", r.githubDeliveryID)
		if err != nil {
			return nil, err
		}
		repositoryID, err := requiredIdentityInt64("github_repository_id", r.githubRepositoryID)
		if err != nil {
			return nil, err
		}
		if !r.githubEvent.Valid {
			return nil, errors.New("entry has no github_event")
		}
		event := EventName(r.githubEvent.String)
		if !slices.Contains(knownEvents, event) {
			return nil, fmt.Errorf("unknown GitHub event %q", event)
		}
		var action *string
		if r.githubAction.Valid {
			action = &r.githubAction.String
		}
		return GitHubEvent{
			AppID: appID, DeliveryID: deliveryID, RepositoryID: repositoryID,
			Event: event, Action: action,
		}, nil
	case "github_mergeability":
		appID, err := requiredIdentityInt64("github_app_id", r.githubAppID)
		if err != nil {
			return nil, err
		}
		repositoryID, err := requiredIdentityInt64("github_repository_id", r.githubRepositoryID)
		if err != nil {
			return nil, err
		}
		pullRequestNumber, err := requiredIdentityInt64("github_pull_request_number", r.githubPullRequestNumber)
		if err != nil {
			return nil, err
		}
		switch {
		case r.githubMergeabilitySequence.Valid && !r.githubSnapshotVersion.Valid:
			return GitHubMergeabilityEvent{
				AppID: appID, RepositoryID: repositoryID, PullRequestNumber: pullRequestNumber,
				Sequence: r.githubMergeabilitySequence.Int64,
			}, nil
		case !r.githubMergeabilitySequence.Valid && r.githubSnapshotVersion.Valid:
			return GitHubMergeabilitySnapshotEvent{
				AppID: appID, RepositoryID: repositoryID, PullRequestNumber: pullRequestNumber,
				Version: r.githubSnapshotVersion.Int64,
			}, nil
		default:
			return nil, errors.New("entry must have exactly one mergeability sequence or snapshot version")
		}
	case "message":
		messageID, err := requiredIdentityUUID("message_id", r.messageID)
		if err != nil {
			return nil, err
		}
		if !r.messageSenderNamespace.Valid || !r.messageSenderName.Valid {
			return nil, errors.New("entry has an incomplete message sender")
		}
		return MessageEvent{
			MessageID: messageID,
			Sender:    ServiceAccount{Namespace: r.messageSenderNamespace.String, Name: r.messageSenderName.String},
		}, nil
	default:
		return nil, fmt.Errorf("unknown relational event identity provider %q", r.provider.String)
	}
}

func requiredIdentityUUID(field string, value sql.NullString) (uuid.UUID, error) {
	if !value.Valid {
		return uuid.Nil, fmt.Errorf("entry has no %s", field)
	}
	parsed, err := uuid.Parse(value.String)
	if err != nil {
		return uuid.Nil, fmt.Errorf("entry %s: %w", field, err)
	}
	return parsed, nil
}

func requiredIdentityInt64(field string, value sql.NullInt64) (int64, error) {
	if !value.Valid {
		return 0, fmt.Errorf("entry has no %s", field)
	}
	return value.Int64, nil
}

func insertEntry(
	ctx context.Context,
	tx *sql.Tx,
	inboxID uuid.UUID,
	cursor int64,
	event EventIdentity,
	payload json.RawMessage,
	createdAt time.Time,
) error {
	identity, err := json.Marshal(event)
	if err != nil {
		return err
	}
	columns, err := eventIdentityStorageFor(event)
	if err != nil {
		return err
	}
	typedPayload, err := marshalEntryContent(event, payload)
	if err != nil {
		return fmt.Errorf("encode %T entry payload: %w", event, err)
	}
	var typedPayloadParameter any
	if typedPayload != nil {
		typedPayloadParameter = typedPayload
	}
	args := append([]any{inboxID, cursor, string(identity), string(payload), createdAt}, columns.args()...)
	args = append(args, typedPayloadParameter)
	_, err = tx.ExecContext(ctx, `INSERT INTO entry (inbox_id, cursor, event, payload, created_at,
		identity_provider, actions_request_id, actions_sequence,
		github_app_id, github_delivery_id, github_repository_id, github_event, github_action,
		github_pull_request_number, github_mergeability_sequence, github_snapshot_version,
		message_id, message_sender_namespace, message_sender_name, typed_payload)
		VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20)`, args...)
	return err
}

func eventIdentityStorageFor(event EventIdentity) (eventIdentityStorage, error) {
	var columns eventIdentityStorage
	switch event := event.(type) {
	case ActionsEvent:
		columns.provider = "actions"
		columns.actionsRequestID = &event.RequestID
		columns.actionsSequence = &event.Sequence
	case GitHubEvent:
		columns.provider = "github"
		columns.githubAppID = &event.AppID
		columns.githubDeliveryID = &event.DeliveryID
		columns.githubRepositoryID = &event.RepositoryID
		name := string(event.Event)
		columns.githubEvent = &name
		columns.githubAction = event.Action
	case GitHubMergeabilityEvent:
		columns.provider = "github_mergeability"
		columns.githubAppID = &event.AppID
		columns.githubRepositoryID = &event.RepositoryID
		columns.githubPullRequestNumber = &event.PullRequestNumber
		columns.githubMergeabilitySequence = &event.Sequence
	case GitHubMergeabilitySnapshotEvent:
		columns.provider = "github_mergeability"
		columns.githubAppID = &event.AppID
		columns.githubRepositoryID = &event.RepositoryID
		columns.githubPullRequestNumber = &event.PullRequestNumber
		columns.githubSnapshotVersion = &event.Version
	case MessageEvent:
		columns.provider = "message"
		columns.messageID = &event.MessageID
		columns.messageSenderNamespace = &event.Sender.Namespace
		columns.messageSenderName = &event.Sender.Name
	default:
		return eventIdentityStorage{}, fmt.Errorf("unsupported event identity type %T", event)
	}
	return columns, nil
}

func (c eventIdentityStorage) args() []any {
	return []any{
		c.provider,
		c.actionsRequestID,
		c.actionsSequence,
		c.githubAppID,
		c.githubDeliveryID,
		c.githubRepositoryID,
		c.githubEvent,
		c.githubAction,
		c.githubPullRequestNumber,
		c.githubMergeabilitySequence,
		c.githubSnapshotVersion,
		c.messageID,
		c.messageSenderNamespace,
		c.messageSenderName,
	}
}
