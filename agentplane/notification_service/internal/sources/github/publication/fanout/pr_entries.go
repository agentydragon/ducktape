package fanout

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	githubfilters "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/filters"
	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/google/uuid"
)

func (c *Coordinator) recordPRState(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID,
	subscriptionID uuid.UUID, subject githubstate.SubjectKey, fences []githubstate.AccessFence,
) (int, bool, error) {
	if subject.Kind != "pull_request" {
		return 0, false, nil
	}
	var creationProto []byte
	var sourceProvider, idempotencyKey string
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
	filters := githubfilters.PRStateFilters(source.Source)
	if len(filters) == 0 || len(fences) == 0 {
		return 0, false, nil
	}
	count, more, err := c.recordObservationSnapshots(ctx, tx, inboxID, subscriptionID, fences, "github_pr_state_subscription_snapshot")
	if err != nil {
		return 0, false, err
	}
	if more {
		return count, true, nil
	}
	var args argList
	accessible := observationAccessClause(&args, fences, installationBeforeRepository)
	selected := githubfilters.PRStateFilterClause("e", filters, args.add)
	repository, number, afterSequence := args.add(subject.RepositoryID), args.add(githubstate.PRNumber(subject)), args.add(after)
	limit := args.add(githubmodel.DeliveryPageSize - count + 1)
	query := `SELECT e.app_id, e.installation_id, e.repository_id, e.event, e.sequence, e.payload
		FROM github_pr_state_event e WHERE (` + accessible + `) AND ` + selected + `
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
	return c.appendObservationPage(ctx, tx, inboxID, subscriptionID, rows, count, "append PR state event")
}
