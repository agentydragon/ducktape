package state

import (
	"context"
	"database/sql"
	"fmt"
	"time"
)

// These are the reads and writes of shared GitHub state a reconcile makes between its calls to GitHub, each in a
// short transaction of its own: no lock is held while GitHub is called.

// SubjectState is the refresh state a match of a subject is made under.
type SubjectState struct {
	// Generation moves on when an event invalidates the subject.
	Generation int64
	// LastSuccessAt is when the subject was last refreshed, nil while it never was.
	LastSuccessAt *time.Time
	// RetryAt is an explicit pending follow-up, such as mergeability settling or a failed refresh.
	RetryAt *time.Time
}

// Subject reads the refresh state of the subject.
func (g *State) Subject(ctx context.Context, key SubjectKey) (SubjectState, error) {
	row, err := loadRefresh(ctx, g.db, key)
	if err != nil {
		return SubjectState{}, err
	}
	state := SubjectState{Generation: row.generation}
	if row.lastSuccessAt.Valid {
		state.LastSuccessAt = utc(row.lastSuccessAt.Time)
	}
	if row.nextAttempt.Valid {
		state.RetryAt = utc(row.nextAttempt.Time)
	}
	return state, nil
}

// ValidUntil is when the access's last validation stops holding. It fails with *RefreshDeferredError, naming now,
// while the access was never validated or its validation failed.
func (g *State) ValidUntil(ctx context.Context, key AccessKey) (time.Time, error) {
	row, err := loadRefresh(ctx, g.db, key)
	if err != nil {
		return time.Time{}, err
	}
	if !row.validUntil.Valid {
		return time.Time{}, &RefreshDeferredError{Until: now()}
	}
	return row.validUntil.Time, nil
}

// RepositoryName is the name last seen for the repository.
func (g *State) RepositoryName(ctx context.Context, repositoryID int64) (string, error) {
	var name string
	if err := g.db.QueryRowContext(ctx, `SELECT full_name FROM github_repository WHERE repository_id = $1`,
		repositoryID).Scan(&name); err != nil {
		return "", fmt.Errorf("github_repository %d: %w", repositoryID, err)
	}
	return name, nil
}

// RevisionGrants are the App's grants on the repositories, other than the subject's own, that hold a commit the
// subject has been seen at: a pull request's fork. Deliveries of those repositories are owed to the subject's
// subscriptions only under a valid grant.
func (g *State) RevisionGrants(ctx context.Context, appID int64, subject SubjectKey) ([]AccessKey, error) {
	rows, err := g.db.QueryContext(ctx, `
		SELECT installation_id, repository_id FROM github_repository_access
		WHERE app_id = $1 AND repository_id != $2 AND repository_id IN (
			SELECT head_repository_id FROM github_subject_revision WHERE repository_id = $2 AND kind = $3 AND subject_key = $4)
		ORDER BY installation_id, repository_id`, appID, subject.RepositoryID, subject.Kind, subject.Key)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var grants []AccessKey
	for rows.Next() {
		grant := AccessKey{AppID: appID}
		if err := rows.Scan(&grant.InstallationID, &grant.RepositoryID); err != nil {
			return nil, err
		}
		grants = append(grants, grant)
	}
	return grants, rows.Err()
}

// Grant records the installation's access to the repository, and the installation and the repository with it,
// unless they are known. The grant is not valid until it is refreshed.
func (g *State) Grant(ctx context.Context, key AccessKey, repositoryName string) error {
	return g.transaction(ctx, func(tx *sql.Tx) error {
		if err := Lock(ctx, tx); err != nil {
			return err
		}
		return insertGrant(ctx, tx, key, repositoryName)
	})
}

// insertGrant writes the rows of a grant that are not there. The caller holds the GitHub lock.
func insertGrant(ctx context.Context, tx *sql.Tx, key AccessKey, repositoryName string) error {
	for _, statement := range []struct {
		query string
		args  []any
	}{
		{
			`INSERT INTO github_installation (app_id, installation_id, generation) VALUES ($1, $2, 0) ON CONFLICT DO NOTHING`,
			[]any{key.AppID, key.InstallationID},
		},
		{
			`INSERT INTO github_repository (repository_id, full_name) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
			[]any{key.RepositoryID, repositoryName},
		},
		{
			`INSERT INTO github_repository_access (app_id, installation_id, repository_id, generation) VALUES ($1, $2, $3, 0)
				ON CONFLICT DO NOTHING`,
			[]any{key.AppID, key.InstallationID, key.RepositoryID},
		},
	} {
		if _, err := tx.ExecContext(ctx, statement.query, statement.args...); err != nil {
			return err
		}
	}
	return nil
}

// SubjectGenerationTx reads the generation fenced by a recorded source page in its publication transaction.
func SubjectGenerationTx(ctx context.Context, tx *sql.Tx, key SubjectKey) (int64, error) {
	var generation int64
	err := tx.QueryRowContext(ctx, `SELECT generation FROM github_subject
		WHERE repository_id = $1 AND kind = $2 AND subject_key = $3`, key.RepositoryID, key.Kind, key.Key).Scan(&generation)
	if err != nil {
		return 0, fmt.Errorf("github_subject %+v: %w", key, err)
	}
	return generation, nil
}
