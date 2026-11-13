package state

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// ErrDeliveryReused is a delivery ID the App already received with different content.
var ErrDeliveryReused = errors.New("GitHub delivery ID reused with different content")

// PRReviewObservation ties a normalized review to its pull request.
type PRReviewObservation struct {
	PullRequestNumber int64
	Review            model.GitHubReviewObservation
}

// Ingest is a validated webhook delivery as it is stored.
type Ingest struct {
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
	Subjects []SubjectKey
	// Revisions are the commits Subjects are seen at.
	Revisions []model.GitHubHeadRevision
	// CheckRun and CommitStatus are native facts extracted from complete webhook payloads. Incomplete payloads
	// remain raw deliveries and do not claim normalized coverage.
	CheckRun     *model.GitHubCheckRun
	CommitStatus *model.GitHubCommitStatus
	// Review and Lifecycle are complete PR state facts from the shared webhook normalizer.
	Review    *PRReviewObservation
	Lifecycle *model.GitHubPRLifecycleObservation
}

// IngestTx deduplicates and persists one delivery with its source facts under the shared GitHub lock.
// The caller owns the transaction and any generic inbox publication that follows this write.
func IngestTx(ctx context.Context, tx *sql.Tx, ingest Ingest, at time.Time) (bool, error) {
	if err := Lock(ctx, tx); err != nil {
		return false, err
	}
	var digest []byte
	err := tx.QueryRowContext(ctx, `SELECT digest FROM github_delivery WHERE app_id = $1 AND delivery_id = $2`,
		ingest.AppID, ingest.DeliveryID).Scan(&digest)
	if err == nil {
		if !bytes.Equal(digest, ingest.Digest) {
			return false, ErrDeliveryReused
		}
		return false, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return false, err
	}
	if err := storeDelivery(ctx, tx, ingest, at); err != nil {
		return false, err
	}
	return true, nil
}

func storeDelivery(ctx context.Context, tx *sql.Tx, ingest Ingest, at time.Time) error {
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
		if err := AddRevisionsTx(ctx, tx, key, ingest.Revisions); err != nil {
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
			ingest.Review.PullRequestNumber, reviewObservation, at)
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
			ingest.Repository.ID, lifecycle, at)
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
		changed, err := observeGitHubCheckRun(ctx, tx, ingest.AppID, ingest.InstallationID, *run, at)
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
		changed, err := observeGitHubCommitStatus(ctx, tx, ingest.AppID, ingest.InstallationID, *status, at)
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
