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

// recordCommitObservations appends current-state snapshots then transitions after the immutable creation
// boundary. Inbox copies and generated identities commit together, so replay after restart is durable.
func (c *Coordinator) recordCommitObservations(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID,
	subscriptionID uuid.UUID, subject githubstate.SubjectKey, fences []githubstate.AccessFence,
) (int, bool, error) {
	if subject.Kind != "pull_request" {
		return 0, false, nil
	}
	var creationProto []byte
	var sourceProvider, idempotencyKey string
	var after int64
	if err := tx.QueryRowContext(ctx, `SELECT creation_proto, source_provider, idempotency_key, github_commit_after_sequence
		FROM subscription WHERE id = $1`, subscriptionID).
		Scan(&creationProto, &sourceProvider, &idempotencyKey, &after); err != nil {
		return 0, false, err
	}
	body, err := sourceadapter.DecodeStoredSubscriptionCreation(subscriptionID, creationProto, sourceProvider, idempotencyKey)
	if err != nil {
		return 0, false, err
	}
	source, ok := body.Source.(sourceadapter.GitHubSource)
	if !ok {
		return 0, false, errors.New("commit observation recording needs a GitHub subscription")
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
	accessible := observationAccessClause(&args, fences, repositoryBeforeInstallation)
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
			AND NOT EXISTS (SELECT 1 FROM subscription_match m JOIN entry existing
				ON existing.inbox_id = ` + args.add(inboxID) + ` AND existing.cursor = m.cursor
				WHERE m.subscription_id = ` + args.add(subscriptionID) + `
					AND existing.identity_provider = 'github_observation'
					AND existing.github_app_id = e.app_id AND existing.github_repository_id = e.repository_id
					AND existing.github_event = e.event AND existing.github_observation_sequence = e.sequence)
		ORDER BY e.sequence LIMIT ` + limit
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return 0, false, err
	}
	return c.appendObservationPage(ctx, tx, inboxID, subscriptionID, rows, count, "append GitHub commit observation")
}
