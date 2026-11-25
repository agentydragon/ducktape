package fanout

import (
	"context"

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
	var after int64
	source, err := githubSourceOf(ctx, tx, subscriptionID, "github_pr_state_after_sequence", &after)
	if err != nil {
		return 0, false, err
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
	accessible := observationAccessClause(&args, fences)
	selected := githubfilters.PRStateFilterClause("e", filters, args.add)
	repository, number, afterSequence := args.add(subject.RepositoryID), args.add(githubstate.PRNumber(subject)), args.add(after)
	limit := args.add(githubmodel.DeliveryPageSize - count + 1)
	query := `SELECT e.app_id, e.installation_id, e.repository_id, e.event, e.sequence, e.payload
		FROM github_pr_state_event e WHERE (` + accessible + `) AND ` + selected + `
		AND e.repository_id = ` + repository + ` AND e.pull_request_number = ` + number + ` AND e.sequence > ` + afterSequence + `
		AND ` + notRecorded(&args, inboxID, subscriptionID) + `
		ORDER BY e.sequence LIMIT ` + limit
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return 0, false, err
	}
	return c.appendObservationPage(ctx, tx, inboxID, subscriptionID, rows, count, "append PR state event")
}
