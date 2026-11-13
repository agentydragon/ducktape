package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// GitHubRefreshStatus is the refresh state of a shared row, which every subscription reading through it sees.
type GitHubRefreshStatus struct {
	LastSuccessAt *time.Time
	// Failure is the current failure of the row's refresh, nil when the last one succeeded.
	Failure *SourceFailure
	// RetryAt is when a failed refresh or pending mergeability computation is next tried.
	RetryAt *time.Time
	// RefreshingUntil is when the lease of a refresh in flight runs out.
	RefreshingUntil *time.Time
}

// GitHubAccessStatus is the state of one installation's access to one repository.
type GitHubAccessStatus struct {
	GitHubRefreshStatus
	Key        GitHubAccessKey
	CheckedAt  *time.Time
	ValidUntil *time.Time
	// CurrentlyValid says the access is known good now: no failure, not expired, and validated under the
	// installation's current generation.
	CurrentlyValid bool
}

// GitHubSubjectStatus is the state of a followed subject.
type GitHubSubjectStatus struct {
	GitHubRefreshStatus
	Key GitHubSubjectKey
}

// GitHubStatus is the shared state behind a GitHub subscription: the accesses it reads through, which are its
// repository's and those of every repository holding a revision of its subject, and the subject.
type GitHubStatus struct {
	// Access is ordered by repository, then installation.
	Access         []GitHubAccessStatus
	Subject        GitHubSubjectStatus
	CheckRuns      *GitHubRefreshStatus
	CommitStatuses *GitHubRefreshStatus
	Reviews        *GitHubRefreshStatus
}

func (row refreshRow) status() (GitHubRefreshStatus, error) {
	status := GitHubRefreshStatus{}
	if row.lastSuccessAt.Valid {
		status.LastSuccessAt = utc(row.lastSuccessAt.Time)
	}
	if row.errorMessage.Valid {
		kind, err := parseFailureKind(row.errorKind.String)
		if err != nil {
			return GitHubRefreshStatus{}, err
		}
		status.Failure = &SourceFailure{
			Kind: kind, Message: row.errorMessage.String, Since: row.errorSince.Time.UTC(), ObservedAt: row.errorObservedAt.Time.UTC(),
		}
	}
	if row.nextAttempt.Valid {
		status.RetryAt = utc(row.nextAttempt.Time)
	}
	if row.claim.Valid && row.claimUntil.Valid {
		status.RefreshingUntil = utc(row.claimUntil.Time)
	}
	return status, nil
}

// loadGitHubStatus fills the status of a GitHub subscription, and leaves any other untouched.
func loadGitHubStatus(ctx context.Context, q querier, subscription *Subscription) error {
	if _, ok := subscription.Source.(model.GitHubSource); !ok {
		return nil
	}
	var app, installation, repository int64
	var subject GitHubSubjectKey
	if err := q.QueryRowContext(ctx, `
		SELECT github_app_id, github_installation_id, github_repository_id, github_subject_kind, github_subject_key
		FROM subscription WHERE id = $1`, subscription.ID).Scan(&app, &installation, &repository, &subject.Kind, &subject.Key); err != nil {
		return fmt.Errorf("subscription %s GitHub binding: %w", subscription.ID, err)
	}
	subject.RepositoryID = repository
	rows, err := q.QueryContext(ctx, `
		SELECT installation_id, repository_id FROM github_repository_access
		WHERE app_id = $1 AND ((repository_id = $2 AND installation_id = $3) OR repository_id IN (
			SELECT head_repository_id FROM github_subject_revision
			WHERE repository_id = $2 AND kind = $4 AND subject_key = $5))
		ORDER BY repository_id, installation_id`, app, repository, installation, subject.Kind, subject.Key)
	if err != nil {
		return err
	}
	var keys []GitHubAccessKey
	for rows.Next() {
		key := GitHubAccessKey{AppID: app}
		if err := rows.Scan(&key.InstallationID, &key.RepositoryID); err != nil {
			rows.Close()
			return err
		}
		keys = append(keys, key)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	if err := rows.Close(); err != nil {
		return err
	}
	status := GitHubStatus{Subject: GitHubSubjectStatus{Key: subject}}
	at := now()
	for _, key := range keys {
		row, err := loadRefresh(ctx, q, key)
		if err != nil {
			return err
		}
		refresh, err := row.status()
		if err != nil {
			return err
		}
		valid, err := accessValid(ctx, q, key, row, at)
		if err != nil {
			return err
		}
		access := GitHubAccessStatus{GitHubRefreshStatus: refresh, Key: key, CurrentlyValid: valid}
		if row.checkedAt.Valid {
			access.CheckedAt = utc(row.checkedAt.Time)
		}
		if row.validUntil.Valid {
			access.ValidUntil = utc(row.validUntil.Time)
		}
		status.Access = append(status.Access, access)
	}
	row, err := loadRefresh(ctx, q, subject)
	if err != nil {
		return err
	}
	if status.Subject.GitHubRefreshStatus, err = row.status(); err != nil {
		return err
	}
	if subject.Kind == "pull_request" {
		for _, event := range []model.EventName{model.EventCheckRun, model.EventStatus} {
			row, err := loadRefresh(ctx, q, GitHubCommitRefreshKey{
				AppID: app, InstallationID: installation, Subject: subject, Event: event,
			})
			if err != nil && !errors.Is(err, sql.ErrNoRows) {
				return err
			}
			if err == nil {
				refresh, err := row.status()
				if err != nil {
					return err
				}
				if event == model.EventCheckRun {
					status.CheckRuns = &refresh
				} else {
					status.CommitStatuses = &refresh
				}
			}
		}
	}
	if subject.Kind == "pull_request" {
		reviewRow, err := loadRefresh(ctx, q, GitHubReviewRefreshKey{AppID: app, InstallationID: installation, Subject: subject})
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if err == nil {
			reviewStatus, err := reviewRow.status()
			if err != nil {
				return err
			}
			status.Reviews = &reviewStatus
		}
	}

	subscription.GitHub = &status
	return nil
}
