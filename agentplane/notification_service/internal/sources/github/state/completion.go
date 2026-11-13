package state

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// completeRefresh validates the lease and access fences in the same transaction as its writes.
func (g *State) completeRefresh(ctx context.Context, lease RefreshLease, fences []AccessFence, write func(*sql.Tx) error) error {
	return g.transaction(ctx, func(tx *sql.Tx) error {
		if err := Lock(ctx, tx); err != nil {
			return err
		}
		if _, err := leased(ctx, tx, lease); err != nil {
			return err
		}
		valid, err := FencesValidTx(ctx, tx, fences)
		if err != nil {
			return err
		}
		if !valid {
			return &RefreshDeferredError{Until: now()}
		}
		if err := write(tx); err != nil {
			return err
		}
		return releaseRefresh(ctx, tx, lease)
	})
}

// CompleteCheckRuns commits a successful check-run read, including an empty result.
func (g *State) CompleteCheckRuns(ctx context.Context, lease RefreshLease, fences []AccessFence, runs []model.GitHubCheckRun) error {
	return g.completeCommit(ctx, lease, fences, model.EventCheckRun, func(tx *sql.Tx, key CommitRefreshKey, at time.Time, observe func(int64, string, func(int64) (bool, error)) error) error {
		for _, run := range runs {
			if err := observe(run.RepositoryID, run.HeadSHA, func(installationID int64) (bool, error) {
				return observeGitHubCheckRun(ctx, tx, key.AppID, installationID, run, at)
			}); err != nil {
				return err
			}
		}
		return nil
	})
}

// CompleteCommitStatuses commits a successful commit-status read, including an empty result.
func (g *State) CompleteCommitStatuses(ctx context.Context, lease RefreshLease, fences []AccessFence, statuses []model.GitHubCommitStatus) error {
	return g.completeCommit(ctx, lease, fences, model.EventStatus, func(tx *sql.Tx, key CommitRefreshKey, at time.Time, observe func(int64, string, func(int64) (bool, error)) error) error {
		for _, status := range statuses {
			if err := observe(status.RepositoryID, status.SHA, func(installationID int64) (bool, error) {
				return observeGitHubCommitStatus(ctx, tx, key.AppID, installationID, status, at)
			}); err != nil {
				return err
			}
		}
		return nil
	})
}

func (g *State) completeCommit(ctx context.Context, lease RefreshLease, fences []AccessFence, event model.EventName,
	record func(*sql.Tx, CommitRefreshKey, time.Time, func(int64, string, func(int64) (bool, error)) error) error,
) error {
	return g.completeRefresh(ctx, lease, fences, func(tx *sql.Tx) error {
		commit, ok := lease.Key.(CommitRefreshKey)
		if !ok || commit.Event != event {
			return errors.New("commit refresh lease does not match its observation resource")
		}
		baseFence := false
		for _, fence := range fences {
			if fence.Key.AppID == commit.AppID && fence.Key.InstallationID == commit.InstallationID &&
				fence.Key.RepositoryID == commit.Subject.RepositoryID {
				baseFence = true
				break
			}
		}
		if !baseFence {
			return errors.New("a commit observation refresh requires its pull request repository access fence")
		}
		parentGeneration, headSHA, err := githubCommitParent(ctx, tx, commit)
		if err != nil {
			return err
		}
		if lease.SubjectGeneration == nil || parentGeneration != *lease.SubjectGeneration || headSHA != lease.HeadSHA {
			return &RefreshDeferredError{Until: now()}
		}
		at := now()
		table, where, args := refreshWhere(lease.Key)
		successPlaceholder := len(args) + 1
		query := fmt.Sprintf("UPDATE %s SET last_success_at = $%d, error_kind = NULL, error = NULL, "+
			"error_since = NULL, error_observed_at = NULL, next_attempt = NULL WHERE %s",
			table, successPlaceholder, where)
		updateArgs := append(append([]any(nil), args...), at)
		if _, err := tx.ExecContext(ctx, query, updateArgs...); err != nil {
			return err
		}
		changed := false
		observe := func(repositoryID int64, sha string, record func(int64) (bool, error)) error {
			if sha != headSHA {
				return &RefreshDeferredError{Until: now()}
			}
			var installationID int64
			for _, fence := range fences {
				if fence.Key.AppID == commit.AppID && fence.Key.RepositoryID == repositoryID {
					installationID = fence.Key.InstallationID
					if repositoryID == commit.Subject.RepositoryID && installationID == commit.InstallationID {
						break
					}
				}
			}
			if installationID == 0 {
				return errors.New("commit observation repository has no matching access fence")
			}
			var current bool
			if err := tx.QueryRowContext(ctx, `SELECT EXISTS (
					SELECT 1 FROM github_subject_revision r JOIN github_pull_request p
						ON p.repository_id = r.repository_id AND p.number::text = r.subject_key
					WHERE r.repository_id = $1 AND r.kind = 'pull_request' AND r.subject_key = $2
						AND r.head_repository_id = $3 AND r.sha = $4 AND p.head_sha = $4)`,
				commit.Subject.RepositoryID, commit.Subject.Key, repositoryID, sha).Scan(&current); err != nil {
				return err
			}
			if !current {
				return &RefreshDeferredError{Until: now()}
			}
			didChange, err := record(installationID)
			if err != nil {
				return err
			}
			changed = changed || didChange
			return nil
		}
		if err := record(tx, commit, at, observe); err != nil {
			return err
		}
		if changed {
			return g.publish(ctx, tx, commit.AppID)
		}
		return nil
	})
}

// CompleteReviews commits the current submitted and dismissed reviews under their independent refresh lease.
func (g *State) CompleteReviews(ctx context.Context, lease RefreshLease, fences []AccessFence, reviews []model.GitHubReviewObservation) error {
	return g.completeRefresh(ctx, lease, fences, func(tx *sql.Tx) error {
		review, ok := lease.Key.(ReviewRefreshKey)
		if !ok {
			return errors.New("reviews require a review refresh lease")
		}
		baseFence := false
		for _, fence := range fences {
			if fence.Key.AppID == review.AppID && fence.Key.InstallationID == review.InstallationID &&
				fence.Key.RepositoryID == review.Subject.RepositoryID {
				baseFence = true
				break
			}
		}
		if !baseFence {
			return errors.New("a review refresh requires its pull request repository access fence")
		}
		parentGeneration, exists, err := githubReviewParent(ctx, tx, review)
		if err != nil {
			return err
		}
		if !exists || lease.SubjectGeneration == nil || parentGeneration != *lease.SubjectGeneration {
			return &RefreshDeferredError{Until: now()}
		}
		at := now()
		changed := false
		for _, observation := range reviews {
			didChange, err := observeGitHubPRReview(ctx, tx, review.AppID, review.InstallationID,
				review.Subject.RepositoryID, PRNumber(review.Subject), observation, at)
			if err != nil {
				return err
			}
			changed = changed || didChange
		}
		if changed {
			if err := invalidateOtherGitHubReviewRefreshes(ctx, tx, review.AppID, review.InstallationID,
				review.Subject.RepositoryID, PRNumber(review.Subject)); err != nil {
				return err
			}
		}
		table, where, args := refreshWhere(lease.Key)
		settledArgs := append(append([]any(nil), args...), at)
		if _, err := tx.ExecContext(ctx, fmt.Sprintf(`UPDATE %s SET last_success_at = $%d, error_kind = NULL,
				error = NULL, error_since = NULL, error_observed_at = NULL, next_attempt = NULL WHERE %s`, table, len(args)+1, where), settledArgs...); err != nil {
			return err
		}
		if changed {
			return g.publish(ctx, tx, review.AppID)
		}
		return nil
	})
}

// SubjectObservation is the revision association learned by one subject refresh.
// PullRequest is present only when the subject is a pull request.
type SubjectObservation struct {
	Revisions   []model.GitHubHeadRevision
	PullRequest *model.GitHubPullRequestObservation
}

// CompleteSubject commits a subject read and its PR observation together under the subject lease.
func (g *State) CompleteSubject(ctx context.Context, lease RefreshLease, fences []AccessFence, observation SubjectObservation) error {
	return g.completeRefresh(ctx, lease, fences, func(tx *sql.Tx) error {
		subject, ok := lease.Key.(SubjectKey)
		if !ok {
			return errors.New("subject observations require a subject refresh lease")
		}
		if len(observation.Revisions) > 0 {
			if err := AddRevisionsTx(ctx, tx, subject, observation.Revisions); err != nil {
				return err
			}
		}
		at := now()
		table, where, args := refreshWhere(lease.Key)
		settledArgs := append(append([]any(nil), args...), at)
		settled := fmt.Sprintf(`last_success_at = $%d, error_kind = NULL, error = NULL, error_since = NULL,
   error_observed_at = NULL, next_attempt = NULL`, len(args)+1)
		if _, err := tx.ExecContext(ctx, "UPDATE "+table+" SET "+settled+" WHERE "+where, settledArgs...); err != nil {
			return err
		}
		if observation.PullRequest != nil {
			return g.observePullRequest(ctx, tx, subject, fences, *observation.PullRequest, at)
		}
		return nil
	})
}

// CompleteAccess validates repository access. A nil name retains the already stored name.
func (g *State) CompleteAccess(ctx context.Context, lease RefreshLease, repositoryName *string) error {
	return g.completeRefresh(ctx, lease, nil, func(tx *sql.Tx) error {
		if _, ok := lease.Key.(AccessKey); !ok {
			return errors.New("access observations require an access refresh lease")
		}
		table, where, args := refreshWhere(lease.Key)
		at := now()
		successPlaceholder := len(args) + 1
		settledArgs := append(append([]any(nil), args...), at)
		settled := fmt.Sprintf(`last_success_at = $%d, error_kind = NULL, error = NULL, error_since = NULL,
   error_observed_at = NULL, next_attempt = NULL`, successPlaceholder)
		if lease.InstallationGeneration == nil {
			return errors.New("an access lease holds the installation generation it was claimed under")
		}
		if repositoryName != nil {
			result, err := tx.ExecContext(ctx, `UPDATE github_repository SET full_name = $2 WHERE repository_id = $1`,
				args[2], *repositoryName)
			if err != nil {
				return err
			}
			if updated, err := result.RowsAffected(); err != nil || updated != 1 {
				return fmt.Errorf("github_repository %v: %d rows renamed: %w", args[2], updated, err)
			}
		}
		checkedPlaceholder := successPlaceholder + 1
		validUntilPlaceholder := checkedPlaceholder + 1
		installationGenerationPlaceholder := validUntilPlaceholder + 1
		query := fmt.Sprintf("UPDATE %s SET %s, checked_at = $%d, valid_until = $%d, "+
			"validated_installation_generation = $%d WHERE %s", table, settled, checkedPlaceholder,
			validUntilPlaceholder, installationGenerationPlaceholder, where)
		settledArgs = append(settledArgs, at, at.Add(g.freshness), *lease.InstallationGeneration)
		_, err := tx.ExecContext(ctx, query, settledArgs...)
		return err
	})
}
