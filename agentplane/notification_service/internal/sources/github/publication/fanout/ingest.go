package fanout

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/publication"
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
)

// Ingest stores a delivery and wakes every inbox following the App, and reports whether the delivery was
// new. A redelivery with the same content stores nothing and wakes nothing; with other content it fails with
// githubstate.ErrDeliveryReused.
//
// An installation or installation_repositories event invalidates what was learned of the installation: its
// generation, and those of its repositories' access rows and subjects, move on, so no result read earlier is
// recorded.
func (c *Coordinator) Ingest(ctx context.Context, ingest githubstate.Ingest) (bool, error) {
	var created bool
	err := c.runner.Run(ctx, func(tx transaction.Tx) error {
		// Source facts commit under the GitHub lock before generic inboxes are woken.
		newDelivery, err := githubstate.IngestTx(ctx, tx, ingest, now())
		if err != nil || !newDelivery {
			return err
		}
		created = true
		return (publication.Adapter{}).Publish(ctx, tx, ingest.AppID)
	})
	return created && err == nil, err
}

// Cleanup deletes bounded batches of raw receipts, generated events and obsolete commit facts
// older than cutoff.
// Expired transition rows are removed even if no inbox consumed them. Inbox entries and current pull-request
// snapshots have independent lifetimes. The GitHub lock serializes this with ingestion and recording.
func (c *Coordinator) Cleanup(ctx context.Context, cutoff time.Time) error {
	return c.runner.Run(ctx, func(tx transaction.Tx) error {
		if err := githubstate.Lock(ctx, tx); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `
			DELETE FROM github_delivery WHERE position IN (
				SELECT position FROM github_delivery WHERE received_at < $1
				ORDER BY received_at, position LIMIT 1000
			)`, cutoff); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `
			DELETE FROM github_mergeability_event WHERE sequence IN (
				SELECT sequence FROM github_mergeability_event WHERE created_at < $1
				ORDER BY created_at, sequence LIMIT 1000
			)`, cutoff)
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `DELETE FROM github_commit_event WHERE sequence IN (
			SELECT sequence FROM github_commit_event WHERE created_at < $1 ORDER BY created_at, sequence LIMIT 1000
		)`, cutoff)
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `DELETE FROM github_pr_state_event WHERE sequence IN (
			SELECT sequence FROM github_pr_state_event WHERE created_at < $1 ORDER BY created_at, sequence LIMIT 1000
		)`, cutoff)
		if err != nil {
			return err
		}
		// Old heads cease to be current snapshots once no live PR subscription follows them.
		// Keep the active head's deduplication facts even when its checks have not changed recently.
		_, err = tx.ExecContext(ctx, `DELETE FROM github_commit_current WHERE
			(app_id, installation_id, repository_id, head_sha, event, entity_key) IN (
				SELECT c.app_id, c.installation_id, c.repository_id, c.head_sha, c.event, c.entity_key
				FROM github_commit_current c WHERE (c.payload->>'observedAt')::timestamptz < $1
				AND NOT EXISTS (
					SELECT 1 FROM subscription s JOIN github_pull_request p
						ON p.repository_id = s.github_repository_id AND p.number::text = s.github_subject_key
					JOIN github_subject_revision r ON r.repository_id = p.repository_id
						AND r.kind = 'pull_request' AND r.subject_key = p.number::text
					WHERE s.github_app_id = c.app_id AND s.github_subject_kind = 'pull_request'
						AND NOT s.cancelled AND s.expires_at > now() AND p.head_sha = c.head_sha
						AND r.head_repository_id = c.repository_id AND r.sha = c.head_sha)
				ORDER BY c.app_id, c.installation_id, c.repository_id, c.head_sha, c.event, c.entity_key LIMIT 1000
			)`, cutoff)
		return err
	})
}
