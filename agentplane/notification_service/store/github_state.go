package store

import (
	"context"
	"database/sql"
	"fmt"
	"strconv"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// githubIngressLock is the advisory lock every transaction that changes shared GitHub state takes first, so
// that delivery positions are allocated in commit order and a subscription's start position is a clean
// boundary. Lock order: this lock, then inboxes. It is never held across a call to GitHub.
const githubIngressLock = 0x474854504E

func githubLock(ctx context.Context, tx *sql.Tx) error {
	_, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, githubIngressLock)
	return err
}

// querier is a *sql.DB or a *sql.Tx.
type querier interface {
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

// GitHubAccessKey names one installation's access to one repository, which is shared by every subscription
// that reads the repository through it.
type GitHubAccessKey struct {
	AppID          int64
	InstallationID int64
	RepositoryID   int64
}

// GitHubSubjectKey names a subject of a repository as its rows are indexed: by kind and by a key made from the
// subject, which is a decimal number for a pull request or issue, a name for a branch and a SHA for a commit.
type GitHubSubjectKey struct {
	RepositoryID int64
	Kind         string
	Key          string
}

// NewGitHubSubjectKey is the key of a subject of the repository, or model.ErrInvalid if the subject breaks the bounds
// a subscription's does.
func NewGitHubSubjectKey(repositoryID int64, subject model.GitHubSubject) (GitHubSubjectKey, error) {
	switch subject := subject.(type) {
	case model.PullRequestSubject:
		if subject.Number <= 0 {
			return GitHubSubjectKey{}, model.Invalid("pull request number must be positive")
		}
		return GitHubSubjectKey{RepositoryID: repositoryID, Kind: "pull_request", Key: strconv.FormatInt(subject.Number, 10)}, nil
	case model.IssueSubject:
		if subject.Number <= 0 {
			return GitHubSubjectKey{}, model.Invalid("issue number must be positive")
		}
		return GitHubSubjectKey{RepositoryID: repositoryID, Kind: "issue", Key: strconv.FormatInt(subject.Number, 10)}, nil
	case model.BranchSubject:
		if err := model.CheckText("branch name", subject.Name, 1, 255); err != nil {
			return GitHubSubjectKey{}, err
		}
		return GitHubSubjectKey{RepositoryID: repositoryID, Kind: "branch", Key: subject.Name}, nil
	case model.CommitSubject:
		if !model.IsCommitSHA(subject.SHA) {
			return GitHubSubjectKey{}, model.Invalid("commit SHA must be 40 lowercase hexadecimal characters")
		}
		return GitHubSubjectKey{RepositoryID: repositoryID, Kind: "commit", Key: subject.SHA}, nil
	default:
		return GitHubSubjectKey{}, model.Invalid("subject must be a pull request, issue, branch or commit")
	}
}

// GitHubRefreshKey is a row that holds shared refresh state: a GitHubAccessKey or a GitHubSubjectKey.
type GitHubRefreshKey interface {
	// row is the key's table, WHERE clause, and structured key arguments. The WHERE placeholders start at $1.
	row() (table, where string, args []any)
}

func (k GitHubAccessKey) row() (string, string, []any) {
	return "github_repository_access", "app_id = $1 AND installation_id = $2 AND repository_id = $3",
		[]any{k.AppID, k.InstallationID, k.RepositoryID}
}

func (k GitHubSubjectKey) row() (string, string, []any) {
	return "github_subject", "repository_id = $1 AND kind = $2 AND subject_key = $3", []any{k.RepositoryID, k.Kind, k.Key}
}

// GitHubRefreshLease is a claim on refreshing a shared row. Whoever holds it may call GitHub for the row and
// record the outcome, until the claim expires or the row is invalidated.
type GitHubRefreshLease struct {
	Key   GitHubRefreshKey
	Claim uuid.UUID
	// Generation is the row's generation when claimed: an invalidating event moves it on.
	Generation int64
	// InstallationGeneration is the installation's generation when claimed, for an access row, and nil for a
	// subject.
	InstallationGeneration *int64
	// A CI refresh also holds the PR subject generation and head SHA it reads. PR webhook and poll changes
	// invalidate this lease even though CI owns an independent freshness row.
	SubjectGeneration *int64
	HeadSHA           string
}

// GitHubAccessFence is the validated access a result was read under. It holds only while the grant is
// unchanged and still valid.
type GitHubAccessFence struct {
	Key                    GitHubAccessKey
	Generation             int64
	InstallationGeneration int64
}

// RefreshDeferredError is a refresh that cannot go on now. The shared row owns the diagnostic, so only the time
// to try again is carried.
type RefreshDeferredError struct {
	Until time.Time
}

func (e *RefreshDeferredError) Error() string { return "GitHub shared refresh deferred" }

// refreshRow is the state both kinds of shared row keep.
type refreshRow struct {
	generation      int64
	lastSuccessAt   sql.NullTime
	errorKind       sql.NullString
	errorMessage    sql.NullString
	errorSince      sql.NullTime
	errorObservedAt sql.NullTime
	nextAttempt     sql.NullTime
	claim           uuid.NullUUID
	claimUntil      sql.NullTime

	// An access row also keeps its validation.
	validatedInstallationGeneration sql.NullInt64
	checkedAt                       sql.NullTime
	validUntil                      sql.NullTime
}

const refreshColumns = `generation, last_success_at, error_kind, error, error_since, error_observed_at, next_attempt,
	claim, claim_until`

const accessColumns = `, validated_installation_generation, checked_at, valid_until`

func loadRefresh(ctx context.Context, q querier, key GitHubRefreshKey) (refreshRow, error) {
	table, where, args := key.row()
	var row refreshRow
	dests := []any{
		&row.generation, &row.lastSuccessAt, &row.errorKind, &row.errorMessage, &row.errorSince,
		&row.errorObservedAt, &row.nextAttempt, &row.claim, &row.claimUntil,
	}
	columns := refreshColumns
	if _, ok := key.(GitHubAccessKey); ok {
		columns += accessColumns
		dests = append(dests, &row.validatedInstallationGeneration, &row.checkedAt, &row.validUntil)
	}
	if err := q.QueryRowContext(ctx, "SELECT "+columns+" FROM "+table+" WHERE "+where, args...).Scan(dests...); err != nil {
		return refreshRow{}, fmt.Errorf("%s %+v: %w", table, key, err)
	}
	return row, nil
}

func installationGeneration(ctx context.Context, q querier, key GitHubAccessKey) (int64, error) {
	var generation int64
	err := q.QueryRowContext(ctx, `SELECT generation FROM github_installation WHERE app_id = $1 AND installation_id = $2`,
		key.AppID, key.InstallationID).Scan(&generation)
	if err != nil {
		return 0, fmt.Errorf("github_installation (%d, %d): %w", key.AppID, key.InstallationID, err)
	}
	return generation, nil
}

// accessValid reports whether an access row's validation holds at the time: it recorded no error, has not
// expired, and was made under the installation's current generation.
func accessValid(ctx context.Context, q querier, key GitHubAccessKey, row refreshRow, at time.Time) (bool, error) {
	if row.errorMessage.Valid || !row.validUntil.Valid || !row.validUntil.Time.After(at) ||
		!row.validatedInstallationGeneration.Valid {
		return false, nil
	}
	generation, err := installationGeneration(ctx, q, key)
	return err == nil && row.validatedInstallationGeneration.Int64 == generation, err
}

// fencesValid reports whether every grant still has the generation its fence was taken at and is valid now.
func fencesValid(ctx context.Context, q querier, fences []GitHubAccessFence) (bool, error) {
	at := now()
	for _, fence := range fences {
		row, err := loadRefresh(ctx, q, fence.Key)
		if err != nil {
			return false, err
		}
		if row.generation != fence.Generation || !row.validatedInstallationGeneration.Valid ||
			row.validatedInstallationGeneration.Int64 != fence.InstallationGeneration {
			return false, nil
		}
		if valid, err := accessValid(ctx, q, fence.Key, row, at); err != nil || !valid {
			return false, err
		}
	}
	return true, nil
}

// addRevisions records the revisions as seen for the subject, with the repositories holding them.
func addRevisions(ctx context.Context, tx *sql.Tx, key GitHubSubjectKey, revisions []model.GitHubHeadRevision) error {
	for _, revision := range revisions {
		if _, err := tx.ExecContext(ctx,
			`INSERT INTO github_repository (repository_id, full_name) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
			revision.Repository.ID, revision.Repository.Name); err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO github_subject_revision (repository_id, kind, subject_key, head_repository_id, sha)
			VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING`,
			key.RepositoryID, key.Kind, key.Key, revision.Repository.ID, revision.SHA); err != nil {
			return err
		}
	}
	return nil
}

// GitHubState is the refresh state shared by every subscription that reads the same repository access or
// subject: one lease at a time per row, so GitHub is called once however many subscriptions need the answer.
type GitHubState struct {
	store *Store
	// freshness is how long a validation or a subject refresh stays good.
	freshness time.Duration
}

func NewGitHubState(store *Store, freshness time.Duration) *GitHubState {
	return &GitHubState{store: store, freshness: freshness}
}

// refreshLease is how long a claim holds a row.
const refreshLease = 30 * time.Second

// Acquire claims the row for a refresh, or returns a nil lease when the row is fresh and needs none. It fails
// with *RefreshDeferredError, naming when to try again, while another claim is live or a failure's backoff has
// not passed.
func (g *GitHubState) Acquire(ctx context.Context, key GitHubRefreshKey) (*GitHubRefreshLease, error) {
	var lease *GitHubRefreshLease
	err := g.store.transaction(ctx, func(tx *sql.Tx) error {
		if err := githubLock(ctx, tx); err != nil {
			return err
		}
		var commitGeneration *int64
		var commitHeadSHA string
		if commit, ok := key.(GitHubCommitRefreshKey); ok {
			if err := ensureGitHubCommitRefresh(ctx, tx, commit); err != nil {
				return err
			}
			generation, headSHA, err := githubCommitParent(ctx, tx, commit)
			if err != nil {
				return err
			}
			if headSHA == "" {
				return nil
			}
			commitGeneration = &generation
			commitHeadSHA = headSHA
		}
		var reviewGeneration *int64
		if review, ok := key.(GitHubReviewRefreshKey); ok {
			if err := ensureGitHubReviewRefresh(ctx, tx, review); err != nil {
				return err
			}
			generation, exists, err := githubReviewParent(ctx, tx, review)
			if err != nil {
				return err
			}
			if !exists {
				return nil
			}
			reviewGeneration = &generation
		}
		row, err := loadRefresh(ctx, tx, key)
		if err != nil {
			return err
		}
		at := now()
		var installation *int64
		var fresh bool
		if access, ok := key.(GitHubAccessKey); ok {
			generation, err := installationGeneration(ctx, tx, access)
			if err != nil {
				return err
			}
			installation = &generation
			if fresh, err = accessValid(ctx, tx, access, row, at); err != nil {
				return err
			}
		} else {
			fresh = !row.errorMessage.Valid && row.lastSuccessAt.Valid && row.lastSuccessAt.Time.Add(g.freshness).After(at) &&
				(!row.nextAttempt.Valid || row.nextAttempt.Time.After(at))
		}
		if fresh {
			return nil
		}
		if row.claim.Valid && row.claimUntil.Valid && row.claimUntil.Time.After(at) {
			return &RefreshDeferredError{Until: row.claimUntil.Time.UTC()}
		}
		if row.nextAttempt.Valid && row.nextAttempt.Time.After(at) {
			return &RefreshDeferredError{Until: row.nextAttempt.Time.UTC()}
		}
		claim := uuid.New()
		table, where, args := key.row()
		claimPlaceholder := len(args) + 1
		claimUntilPlaceholder := claimPlaceholder + 1
		query := fmt.Sprintf("UPDATE %s SET claim = $%d, claim_until = $%d WHERE %s",
			table, claimPlaceholder, claimUntilPlaceholder, where)
		updateArgs := append(append([]any(nil), args...), claim, at.Add(refreshLease))
		if _, err := tx.ExecContext(ctx, query, updateArgs...); err != nil {
			return err
		}
		lease = &GitHubRefreshLease{
			Key: key, Claim: claim, Generation: row.generation, InstallationGeneration: installation,
			SubjectGeneration: commitGeneration, HeadSHA: commitHeadSHA,
		}
		if reviewGeneration != nil {
			lease.SubjectGeneration = reviewGeneration
		}
		return nil
	})
	return lease, err
}

// Fence is the fence of an access that is valid now, or fails with *RefreshDeferredError, naming now, if it is
// not.
func (g *GitHubState) Fence(ctx context.Context, key GitHubAccessKey) (GitHubAccessFence, error) {
	row, err := loadRefresh(ctx, g.store.db, key)
	if err != nil {
		return GitHubAccessFence{}, err
	}
	at := now()
	valid, err := accessValid(ctx, g.store.db, key, row, at)
	if err != nil {
		return GitHubAccessFence{}, err
	}
	if !valid {
		return GitHubAccessFence{}, &RefreshDeferredError{Until: at}
	}
	return GitHubAccessFence{Key: key, Generation: row.generation, InstallationGeneration: row.validatedInstallationGeneration.Int64}, nil
}

// leased loads the row, failing with *RefreshDeferredError at once unless the lease is still the row's: the
// claim live, and neither the row nor its installation invalidated since.
func leased(ctx context.Context, tx *sql.Tx, lease GitHubRefreshLease) (refreshRow, error) {
	row, err := loadRefresh(ctx, tx, lease.Key)
	if err != nil {
		return refreshRow{}, err
	}
	at := now()
	current := row.claim.Valid && row.claim.UUID == lease.Claim && row.claimUntil.Valid && row.claimUntil.Time.After(at) &&
		row.generation == lease.Generation
	if access, ok := lease.Key.(GitHubAccessKey); ok && current {
		generation, err := installationGeneration(ctx, tx, access)
		if err != nil {
			return refreshRow{}, err
		}
		current = lease.InstallationGeneration != nil && generation == *lease.InstallationGeneration
	}
	if commit, ok := lease.Key.(GitHubCommitRefreshKey); ok && current {
		generation, headSHA, err := githubCommitParent(ctx, tx, commit)
		if err != nil {
			return refreshRow{}, err
		}
		current = lease.SubjectGeneration != nil && generation == *lease.SubjectGeneration && headSHA == lease.HeadSHA
	}
	if review, ok := lease.Key.(GitHubReviewRefreshKey); ok && current {
		generation, exists, err := githubReviewParent(ctx, tx, review)
		if err != nil {
			return refreshRow{}, err
		}
		current = exists && lease.SubjectGeneration != nil && generation == *lease.SubjectGeneration
	}
	if !current {
		return refreshRow{}, &RefreshDeferredError{Until: at}
	}
	return row, nil
}

// Fail records a failed refresh and releases the lease, and returns when the row may be refreshed again. An
// access row stops being valid. It fails with *RefreshDeferredError, recording nothing, if the lease no longer
// holds.
func (g *GitHubState) Fail(ctx context.Context, lease GitHubRefreshLease, kind SourceFailureKind, message string, retry time.Duration) (time.Time, error) {
	var next time.Time
	err := g.store.transaction(ctx, func(tx *sql.Tx) error {
		if err := githubLock(ctx, tx); err != nil {
			return err
		}
		row, err := leased(ctx, tx, lease)
		if err != nil {
			return err
		}
		at := now()
		since := at
		if row.errorKind.Valid && row.errorKind.String == string(kind) && row.errorSince.Valid {
			since = row.errorSince.Time
		}
		next = at.Add(retry)
		table, where, args := lease.Key.row()
		kindPlaceholder := len(args) + 1
		set := fmt.Sprintf(`error_kind = $%d, error = $%d, error_since = $%d, error_observed_at = $%d, next_attempt = $%d,
			claim = NULL, claim_until = NULL`, kindPlaceholder, kindPlaceholder+1, kindPlaceholder+2,
			kindPlaceholder+3, kindPlaceholder+4)
		if _, ok := lease.Key.(GitHubAccessKey); ok {
			set += fmt.Sprintf(`, valid_until = NULL, checked_at = $%d`, kindPlaceholder+3)
		}
		query := fmt.Sprintf("UPDATE %s SET %s WHERE %s", table, set, where)
		updateArgs := append(append([]any(nil), args...), string(kind), message, since, at, next)
		_, err = tx.ExecContext(ctx, query, updateArgs...)
		return err
	})
	return next, err
}
