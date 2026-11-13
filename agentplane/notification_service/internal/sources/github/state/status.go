package state

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// RefreshStatus is the refresh state of a shared row, which every subscription reading through it sees.
type RefreshStatus struct {
	LastSuccessAt *time.Time
	// Failure is the current failure of the row's refresh, nil when the last one succeeded.
	Failure *model.SourceFailure
	// RetryAt is when a failed refresh or pending mergeability computation is next tried.
	RetryAt *time.Time
	// RefreshingUntil is when the lease of a refresh in flight runs out.
	RefreshingUntil *time.Time
}

// AccessStatus is the state of one installation's access to one repository.
type AccessStatus struct {
	RefreshStatus
	Key        AccessKey
	CheckedAt  *time.Time
	ValidUntil *time.Time
	// CurrentlyValid says the access is known good now: no failure, not expired, and validated under the
	// installation's current generation.
	CurrentlyValid bool
}

// SubjectStatus is the state of a followed subject.
type SubjectStatus struct {
	RefreshStatus
	Key SubjectKey
}

// Status is the shared state behind a GitHub subscription: the accesses it reads through, which are its
// repository's and those of every repository holding a revision of its subject, and the subject.
type Status struct {
	// Access is ordered by repository, then installation.
	Access         []AccessStatus
	Subject        SubjectStatus
	CheckRuns      *RefreshStatus
	CommitStatuses *RefreshStatus
	Reviews        *RefreshStatus
}

// LoadStatus returns the shared source state read within q's snapshot. The caller supplies the binding stored on
// the generic subscription; all GitHub-owned row reads stay behind this package boundary.
func LoadStatus(ctx context.Context, q Querier, appID, installationID int64, subject SubjectKey) (Status, error) {
	rows, err := q.QueryContext(ctx, `
		SELECT installation_id, repository_id FROM github_repository_access
		WHERE app_id = $1 AND ((repository_id = $2 AND installation_id = $3) OR repository_id IN (
			SELECT head_repository_id FROM github_subject_revision
			WHERE repository_id = $2 AND kind = $4 AND subject_key = $5))
		ORDER BY repository_id, installation_id`, appID, subject.RepositoryID, installationID, subject.Kind, subject.Key)
	if err != nil {
		return Status{}, err
	}
	var keys []AccessKey
	for rows.Next() {
		key := AccessKey{AppID: appID}
		if err := rows.Scan(&key.InstallationID, &key.RepositoryID); err != nil {
			rows.Close()
			return Status{}, err
		}
		keys = append(keys, key)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return Status{}, err
	}

	status := Status{Subject: SubjectStatus{Key: subject}}
	at := now()
	for _, key := range keys {
		row, err := loadRefresh(ctx, q, key)
		if err != nil {
			return Status{}, err
		}
		refresh, err := row.status()
		if err != nil {
			return Status{}, err
		}
		valid, err := accessValid(ctx, q, key, row, at)
		if err != nil {
			return Status{}, err
		}
		access := AccessStatus{RefreshStatus: refresh, Key: key, CurrentlyValid: valid}
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
		return Status{}, err
	}
	if status.Subject.RefreshStatus, err = row.status(); err != nil {
		return Status{}, err
	}
	if subject.Kind == "pull_request" {
		for _, event := range []model.EventName{model.EventCheckRun, model.EventStatus} {
			row, err := loadRefresh(ctx, q, CommitRefreshKey{
				AppID: appID, InstallationID: installationID, Subject: subject, Event: event,
			})
			if err != nil && !errors.Is(err, sql.ErrNoRows) {
				return Status{}, err
			}
			if err == nil {
				refresh, err := row.status()
				if err != nil {
					return Status{}, err
				}
				if event == model.EventCheckRun {
					status.CheckRuns = &refresh
				} else {
					status.CommitStatuses = &refresh
				}
			}
		}
		row, err := loadRefresh(ctx, q, ReviewRefreshKey{AppID: appID, InstallationID: installationID, Subject: subject})
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return Status{}, err
		}
		if err == nil {
			review, err := row.status()
			if err != nil {
				return Status{}, err
			}
			status.Reviews = &review
		}
	}
	return status, nil
}

func (row refreshRow) status() (RefreshStatus, error) {
	status := RefreshStatus{}
	if row.lastSuccessAt.Valid {
		status.LastSuccessAt = utc(row.lastSuccessAt.Time)
	}
	if row.errorMessage.Valid {
		kind, err := model.ParseSourceFailureKind(row.errorKind.String)
		if err != nil {
			return RefreshStatus{}, err
		}
		status.Failure = &model.SourceFailure{
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
