package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/google/uuid"
)

// eventIdentityStorage is the relational copy of an entry's source identity. The legacy JSON identity remains
// the deduplication key until the explicit online backfill and index build have completed.
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
	_, err = tx.ExecContext(ctx, `INSERT INTO entry (inbox_id, cursor, event, payload, created_at,
		identity_provider, actions_request_id, actions_sequence,
		github_app_id, github_delivery_id, github_repository_id, github_event, github_action,
		github_pull_request_number, github_mergeability_sequence, github_snapshot_version,
		message_id, message_sender_namespace, message_sender_name)
		VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)`,
		append([]any{inboxID, cursor, string(identity), string(payload), createdAt}, columns.args()...)...)
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
