package state

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strconv"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// UNKNOWN results get bounded settling reads. An exhausted result stays visible until an event invalidates it.
var mergeabilityRetries = [...]time.Duration{5 * time.Second, 15 * time.Second, 45 * time.Second, time.Minute}

// observePullRequest runs under the subject lease and access fences checked by CompleteSubject. Its event and
// observation commit together; inbox fan-out can retry independently without losing a transition.
func (s *State) observePullRequest(ctx context.Context, tx *sql.Tx, subject SubjectKey, fences []AccessFence, observation model.GitHubPullRequestObservation, at time.Time) error {
	if subject.Kind != "pull_request" || len(fences) == 0 {
		return errors.New("a pull request observation needs its subject and repository access fence")
	}
	number, err := strconv.ParseInt(subject.Key, 10, 64)
	if err != nil {
		return err
	}
	var access AccessKey
	foundAccess := false
	for _, fence := range fences {
		if fence.Key.RepositoryID == subject.RepositoryID {
			access = fence.Key
			foundAccess = true
			break
		}
	}
	if !foundAccess {
		return errors.New("pull request observation access does not match its repository")
	}
	if err := observation.Validate(); err != nil {
		return err
	}
	var mergeCommitSHA any
	if proof := observation.MergeCommit; proof != nil {
		mergeCommitSHA = proof.SHA
	}
	var previousHead sql.NullString
	err = tx.QueryRowContext(ctx, `SELECT head_sha FROM github_pull_request WHERE repository_id = $1 AND number = $2`,
		subject.RepositoryID, number).Scan(&previousHead)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	var previousLifecycle struct {
		state   model.GitHubPullRequestState
		headSHA string
		baseRef string
	}
	previousLifecycleFound := false
	err = tx.QueryRowContext(ctx, `SELECT payload->>'state', payload->>'headSha', payload->>'baseRef'
		FROM github_pr_state_current WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3
			AND pull_request_number = $4 AND event = 'pull_request' AND entity_id = $4`,
		access.AppID, access.InstallationID, subject.RepositoryID, number).
		Scan(&previousLifecycle.state, &previousLifecycle.headSHA, &previousLifecycle.baseRef)
	if err == nil {
		previousLifecycleFound = true
	} else if !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	var lifecycleAction *string
	if previousLifecycleFound {
		action := ""
		switch {
		case previousLifecycle.state == model.GitHubPullRequestOpen && observation.State != model.GitHubPullRequestOpen:
			action = "closed"
		case previousLifecycle.state != model.GitHubPullRequestOpen && observation.State == model.GitHubPullRequestOpen:
			action = "reopened"
		case previousLifecycle.headSHA != observation.HeadSHA:
			action = "synchronize"
		case previousLifecycle.baseRef != observation.BaseRef:
			action = "edited"
		}
		if action != "" {
			lifecycleAction = &action
		}
	}
	previous := model.GitHubMergeabilityUnknown
	var attempt int
	if err := tx.QueryRowContext(ctx, `SELECT last_settled_mergeability,
		CASE WHEN (base_ref, head_sha, base_sha, state, mergeability) = ($3, $4, $5, $6, $7)
			THEN mergeability_recheck_attempt ELSE 0 END
		FROM github_pull_request WHERE repository_id = $1 AND number = $2`,
		subject.RepositoryID, number, observation.BaseRef, observation.HeadSHA, observation.BaseSHA,
		observation.State, observation.Mergeability).Scan(&previous, &attempt); err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	pending := observation.State == model.GitHubPullRequestOpen && observation.Mergeability == model.GitHubMergeabilityUnknown
	var recheckAt *time.Time
	unknownRetryPending := pending && attempt < len(mergeabilityRetries)
	conflictRetryPending := observation.State == model.GitHubPullRequestOpen &&
		observation.Mergeability == model.GitHubConflicting && attempt < 3
	if unknownRetryPending || conflictRetryPending {
		next := at.Add(mergeabilityRetries[min(attempt, len(mergeabilityRetries)-1)])
		recheckAt = &next
		attempt = min(attempt+1, len(mergeabilityRetries))
	} else if observation.State != model.GitHubPullRequestOpen || observation.Mergeability == model.GitHubMergeable {
		attempt = 0
	}
	settled := previous
	if observation.State == model.GitHubPullRequestOpen && observation.Mergeability != model.GitHubMergeabilityUnknown {
		settled = observation.Mergeability
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO github_pull_request AS p (repository_id, number, base_ref, head_sha, base_sha, state, mergeability, last_settled_mergeability, observed_at, merge_commit_sha, mergeability_recheck_attempt)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
		ON CONFLICT (repository_id, number) DO UPDATE SET base_ref = EXCLUDED.base_ref,
			head_sha = EXCLUDED.head_sha, base_sha = EXCLUDED.base_sha, state = EXCLUDED.state,
			mergeability = EXCLUDED.mergeability, last_settled_mergeability = EXCLUDED.last_settled_mergeability,
			observed_at = EXCLUDED.observed_at, merge_commit_sha = EXCLUDED.merge_commit_sha,
			mergeability_recheck_attempt = EXCLUDED.mergeability_recheck_attempt,
			snapshot_version = p.snapshot_version + CASE WHEN
				(p.base_ref, p.head_sha, p.base_sha, p.state, p.mergeability) IS DISTINCT FROM
				(EXCLUDED.base_ref, EXCLUDED.head_sha, EXCLUDED.base_sha, EXCLUDED.state, EXCLUDED.mergeability)
				THEN 1 ELSE 0 END`,
		subject.RepositoryID, number, observation.BaseRef, observation.HeadSHA, observation.BaseSHA,
		observation.State, observation.Mergeability, settled, at, mergeCommitSHA, attempt); err != nil {
		return err
	}
	if previousHead.Valid && previousHead.String != observation.HeadSHA {
		if err := invalidateGitHubCommitForPullRequest(ctx, tx, subject.RepositoryID, subject.Key, nil); err != nil {
			return err
		}
	}
	lifecycleChanged, err := observeGitHubPRLifecycle(ctx, tx, access.AppID, access.InstallationID,
		subject.RepositoryID, model.GitHubPRLifecycleObservation{
			PullRequestNumber: number, State: observation.State, HeadSHA: observation.HeadSHA,
			BaseRef: observation.BaseRef, Action: lifecycleAction,
		}, at)
	if err != nil {
		return err
	}
	if lifecycleChanged {
		if err := s.publish(ctx, tx, access.AppID); err != nil {
			return err
		}
	}
	if recheckAt != nil {
		_, err := tx.ExecContext(ctx, `UPDATE github_subject SET next_attempt = $4
			WHERE repository_id = $1 AND kind = $2 AND subject_key = $3`,
			subject.RepositoryID, subject.Kind, subject.Key, *recheckAt)
		if err != nil {
			return err
		}
	}
	if pending || observation.State != model.GitHubPullRequestOpen || previous == settled || (previous == model.GitHubMergeabilityUnknown && settled == model.GitHubMergeable) {
		return nil
	}
	var repository string
	if err := tx.QueryRowContext(ctx, `SELECT full_name FROM github_repository WHERE repository_id = $1`, subject.RepositoryID).Scan(&repository); err != nil {
		return err
	}
	payload, err := json.Marshal(storagecodec.MergeabilityPayload{
		Repository: repository, PullRequestNumber: number, BaseRef: observation.BaseRef,
		HeadSHA: observation.HeadSHA, BaseSHA: observation.BaseSHA, PreviousMergeability: previous,
		Mergeability: settled, ObservedAt: at, MergeCommit: observation.MergeCommit,
	})
	if err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO github_mergeability_event
		(app_id, installation_id, repository_id, pull_request_number, payload) VALUES ($1, $2, $3, $4, $5::jsonb)`,
		access.AppID, access.InstallationID, subject.RepositoryID, number, string(payload)); err != nil {
		return err
	}
	if err := s.publish(ctx, tx, access.AppID); err != nil {
		return err
	}
	return nil
}

// invalidateMergeability fences first observations and in-flight reads whose base may have changed.
// The saved base is authoritative only while no refresh is running; a retarget may race that read.
func invalidateMergeability(ctx context.Context, tx *sql.Tx, ingest Ingest) error {
	for _, subject := range ingest.Subjects {
		switch {
		case ingest.Event == string(model.EventPullRequest) && subject.Kind == "pull_request":
			if _, err := tx.ExecContext(ctx, `UPDATE github_subject
				SET generation = generation + 1, last_success_at = NULL,
					next_attempt = CASE WHEN error IS NOT NULL THEN next_attempt END, claim = NULL, claim_until = NULL
				WHERE repository_id = $1 AND kind = $2 AND subject_key = $3`,
				subject.RepositoryID, subject.Kind, subject.Key); err != nil {
				return err
			}
			if ingest.HeadSHA != nil {
				var currentHead sql.NullString
				err := tx.QueryRowContext(ctx, `SELECT head_sha FROM github_pull_request WHERE repository_id = $1 AND number::text = $2`,
					subject.RepositoryID, subject.Key).Scan(&currentHead)
				if err != nil && !errors.Is(err, sql.ErrNoRows) {
					return err
				}
				if currentHead.Valid && currentHead.String != *ingest.HeadSHA {
					if err := invalidateGitHubCommitForPullRequest(ctx, tx, subject.RepositoryID, subject.Key, &ingest.AppID); err != nil {
						return err
					}
				}
			}
		case subject.Kind == "branch" && (ingest.Event == string(model.EventPush) || ingest.Event == string(model.EventCreate) || ingest.Event == string(model.EventDelete)):
			if _, err := tx.ExecContext(ctx, `UPDATE github_subject s
				SET generation = generation + 1, last_success_at = NULL,
					next_attempt = CASE WHEN error IS NOT NULL THEN next_attempt END, claim = NULL, claim_until = NULL
				WHERE s.repository_id = $1 AND s.kind = 'pull_request' AND (
					s.claim IS NOT NULL
					OR NOT EXISTS (SELECT 1 FROM github_pull_request p WHERE p.repository_id = s.repository_id AND p.number::text = s.subject_key)
					OR EXISTS (SELECT 1 FROM github_pull_request p WHERE p.repository_id = s.repository_id
						AND p.number::text = s.subject_key AND p.state = 'OPEN' AND p.base_ref = $2))`,
				subject.RepositoryID, subject.Key); err != nil {
				return err
			}
		}
	}
	return nil
}
