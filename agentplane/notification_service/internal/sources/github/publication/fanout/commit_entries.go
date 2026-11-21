package fanout

import (
	"context"

	githubfilters "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/filters"
	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/google/uuid"
)

// recordCommitObservations appends current-state snapshots then transitions after the immutable creation
// boundary. Inbox copies and generated identities commit together, so replay after restart is durable.
func (c *Coordinator) recordCommitObservations(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID,
	subscriptionID uuid.UUID, subject githubstate.SubjectKey, fences []githubstate.AccessFence,
) (int, bool, error) {
	if subject.Kind != "pull_request" {
		return 0, false, nil
	}
	var after int64
	source, err := githubSourceOf(ctx, tx, subscriptionID, "github_commit_after_sequence", &after)
	if err != nil {
		return 0, false, err
	}
	selection := source.CommitObservationSelection()
	if !selection.CheckRuns && !selection.CommitStatuses {
		return 0, false, nil
	}
	count, more, err := c.recordObservationSnapshots(ctx, tx, inboxID, subscriptionID, fences, "github_commit_subscription_snapshot")
	if err != nil {
		return 0, false, err
	}
	if more {
		return count, true, nil
	}
	if len(fences) == 0 {
		return count, false, nil
	}
	var args argList
	accessible := observationAccessClause(&args, fences)
	selected := githubfilters.CommitObservationClause("e", selection, args.add)
	repository := args.add(subject.RepositoryID)
	kind := args.add(subject.Kind)
	key := args.add(subject.Key)
	sequence := args.add(after)
	limit := args.add(githubmodel.DeliveryPageSize - count + 1)
	query := `SELECT e.app_id, e.installation_id, e.repository_id, e.event, e.sequence, e.payload
		FROM github_commit_event e
		WHERE (` + accessible + `) AND ` + selected + `
			AND EXISTS (SELECT 1 FROM github_subject_revision r
				WHERE r.repository_id = ` + repository + ` AND r.kind = ` + kind + ` AND r.subject_key = ` + key + `
					AND r.head_repository_id = e.repository_id AND r.sha = e.head_sha)
			AND e.sequence > ` + sequence + `
			AND ` + notRecorded(&args, inboxID, subscriptionID) + `
		ORDER BY e.sequence LIMIT ` + limit
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return 0, false, err
	}
	return c.appendObservationPage(ctx, tx, inboxID, subscriptionID, rows, count, "append GitHub commit observation")
}
