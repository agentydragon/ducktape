package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/google/uuid"
)

func prStateFilterClause(alias string, filters []githubmodel.EventFilter, args *argList) string {
	selected := make([]string, 0, len(filters))
	for _, filter := range filters {
		if filter.Event != githubmodel.EventPullRequest && filter.Event != githubmodel.EventPullRequestReview {
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

func prStateFilters(source githubmodel.Source) []githubmodel.EventFilter {
	filters := source.Filters()
	selected := make([]githubmodel.EventFilter, 0, len(filters))
	for _, filter := range filters {
		if filter.Event == githubmodel.EventPullRequest || filter.Event == githubmodel.EventPullRequestReview {
			selected = append(selected, filter)
		}
	}
	return selected
}

func (s *Store) RecordGitHubPRStateTx(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID,
	subscriptionID uuid.UUID, subject githubstate.SubjectKey, fences []githubstate.AccessFence,
) (int, bool, error) {
	if subject.Kind != "pull_request" {
		return 0, false, nil
	}
	var creationProto sql.Null[[]byte]
	var sourceProvider sql.NullString
	var idempotencyKey string
	var after int64
	if err := tx.QueryRowContext(ctx, `SELECT creation_proto, source_provider, idempotency_key,
		github_pr_state_after_sequence FROM subscription WHERE id = $1`, subscriptionID).
		Scan(&creationProto, &sourceProvider, &idempotencyKey, &after); err != nil {
		return 0, false, err
	}
	body, err := sourceadapter.DecodeStoredSubscriptionCreation(subscriptionID, creationProto, sourceProvider, idempotencyKey)
	if err != nil {
		return 0, false, err
	}
	source, ok := body.Source.(sourceadapter.GitHubSource)
	if !ok {
		return 0, false, errors.New("PR state recording needs a GitHub subscription")
	}
	filters := prStateFilters(source.Source)
	if len(filters) == 0 || len(fences) == 0 {
		return 0, false, nil
	}
	count, more, err := s.recordGitHubObservationSnapshots(ctx, tx, inboxID, subscriptionID, fences, "github_pr_state_subscription_snapshot")
	if err != nil {
		return 0, false, err
	}
	if more {
		return count, true, nil
	}
	remaining := githubmodel.DeliveryPageSize - count
	var args argList
	accessible := make([]string, 0, len(fences))
	for _, fence := range fences {
		accessible = append(accessible, "(e.app_id = "+args.add(fence.Key.AppID)+" AND e.installation_id = "+
			args.add(fence.Key.InstallationID)+" AND e.repository_id = "+args.add(fence.Key.RepositoryID)+")")
	}
	selected := prStateFilterClause("e", filters, &args)
	repository, number, afterSequence := args.add(subject.RepositoryID), args.add(githubstate.PRNumber(subject)), args.add(after)
	limit := args.add(remaining + 1)
	query := `SELECT e.app_id, e.installation_id, e.repository_id, e.event, e.sequence, e.payload
		FROM github_pr_state_event e WHERE (` + strings.Join(accessible, " OR ") + `) AND ` + selected + `
		AND e.repository_id = ` + repository + ` AND e.pull_request_number = ` + number + ` AND e.sequence > ` + afterSequence + `
		AND NOT EXISTS (SELECT 1 FROM subscription_match m JOIN entry existing
			ON existing.inbox_id = ` + args.add(inboxID) + ` AND existing.cursor = m.cursor
			WHERE m.subscription_id = ` + args.add(subscriptionID) + `
				AND existing.identity_provider = 'github_observation'
				AND existing.github_app_id = e.app_id
				AND existing.github_repository_id = e.repository_id
				AND existing.github_event = e.event
				AND existing.github_observation_sequence = e.sequence)
		ORDER BY e.sequence LIMIT ` + limit
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return 0, false, err
	}
	events, err := readGitHubObservations(rows)
	if err != nil {
		return 0, false, err
	}
	more = len(events) > remaining
	if more {
		events = events[:remaining]
	}
	for _, value := range events {
		input, err := sourceadapter.AppendInputFor(value.identity, value.payload)
		if err != nil {
			return 0, false, err
		}
		if err := s.AppendTx(ctx, tx, inboxID, subscriptionID, input); err != nil {
			return 0, false, fmt.Errorf("append PR state event: %w", err)
		}
		count++
	}
	return count, more, nil
}
