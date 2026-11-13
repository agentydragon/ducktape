package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

func prStateFilterClause(alias string, filters []model.EventFilter, args *argList) string {
	selected := make([]string, 0, len(filters))
	for _, filter := range filters {
		if filter.Event != model.EventPullRequest && filter.Event != model.EventPullRequestReview {
			continue
		}
		part := alias + ".event = " + args.add(string(filter.Event))
		if filter.Actions != nil {
			actions := make([]string, len(filter.Actions))
			for i, action := range filter.Actions {
				actions[i] = args.add(action)
			}
			part += " AND " + alias + ".payload->>'action' IN (" + strings.Join(actions, ", ") + ")"
		}
		selected = append(selected, "("+part+")")
	}
	if len(selected) == 0 {
		return "FALSE"
	}
	return "(" + strings.Join(selected, " OR ") + ")"
}

func prStateFilters(source model.GitHubSource) []model.EventFilter {
	filters := source.Filters()
	selected := make([]model.EventFilter, 0, len(filters))
	for _, filter := range filters {
		if filter.Event == model.EventPullRequest || filter.Event == model.EventPullRequestReview {
			selected = append(selected, filter)
		}
	}
	return selected
}

func snapshotGitHubPRState(ctx context.Context, tx *sql.Tx, subscriptionID uuid.UUID, appID, installationID int64,
	subject githubstate.SubjectKey, filters []model.EventFilter,
) error {
	if subject.Kind != "pull_request" {
		return nil
	}
	args := argList{subscriptionID, appID, installationID, subject.RepositoryID, githubstate.PRNumber(subject)}
	selected := prStateFilterClause("c", filters, &args)
	_, err := tx.ExecContext(ctx, `INSERT INTO github_pr_state_subscription_snapshot
		(subscription_id, app_id, installation_id, repository_id, pull_request_number, event, sequence, payload)
		SELECT $1, c.app_id, c.installation_id, c.repository_id, c.pull_request_number, c.event, c.last_sequence, c.payload
		FROM github_pr_state_current c
		JOIN github_repository_access a ON a.app_id = c.app_id AND a.installation_id = c.installation_id
			AND a.repository_id = c.repository_id AND a.valid_until > now() AND a.error IS NULL
		JOIN github_installation i ON i.app_id = a.app_id AND i.installation_id = a.installation_id
			AND i.generation = a.validated_installation_generation
		WHERE c.app_id = $2 AND c.installation_id = $3 AND c.repository_id = $4 AND c.pull_request_number = $5
			AND c.last_sequence > 0 AND `+selected+`
		ON CONFLICT DO NOTHING`, args...)
	return err
}

func (s *Store) recordGitHubPRState(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID,
	recording GitHubRecording,
) (int, bool, error) {
	if recording.Subject.Kind != "pull_request" {
		return 0, false, nil
	}
	var creationProto sql.Null[[]byte]
	var sourceProvider sql.NullString
	var idempotencyKey string
	var after int64
	if err := tx.QueryRowContext(ctx, `SELECT creation_proto, source_provider, idempotency_key,
		github_pr_state_after_sequence FROM subscription WHERE id = $1`, recording.SubscriptionID).
		Scan(&creationProto, &sourceProvider, &idempotencyKey, &after); err != nil {
		return 0, false, err
	}
	body, err := storagecodec.DecodeStoredSubscriptionCreation(recording.SubscriptionID, creationProto, sourceProvider, idempotencyKey)
	if err != nil {
		return 0, false, err
	}
	source, ok := body.Source.(model.GitHubSource)
	if !ok {
		return 0, false, errors.New("PR state recording needs a GitHub subscription")
	}
	filters := prStateFilters(source)
	if len(filters) == 0 {
		return 0, false, nil
	}
	var snapshotArgs argList = []any{recording.SubscriptionID}
	access := make([]string, 0, len(recording.Fences))
	for _, fence := range recording.Fences {
		access = append(access, "(app_id = "+snapshotArgs.add(fence.Key.AppID)+" AND installation_id = "+
			snapshotArgs.add(fence.Key.InstallationID)+" AND repository_id = "+snapshotArgs.add(fence.Key.RepositoryID)+")")
	}
	if len(access) == 0 {
		return 0, false, nil
	}
	rows, err := tx.QueryContext(ctx, `SELECT app_id, installation_id, repository_id, event, sequence, payload
		FROM github_pr_state_subscription_snapshot WHERE subscription_id = $1 AND (`+strings.Join(access, " OR ")+`)
		ORDER BY sequence LIMIT `+snapshotArgs.add(GitHubDeliveryPageSize+1), snapshotArgs...)
	if err != nil {
		return 0, false, err
	}
	type item struct {
		app, installation, repository int64
		event                         model.EventName
		sequence                      int64
		payload                       json.RawMessage
	}
	var snapshots []item
	for rows.Next() {
		var value item
		if err := rows.Scan(&value.app, &value.installation, &value.repository, &value.event, &value.sequence, &value.payload); err != nil {
			rows.Close()
			return 0, false, err
		}
		snapshots = append(snapshots, value)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return 0, false, err
	}
	more := len(snapshots) > GitHubDeliveryPageSize
	if more {
		snapshots = snapshots[:GitHubDeliveryPageSize]
	}
	count := 0
	for _, value := range snapshots {
		identity := model.GitHubObservationEvent{AppID: value.app, RepositoryID: value.repository, Event: value.event, Sequence: value.sequence}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, value.payload); err != nil {
			return 0, false, fmt.Errorf("append PR state snapshot: %w", err)
		}
		count++
		if _, err := tx.ExecContext(ctx, `DELETE FROM github_pr_state_subscription_snapshot WHERE subscription_id = $1 AND sequence = $2`,
			recording.SubscriptionID, value.sequence); err != nil {
			return 0, false, err
		}
	}
	if more || count == GitHubDeliveryPageSize {
		return count, true, nil
	}
	remaining := GitHubDeliveryPageSize - count
	var args argList
	accessible := make([]string, 0, len(recording.Fences))
	for _, fence := range recording.Fences {
		accessible = append(accessible, "(e.app_id = "+args.add(fence.Key.AppID)+" AND e.installation_id = "+
			args.add(fence.Key.InstallationID)+" AND e.repository_id = "+args.add(fence.Key.RepositoryID)+")")
	}
	selected := prStateFilterClause("e", filters, &args)
	repository, number, afterSequence := args.add(recording.Subject.RepositoryID), args.add(githubstate.PRNumber(recording.Subject)), args.add(after)
	limit := args.add(remaining + 1)
	query := `SELECT e.app_id, e.installation_id, e.repository_id, e.event, e.sequence, e.payload
		FROM github_pr_state_event e WHERE (` + strings.Join(accessible, " OR ") + `) AND ` + selected + `
		AND e.repository_id = ` + repository + ` AND e.pull_request_number = ` + number + ` AND e.sequence > ` + afterSequence + `
		AND NOT EXISTS (SELECT 1 FROM subscription_match m JOIN entry existing
			ON existing.inbox_id = ` + args.add(inboxID) + ` AND existing.cursor = m.cursor
			WHERE m.subscription_id = ` + args.add(recording.SubscriptionID) + `
				AND existing.identity_provider = 'github_observation'
				AND existing.github_app_id = e.app_id
				AND existing.github_repository_id = e.repository_id
				AND existing.github_event = e.event
				AND existing.github_observation_sequence = e.sequence)
		ORDER BY e.sequence LIMIT ` + limit
	rows, err = tx.QueryContext(ctx, query, args...)
	if err != nil {
		return 0, false, err
	}
	var events []item
	for rows.Next() {
		var value item
		if err := rows.Scan(&value.app, &value.installation, &value.repository, &value.event, &value.sequence, &value.payload); err != nil {
			rows.Close()
			return 0, false, err
		}
		events = append(events, value)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return 0, false, err
	}
	more = len(events) > remaining
	if more {
		events = events[:remaining]
	}
	for _, value := range events {
		identity := model.GitHubObservationEvent{AppID: value.app, RepositoryID: value.repository, Event: value.event, Sequence: value.sequence}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, value.payload); err != nil {
			return 0, false, fmt.Errorf("append PR state event: %w", err)
		}
		count++
	}
	return count, more, nil
}
