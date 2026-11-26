package fanout

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/facts"
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

// BackfillFacts upgrades retained rows written by an older ingress version before a worker matches
// them. The receipt identity and raw body remain unchanged; the write is serialized with ingress.
func (c *Coordinator) BackfillFacts(ctx context.Context) error {
	for {
		projected := 0
		err := c.runner.Run(ctx, func(tx transaction.Tx) error {
			if err := githubstate.Lock(ctx, tx); err != nil {
				return err
			}
			rows, err := tx.QueryContext(ctx, `
				SELECT position, event, payload FROM github_delivery
				WHERE fact IS NULL
				ORDER BY position LIMIT 500
				FOR UPDATE SKIP LOCKED`)
			if err != nil {
				return err
			}
			type legacyDelivery struct {
				position int64
				event    string
				payload  []byte
			}
			var batch []legacyDelivery
			for rows.Next() {
				var item legacyDelivery
				if err := rows.Scan(&item.position, &item.event, &item.payload); err != nil {
					rows.Close()
					return err
				}
				batch = append(batch, item)
			}
			if err := errors.Join(rows.Err(), rows.Close()); err != nil {
				return err
			}
			for _, item := range batch {
				if len(item.payload) == 0 || !json.Valid(item.payload) {
					return errors.New("legacy GitHub delivery has no valid body for fact backfill")
				}
				fact, err := facts.Project(item.event, item.payload)
				if err != nil {
					return err
				}
				if _, err := tx.ExecContext(ctx,
					`UPDATE github_delivery SET fact = $2::jsonb WHERE position = $1 AND fact IS NULL`,
					item.position, string(fact)); err != nil {
					return err
				}
				projected++
			}
			return nil
		})
		if err != nil {
			return err
		}
		if projected < 500 {
			return nil
		}
	}
}

// CleanupPayloads removes only the optional webhook body from bounded batches. Every receipt must have a
// durable fact first, so old writers can overlap this cleanup without losing matchable notification content.
func (c *Coordinator) CleanupPayloads(ctx context.Context, cutoff time.Time) error {
	if err := c.BackfillFacts(ctx); err != nil {
		return err
	}
	return c.runner.Run(ctx, func(tx transaction.Tx) error {
		if err := githubstate.Lock(ctx, tx); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, `
			WITH expired AS (
				SELECT position FROM github_delivery
				WHERE received_at < $1 AND payload IS NOT NULL AND fact IS NOT NULL
				ORDER BY received_at, position LIMIT 1000
				FOR UPDATE SKIP LOCKED
			)
			UPDATE github_delivery d SET payload = NULL FROM expired e WHERE d.position = e.position`, cutoff)
		return err
	})
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
