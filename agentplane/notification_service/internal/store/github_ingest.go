package store

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"time"

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/google/uuid"
)

// IngestGitHub stores a delivery and wakes every inbox following the App, and reports whether the delivery was
// new. A redelivery with the same content stores nothing and wakes nothing; with other content it fails with
// githubstate.ErrDeliveryReused.
//
// An installation or installation_repositories event invalidates what was learned of the installation: its
// generation, and those of its repositories' access rows and subjects, move on, so no result read earlier is
// recorded.
func (s *Store) IngestGitHub(ctx context.Context, ingest githubstate.Ingest) (bool, error) {
	var created bool
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		// Source facts commit under the GitHub lock before generic inboxes are woken.
		newDelivery, err := githubstate.IngestTx(ctx, tx, ingest, now())
		if err != nil || !newDelivery {
			return err
		}
		if err := wakeGitHubInboxes(ctx, tx, ingest.AppID); err != nil {
			return err
		}
		created = true
		return notify(ctx, tx)
	})
	return created && err == nil, err
}

// wakeGitHubInboxes makes every inbox with a live subscription to the App due now, and its GitHub subscriptions
// with it. This is conservative: a pull request whose head lives in a fork is woken too.
func wakeGitHubInboxes(ctx context.Context, tx *sql.Tx, appID int64) error {
	// Lock in ID order, after the GitHub lock.
	rows, err := tx.QueryContext(ctx, `
		SELECT id FROM inbox
		WHERE NOT retired AND id IN (
			SELECT inbox_id FROM subscription WHERE github_app_id = $1 AND NOT cancelled AND expires_at > now())
		ORDER BY id FOR UPDATE`, appID)
	if err != nil {
		return err
	}
	var inboxes []string
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		inboxes = append(inboxes, id.String())
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return err
	}
	if len(inboxes) == 0 {
		return nil
	}
	ids := "{" + strings.Join(inboxes, ",") + "}"
	at := now()
	// A failure's backoff stands; otherwise the source is due at once.
	if _, err := tx.ExecContext(ctx, `
		UPDATE subscription SET generation = generation + 1,
			next_attempt = COALESCE(CASE WHEN error IS NOT NULL THEN next_attempt END, $3::timestamptz)
		WHERE github_app_id = $1 AND NOT cancelled AND expires_at > now() AND inbox_id = ANY($2::uuid[])`,
		appID, ids, at); err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `UPDATE inbox SET next_attempt = $2 WHERE id = ANY($1::uuid[])`, ids, at)
	return err
}

// CleanupGitHubDeliveries deletes bounded batches of raw receipts, generated events and obsolete commit facts
// older than cutoff.
// Expired transition rows are removed even if no inbox consumed them. Inbox entries and current pull-request
// snapshots have independent lifetimes. The GitHub lock serializes this with ingestion and recording.
func (s *Store) CleanupGitHubDeliveries(ctx context.Context, cutoff time.Time) error {
	return s.transaction(ctx, func(tx *sql.Tx) error {
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

// GitHubEffects adapts GitHub source-state transactions to generic inbox wakeup and notification publication.
func GitHubEffects() githubstate.Effects {
	return githubstate.Effects{
		WakeApp: wakeGitHubInboxes,
		Publish: notify,
	}
}
