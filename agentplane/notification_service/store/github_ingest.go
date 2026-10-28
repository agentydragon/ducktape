package store

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// ErrDeliveryReused is a delivery ID the App already received with different content.
var ErrDeliveryReused = errors.New("GitHub delivery ID reused with different content")

// GitHubPRReviewObservation ties a normalized review to its pull request.
type GitHubPRReviewObservation struct {
	PullRequestNumber int64
	Review            model.GitHubReviewObservation
}

// GitHubIngest is a validated webhook delivery as it is stored.
type GitHubIngest struct {
	AppID          int64
	DeliveryID     uuid.UUID
	InstallationID int64
	// Repository is nil for an installation event that names none.
	Repository *model.GitHubRepository
	Event      string
	// Digest identifies the delivery's content: SHA-256 over the event name, a NUL byte and the raw body.
	Digest []byte
	// Payload is the raw body, a JSON object.
	Payload json.RawMessage
	Action  *string
	// HeadSHA is the commit the event is about, if it is about one.
	HeadSHA *string
	// Subjects are the subjects the event refers to, all in Repository.
	Subjects []GitHubSubjectKey
	// Revisions are the commits Subjects are seen at.
	Revisions []model.GitHubHeadRevision
	// CheckRun and CommitStatus are native facts extracted from complete webhook payloads. Incomplete payloads
	// remain raw deliveries and do not claim normalized coverage.
	CheckRun     *model.GitHubCheckRun
	CommitStatus *model.GitHubCommitStatus
	// Review and Lifecycle are complete PR state facts from the shared webhook normalizer.
	Review    *GitHubPRReviewObservation
	Lifecycle *model.GitHubPRLifecycleObservation
}

// IngestGitHub stores a delivery and wakes every inbox following the App, and reports whether the delivery was
// new. A redelivery with the same content stores nothing and wakes nothing; with other content it fails with
// ErrDeliveryReused.
//
// An installation or installation_repositories event invalidates what was learned of the installation: its
// generation, and those of its repositories' access rows and subjects, move on, so no result read earlier is
// recorded.
func (s *Store) IngestGitHub(ctx context.Context, ingest GitHubIngest) (bool, error) {
	var created bool
	err := s.transaction(ctx, func(tx *sql.Tx) error {
		// Allocate delivery positions in commit order, which also fences a subscription's start boundary.
		if err := githubLock(ctx, tx); err != nil {
			return err
		}
		var digest []byte
		err := tx.QueryRowContext(ctx, `SELECT digest FROM github_delivery WHERE app_id = $1 AND delivery_id = $2`,
			ingest.AppID, ingest.DeliveryID).Scan(&digest)
		if err == nil {
			if !bytes.Equal(digest, ingest.Digest) {
				return ErrDeliveryReused
			}
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if err := s.storeDelivery(ctx, tx, ingest); err != nil {
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

func (s *Store) storeDelivery(ctx context.Context, tx *sql.Tx, ingest GitHubIngest) error {
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO github_installation (app_id, installation_id, generation) VALUES ($1, $2, 0) ON CONFLICT DO NOTHING`,
		ingest.AppID, ingest.InstallationID); err != nil {
		return err
	}
	if ingest.Event == "installation" || ingest.Event == "installation_repositories" {
		if err := invalidateInstallation(ctx, tx, ingest.AppID, ingest.InstallationID); err != nil {
			return err
		}
	}
	if ingest.Repository != nil {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO github_repository (repository_id, full_name) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
			ingest.Repository.ID, ingest.Repository.Name); err != nil {
			return err
		}
	}
	for _, key := range ingest.Subjects {
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO github_subject (repository_id, kind, subject_key, generation) VALUES ($1, $2, $3, 0)
			ON CONFLICT DO NOTHING`, key.RepositoryID, key.Kind, key.Key); err != nil {
			return err
		}
		if err := addRevisions(ctx, tx, key, ingest.Revisions); err != nil {
			return err
		}
	}
	if err := invalidateMergeability(ctx, tx, ingest); err != nil {
		return err
	}
	commitNormalized := false
	prNormalized := false
	var prNormalizedAction any
	if ingest.Review != nil {
		if ingest.Repository == nil || ingest.Event != string(model.EventPullRequestReview) {
			return errors.New("a normalized review delivery needs its repository and pull_request_review event")
		}
		if ingest.Review.PullRequestNumber <= 0 {
			return model.Invalid("a normalized review delivery needs a positive pull request number")
		}
		reviewObservation := ingest.Review.Review
		if reviewObservation.Action == nil {
			reviewObservation.Action = ingest.Action
		}
		changed, err := observeGitHubPRReview(ctx, tx, ingest.AppID, ingest.InstallationID, ingest.Repository.ID,
			ingest.Review.PullRequestNumber, reviewObservation, now())
		if err != nil {
			return err
		}
		if changed {
			if err := invalidateGitHubReviewForPullRequest(ctx, tx, ingest.Repository.ID,
				ingest.Review.PullRequestNumber); err != nil {
				return err
			}
		}
		review, err := model.NormalizeGitHubReview(reviewObservation)
		if err != nil {
			return err
		}
		covered, action, err := githubPRStateCurrentAction(ctx, tx, ingest.AppID, ingest.InstallationID,
			ingest.Repository.ID, ingest.Review.PullRequestNumber, review.ID, model.EventPullRequestReview,
			githubPRStatePayload{PullRequestNumber: ingest.Review.PullRequestNumber, GitHubReviewObservation: review})
		if err != nil {
			return err
		}
		if covered && (changed || (ingest.Action != nil && action.Valid && action.String == *ingest.Action)) &&
			(ingest.Action == nil || *ingest.Action == "submitted" || *ingest.Action == "dismissed") {
			prNormalized = true
			if ingest.Action != nil && action.Valid && action.String == *ingest.Action {
				prNormalizedAction = *ingest.Action
			}
		}
	}
	if ingest.Lifecycle != nil {
		if ingest.Repository == nil || ingest.Event != string(model.EventPullRequest) {
			return errors.New("a normalized lifecycle delivery needs its repository and pull_request event")
		}
		if ingest.Lifecycle.PullRequestNumber <= 0 {
			return model.Invalid("a normalized lifecycle delivery needs a positive pull request number")
		}
		lifecycle := *ingest.Lifecycle
		if lifecycle.Action == nil {
			lifecycle.Action = ingest.Action
		}
		changed, err := observeGitHubPRLifecycle(ctx, tx, ingest.AppID, ingest.InstallationID,
			ingest.Repository.ID, lifecycle, now())
		if err != nil {
			return err
		}
		covered, action, err := githubPRStateCurrentAction(ctx, tx, ingest.AppID, ingest.InstallationID,
			ingest.Repository.ID, ingest.Lifecycle.PullRequestNumber, ingest.Lifecycle.PullRequestNumber,
			model.EventPullRequest, githubPRLifecyclePayload{GitHubPRLifecycleObservation: lifecycle})
		if err != nil {
			return err
		}
		if covered && (changed || (ingest.Action != nil && action.Valid && action.String == *ingest.Action &&
			*ingest.Action != "edited")) {
			prNormalized = true
			if ingest.Action != nil && action.Valid && action.String == *ingest.Action {
				prNormalizedAction = *ingest.Action
			}
		}
	}
	if run := ingest.CheckRun; run != nil {
		if ingest.Repository == nil {
			return errors.New("a normalized check-run delivery needs its repository")
		}
		if run.RepositoryID == 0 {
			run.RepositoryID = ingest.Repository.ID
		}
		changed, err := observeGitHubCheckRun(ctx, tx, ingest.AppID, ingest.InstallationID, *run, now())
		if err != nil {
			return err
		}
		commitNormalized = true
		if changed {
			if err := invalidateGitHubCommitForRevision(ctx, tx, ingest.AppID, run.RepositoryID, run.HeadSHA, model.EventCheckRun); err != nil {
				return err
			}
		}
	}
	if status := ingest.CommitStatus; status != nil {
		if ingest.Repository == nil {
			return errors.New("a normalized commit-status delivery needs its repository")
		}
		if status.RepositoryID == 0 {
			status.RepositoryID = ingest.Repository.ID
		}
		changed, err := observeGitHubCommitStatus(ctx, tx, ingest.AppID, ingest.InstallationID, *status, now())
		if err != nil {
			return err
		}
		commitNormalized = true
		if changed {
			if err := invalidateGitHubCommitForRevision(ctx, tx, ingest.AppID, status.RepositoryID, status.SHA, model.EventStatus); err != nil {
				return err
			}
		}
	}
	var repositoryID sql.NullInt64
	if ingest.Repository != nil {
		repositoryID = sql.NullInt64{Int64: ingest.Repository.ID, Valid: true}
	}
	var position int64
	if err := tx.QueryRowContext(ctx, `
		INSERT INTO github_delivery (app_id, delivery_id, installation_id, repository_id, event, action, head_sha, digest, payload, commit_normalized, pr_normalized, pr_normalized_action)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12) RETURNING position`,
		ingest.AppID, ingest.DeliveryID, ingest.InstallationID, repositoryID, ingest.Event, ingest.Action, ingest.HeadSHA,
		ingest.Digest, string(ingest.Payload), commitNormalized, prNormalized, prNormalizedAction).Scan(&position); err != nil {
		return err
	}
	for _, key := range ingest.Subjects {
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO github_delivery_subject (delivery_position, repository_id, kind, subject_key) VALUES ($1, $2, $3, $4)
			ON CONFLICT DO NOTHING`, position, key.RepositoryID, key.Kind, key.Key); err != nil {
			return err
		}
	}
	return nil
}

// invalidateInstallation moves on the generation of an installation and of everything validated or refreshed
// under it, and drops the leases on them, so that a refresh in flight records nothing.
func invalidateInstallation(ctx context.Context, tx *sql.Tx, appID, installationID int64) error {
	if _, err := tx.ExecContext(ctx,
		`UPDATE github_installation SET generation = generation + 1 WHERE app_id = $1 AND installation_id = $2`,
		appID, installationID); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `
		UPDATE github_subject SET generation = generation + 1, last_success_at = NULL, claim = NULL, claim_until = NULL
		WHERE repository_id IN (
			SELECT repository_id FROM github_repository_access WHERE app_id = $1 AND installation_id = $2)`,
		appID, installationID); err != nil {
		return err
	}
	_, err := tx.ExecContext(ctx, `
		UPDATE github_repository_access
		SET generation = generation + 1, valid_until = NULL, claim = NULL, claim_until = NULL
		WHERE app_id = $1 AND installation_id = $2`, appID, installationID)
	if err != nil {
		return err
	}
	if err := invalidateGitHubCommitForInstallation(ctx, tx, appID, installationID); err != nil {
		return err
	}
	return invalidateGitHubReviewForInstallation(ctx, tx, appID, installationID)
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
		if err := githubLock(ctx, tx); err != nil {
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
		// Migration 00007 copy-forwards the aggregate CI tables so legacy payloads keep their original schema and
		// rollback data remains available. Bound their retention by the same cutoff while both schemas coexist.
		_, err = tx.ExecContext(ctx, `DELETE FROM github_ci_event WHERE sequence IN (
			SELECT sequence FROM github_ci_event WHERE created_at < $1 ORDER BY created_at, sequence LIMIT 1000
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
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `DELETE FROM github_ci_current WHERE
			(app_id, installation_id, repository_id, head_sha, event, entity_key) IN (
				SELECT c.app_id, c.installation_id, c.repository_id, c.head_sha, c.event, c.entity_key
				FROM github_ci_current c WHERE (c.payload->>'observedAt')::timestamptz < $1
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
