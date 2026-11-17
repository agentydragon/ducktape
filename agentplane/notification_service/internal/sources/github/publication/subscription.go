package publication

import (
	"context"
	"database/sql"
	"strconv"
	"strings"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// BindSubscriptionTx runs after the GitHub lock and authorization grant have been established.
func BindSubscriptionTx(ctx context.Context, tx transaction.Tx, appID, installationID, repositoryID int64,
	kind, key string,
) (model.SubscriptionBinding, error) {
	var start, commitSequence, prStateSequence int64
	if err := tx.QueryRowContext(ctx, `SELECT coalesce(max(position), 0) FROM github_delivery`).Scan(&start); err != nil {
		return model.SubscriptionBinding{}, err
	}
	if err := tx.QueryRowContext(ctx, `SELECT coalesce(max(sequence), 0) FROM github_commit_event`).Scan(&commitSequence); err != nil {
		return model.SubscriptionBinding{}, err
	}
	if err := tx.QueryRowContext(ctx, `SELECT coalesce(max(sequence), 0) FROM github_pr_state_event`).Scan(&prStateSequence); err != nil {
		return model.SubscriptionBinding{}, err
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO github_subject (repository_id, kind, subject_key, generation) VALUES ($1, $2, $3, 0) ON CONFLICT DO NOTHING`,
		repositoryID, kind, key); err != nil {
		return model.SubscriptionBinding{}, err
	}
	return model.SubscriptionBinding{
		GitHubStartPosition:        sql.NullInt64{Int64: start, Valid: true},
		GitHubCommitAfterSequence:  sql.NullInt64{Int64: commitSequence, Valid: true},
		GitHubPRStateAfterSequence: sql.NullInt64{Int64: prStateSequence, Valid: true},
		GitHubApp:                  sql.NullInt64{Int64: appID, Valid: true},
		GitHubInstallation:         sql.NullInt64{Int64: installationID, Valid: true},
		GitHubRepository:           sql.NullInt64{Int64: repositoryID, Valid: true},
		GitHubSubjectKind:          sql.NullString{String: kind, Valid: true},
		GitHubSubjectKey:           sql.NullString{String: key, Valid: true},
	}, nil
}

func InitializeSubscriptionTx(ctx context.Context, tx transaction.Tx, id uuid.UUID, source githubmodel.Source,
	appID, installationID, repositoryID int64, kind, key string,
) error {
	if _, err := tx.ExecContext(ctx, `UPDATE subscription
  SET github_mergeability_after_sequence = (SELECT coalesce(max(sequence), 0) FROM github_mergeability_event)
  WHERE id = $1`, id); err != nil {
		return err
	}
	if kind != "pull_request" {
		return nil
	}
	if err := snapshotGitHubCommitObservations(ctx, tx, id, appID, repositoryID, kind, key, source); err != nil {
		return err
	}
	return snapshotGitHubPRState(ctx, tx, id, appID, installationID, repositoryID, kind, key, prStateFilters(source))
}

type argList []any

func (a *argList) add(value any) string {
	*a = append(*a, value)
	return "$" + strconv.Itoa(len(*a))
}

func commitObservationClause(alias string, selection githubmodel.CommitObservationSelection, args *argList) string {
	var selected []string
	if selection.CheckRuns {
		part := alias + ".event = " + args.add(string(githubmodel.EventCheckRun))
		if selection.CompletedCheckRunsOnly {
			part += " AND " + alias + ".payload->>'status' = " + args.add(string(githubmodel.CheckRunCompleted))
		}
		selected = append(selected, "("+part+")")
	}
	if selection.CommitStatuses {
		selected = append(selected, "("+alias+".event = "+args.add(string(githubmodel.EventStatus))+")")
	}
	if len(selected) == 0 {
		return "FALSE"
	}
	return "(" + strings.Join(selected, " OR ") + ")"
}

// snapshotGitHubCommitObservations stores the current native state for current PR-head revisions at the
// subscription's immutable creation boundary. New transitions have larger sequences and remain replayable.
func snapshotGitHubCommitObservations(ctx context.Context, tx transaction.Tx, subscriptionID uuid.UUID, appID int64,
	repositoryID int64, kind, key string, source githubmodel.Source,
) error {
	selection := source.CommitObservationSelection()
	args := argList{subscriptionID, appID, repositoryID, kind, key}
	selected := commitObservationClause("c", selection, &args)
	_, err := tx.ExecContext(ctx, `INSERT INTO github_commit_subscription_snapshot
		(subscription_id, app_id, installation_id, repository_id, event, sequence, payload)
		SELECT $1, c.app_id, c.installation_id, c.repository_id, c.event, c.last_sequence, c.payload
		FROM (
			SELECT DISTINCT ON (app_id, installation_id, repository_id, head_sha, event,
				CASE WHEN event = 'check_run' THEN COALESCE(payload->>'appId', payload->>'checkAppId') END,
				CASE WHEN event = 'check_run' THEN payload->>'name' END,
				CASE WHEN event = 'check_run' AND COALESCE(payload->>'appId', payload->>'checkAppId') IS NULL THEN entity_key END,
				CASE WHEN event = 'status' THEN entity_key END) *
			FROM github_commit_current
			ORDER BY app_id, installation_id, repository_id, head_sha, event,
				CASE WHEN event = 'check_run' THEN COALESCE(payload->>'appId', payload->>'checkAppId') END,
				CASE WHEN event = 'check_run' THEN payload->>'name' END,
				CASE WHEN event = 'check_run' AND COALESCE(payload->>'appId', payload->>'checkAppId') IS NULL THEN entity_key END,
				CASE WHEN event = 'status' THEN entity_key END,
				(payload->>'id')::bigint DESC
		) c
		JOIN github_pull_request p ON p.repository_id = $3 AND p.number::text = $5 AND p.head_sha = c.head_sha
		JOIN github_repository_access a ON a.app_id = c.app_id AND a.installation_id = c.installation_id
			AND a.repository_id = c.repository_id AND a.valid_until > now() AND a.error IS NULL
		JOIN github_installation i ON i.app_id = a.app_id AND i.installation_id = a.installation_id
			AND i.generation = a.validated_installation_generation
		WHERE c.app_id = $2 AND EXISTS (
			SELECT 1 FROM github_subject_revision r WHERE r.repository_id = $3 AND r.kind = $4 AND r.subject_key = $5
				AND r.head_repository_id = c.repository_id AND r.sha = p.head_sha)
			AND `+selected+`
		ON CONFLICT DO NOTHING`, args...)
	return err
}

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

func snapshotGitHubPRState(ctx context.Context, tx transaction.Tx, subscriptionID uuid.UUID, appID, installationID int64,
	repositoryID int64, kind, key string, filters []githubmodel.EventFilter,
) error {
	if kind != "pull_request" {
		return nil
	}
	number, err := strconv.ParseInt(key, 10, 64)
	if err != nil {
		return err
	}
	args := argList{subscriptionID, appID, installationID, repositoryID, number}
	selected := prStateFilterClause("c", filters, &args)
	_, err = tx.ExecContext(ctx, `INSERT INTO github_pr_state_subscription_snapshot
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
