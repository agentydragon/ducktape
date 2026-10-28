package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/google/uuid"
)

// eventIdentityLookup uses the indexed relational source key, including for expired payloads.
func eventIdentityLookup(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, event model.EventIdentity) (int64, bool, error) {
	switch event := event.(type) {
	case model.ActionsEvent:
		return eventIdentityCursor(ctx, tx, `SELECT cursor FROM entry
			WHERE inbox_id = $1 AND identity_provider = 'actions'
			AND actions_request_id = $2 AND actions_sequence = $3`, inboxID, event.RequestID, event.Sequence)
	case model.GitHubEvent:
		query := `SELECT cursor FROM entry WHERE inbox_id = $1 AND identity_provider = 'github'
			AND github_app_id = $2 AND github_delivery_id = $3 AND github_repository_id = $4 AND github_event = $5`
		args := []any{inboxID, event.AppID, event.DeliveryID, event.RepositoryID, string(event.Event)}
		if event.Action == nil {
			query += ` AND github_action IS NULL`
		} else {
			query += ` AND github_action = $6`
			args = append(args, *event.Action)
		}
		return eventIdentityCursor(ctx, tx, query, args...)
	case model.GitHubObservationEvent:
		return eventIdentityCursor(ctx, tx, `SELECT cursor FROM entry WHERE inbox_id = $1 AND identity_provider = 'github_observation'
			AND github_app_id = $2 AND github_repository_id = $3 AND github_event = $4
			AND github_observation_sequence = $5`, inboxID, event.AppID, event.RepositoryID,
			string(event.Event), event.Sequence)
	case model.GitHubMergeabilityEvent:
		return eventIdentityCursor(ctx, tx, `SELECT cursor FROM entry
			WHERE inbox_id = $1 AND identity_provider = 'github_mergeability'
			AND github_app_id = $2 AND github_repository_id = $3 AND github_pull_request_number = $4
			AND github_mergeability_sequence IS NOT NULL AND github_mergeability_sequence = $5`,
			inboxID, event.AppID, event.RepositoryID, event.PullRequestNumber, event.Sequence)
	case model.GitHubMergeabilitySnapshotEvent:
		return eventIdentityCursor(ctx, tx, `SELECT cursor FROM entry
			WHERE inbox_id = $1 AND identity_provider = 'github_mergeability'
			AND github_app_id = $2 AND github_repository_id = $3 AND github_pull_request_number = $4
			AND github_snapshot_version IS NOT NULL AND github_snapshot_version = $5`,
			inboxID, event.AppID, event.RepositoryID, event.PullRequestNumber, event.Version)
	case model.MessageEvent:
		return eventIdentityCursor(ctx, tx, `SELECT cursor FROM entry
			WHERE inbox_id = $1 AND identity_provider = 'message'
			AND message_id = $2 AND message_sender_namespace = $3 AND message_sender_name = $4`,
			inboxID, event.MessageID, event.Sender.Namespace, event.Sender.Name)
	default:
		return 0, false, fmt.Errorf("unsupported event identity type %T", event)
	}
}

func eventIdentityCursor(ctx context.Context, tx *sql.Tx, query string, args ...any) (int64, bool, error) {
	var cursor int64
	err := tx.QueryRowContext(ctx, query, args...).Scan(&cursor)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	return cursor, true, nil
}
