package state

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	"github.com/google/uuid"
)

// githubIngressLock is the advisory lock every transaction that changes shared GitHub state takes first, so
// that delivery positions are allocated in commit order and a subscription's start position is a clean
// boundary. Lock order: this lock, then inboxes. It is never held across a call to GitHub.
const githubIngressLock = 0x474854504E

func Lock(ctx context.Context, tx *sql.Tx) error {
	_, err := tx.ExecContext(ctx, `SELECT pg_advisory_xact_lock($1)`, githubIngressLock)
	return err
}

// Querier is the read-only SQL surface used for source-state reads inside the caller's transaction.
type Querier interface {
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
	QueryRowContext(ctx context.Context, query string, args ...any) *sql.Row
}

// AccessKey names one installation's access to one repository, which is shared by every subscription
// that reads the repository through it.
type AccessKey struct {
	AppID          int64
	InstallationID int64
	RepositoryID   int64
}

// SubjectKey names a subject of a repository as its rows are indexed: by kind and by a key made from the
// subject, which is a decimal number for a pull request or issue, a name for a branch and a SHA for a commit.
type SubjectKey struct {
	RepositoryID int64
	Kind         string
	Key          string
}

// NewSubjectKey is the key of a subject of the repository, or model.ErrInvalid if the subject breaks the bounds
// a subscription's does.
func NewSubjectKey(repositoryID int64, subject model.GitHubSubject) (SubjectKey, error) {
	switch subject := subject.(type) {
	case model.PullRequestSubject:
		if subject.Number <= 0 {
			return SubjectKey{}, model.Invalid("pull request number must be positive")
		}
		return SubjectKey{RepositoryID: repositoryID, Kind: "pull_request", Key: strconv.FormatInt(subject.Number, 10)}, nil
	case model.IssueSubject:
		if subject.Number <= 0 {
			return SubjectKey{}, model.Invalid("issue number must be positive")
		}
		return SubjectKey{RepositoryID: repositoryID, Kind: "issue", Key: strconv.FormatInt(subject.Number, 10)}, nil
	case model.BranchSubject:
		if err := model.CheckText("branch name", subject.Name, 1, 255); err != nil {
			return SubjectKey{}, err
		}
		return SubjectKey{RepositoryID: repositoryID, Kind: "branch", Key: subject.Name}, nil
	case model.CommitSubject:
		if !model.IsCommitSHA(subject.SHA) {
			return SubjectKey{}, model.Invalid("commit SHA must be 40 lowercase hexadecimal characters")
		}
		return SubjectKey{RepositoryID: repositoryID, Kind: "commit", Key: subject.SHA}, nil
	default:
		return SubjectKey{}, model.Invalid("subject must be a pull request, issue, branch or commit")
	}
}

// RefreshKey is a row that holds shared refresh state: a AccessKey or a SubjectKey.
type RefreshKey interface {
	// refresh is the table holding the key's row, with the refresh lease's columns, and the row's key values.
	refresh() (lease.Table, []any)
}

// refreshTable is a table of shared refresh rows identified by key; each row's claim/claim_until is a lease.
func refreshTable(name string, key ...string) lease.Table {
	return lease.Table{Name: name, Key: key, Holder: "claim", Until: "claim_until"}
}

var (
	accessRefresh  = refreshTable("github_repository_access", "app_id", "installation_id", "repository_id")
	subjectRefresh = refreshTable("github_subject", "repository_id", "kind", "subject_key")
)

// refreshWhere is the key's table, WHERE clause, and key arguments. The WHERE placeholders start at $1.
func refreshWhere(key RefreshKey) (table, where string, args []any) {
	t, args := key.refresh()
	terms := make([]string, len(t.Key))
	for i, column := range t.Key {
		terms[i] = fmt.Sprintf("%s = $%d", column, i+1)
	}
	return t.Name, strings.Join(terms, " AND "), args
}

func (k AccessKey) refresh() (lease.Table, []any) {
	return accessRefresh, []any{k.AppID, k.InstallationID, k.RepositoryID}
}

func (k SubjectKey) refresh() (lease.Table, []any) {
	return subjectRefresh, []any{k.RepositoryID, k.Kind, k.Key}
}

// RefreshLease is a claim on refreshing a shared row. Whoever holds it may call GitHub for the row and
// record the outcome, until the claim expires or the row is invalidated.
type RefreshLease struct {
	Key  RefreshKey
	held lease.Lease
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

// AccessFence is the validated access a result was read under. It holds only while the grant is
// unchanged and still valid.
type AccessFence struct {
	Key                    AccessKey
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

func loadRefresh(ctx context.Context, q Querier, key RefreshKey) (refreshRow, error) {
	table, where, args := refreshWhere(key)
	var row refreshRow
	dests := []any{
		&row.generation, &row.lastSuccessAt, &row.errorKind, &row.errorMessage, &row.errorSince,
		&row.errorObservedAt, &row.nextAttempt, &row.claim, &row.claimUntil,
	}
	columns := refreshColumns
	if _, ok := key.(AccessKey); ok {
		columns += accessColumns
		dests = append(dests, &row.validatedInstallationGeneration, &row.checkedAt, &row.validUntil)
	}
	if err := q.QueryRowContext(ctx, "SELECT "+columns+" FROM "+table+" WHERE "+where, args...).Scan(dests...); err != nil {
		return refreshRow{}, fmt.Errorf("%s %+v: %w", table, key, err)
	}
	return row, nil
}

func installationGeneration(ctx context.Context, q Querier, key AccessKey) (int64, error) {
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
func accessValid(ctx context.Context, q Querier, key AccessKey, row refreshRow, at time.Time) (bool, error) {
	if row.errorMessage.Valid || !row.validUntil.Valid || !row.validUntil.Time.After(at) ||
		!row.validatedInstallationGeneration.Valid {
		return false, nil
	}
	generation, err := installationGeneration(ctx, q, key)
	return err == nil && row.validatedInstallationGeneration.Int64 == generation, err
}

// FencesValidTx reports whether every grant still has the generation its fence was taken at and is valid now.
func FencesValidTx(ctx context.Context, q *sql.Tx, fences []AccessFence) (bool, error) {
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

// AddRevisionsTx records the revisions as seen for the subject, with the repositories holding them.
func AddRevisionsTx(ctx context.Context, tx *sql.Tx, key SubjectKey, revisions []model.GitHubHeadRevision) error {
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

// State is the refresh state shared by every subscription that reads the same repository access or
// subject: one lease at a time per row, so GitHub is called once however many subscriptions need the answer.
type State struct {
	db *sql.DB
	// freshness is how long a validation or a subject refresh stays good.
	freshness time.Duration
	effects   Effects
}

// Effects are the generic inbox operations a source-state transaction may need to publish.
type Effects struct {
	WakeApp func(context.Context, *sql.Tx, int64) error
	Publish func(context.Context, *sql.Tx) error
}

func New(db *sql.DB, freshness time.Duration, effects Effects) *State {
	return &State{db: db, freshness: freshness, effects: effects}
}

func (g *State) transaction(ctx context.Context, f func(*sql.Tx) error) error {
	return postgres.Transaction(ctx, g.db, sql.TxOptions{}, f)
}

func (g *State) publish(ctx context.Context, tx *sql.Tx, appID int64) error {
	if g.effects.WakeApp == nil || g.effects.Publish == nil {
		return errors.New("GitHub state publication effects are not configured")
	}
	if err := g.effects.WakeApp(ctx, tx, appID); err != nil {
		return err
	}
	return g.effects.Publish(ctx, tx)
}

func now() time.Time { return time.Now().UTC().Truncate(time.Microsecond) }

func utc(at time.Time) *time.Time {
	value := at.UTC()
	return &value
}

// refreshLease is how long a claim holds a row.
const refreshLease = 30 * time.Second

// Acquire claims the row for a refresh, or returns a nil lease when the row is fresh and needs none. It fails
// with *RefreshDeferredError, naming when to try again, while another claim is live or a failure's backoff has
// not passed.
func (g *State) Acquire(ctx context.Context, key RefreshKey) (*RefreshLease, error) {
	var acquired *RefreshLease
	err := g.transaction(ctx, func(tx *sql.Tx) error {
		if err := Lock(ctx, tx); err != nil {
			return err
		}
		var commitGeneration *int64
		var commitHeadSHA string
		if commit, ok := key.(CommitRefreshKey); ok {
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
		if review, ok := key.(ReviewRefreshKey); ok {
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
		if access, ok := key.(AccessKey); ok {
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
		table, args := key.refresh()
		held, err := table.Acquire(ctx, tx, args, refreshLease)
		var busy *lease.HeldError
		if errors.As(err, &busy) {
			return &RefreshDeferredError{Until: busy.Until.UTC()}
		}
		if err != nil {
			return err
		}
		// Returning the error rolls the acquisition back.
		if row.nextAttempt.Valid && row.nextAttempt.Time.After(at) {
			return &RefreshDeferredError{Until: row.nextAttempt.Time.UTC()}
		}
		acquired = &RefreshLease{
			Key: key, held: held, Generation: row.generation, InstallationGeneration: installation,
			SubjectGeneration: commitGeneration, HeadSHA: commitHeadSHA,
		}
		if reviewGeneration != nil {
			acquired.SubjectGeneration = reviewGeneration
		}
		return nil
	})
	return acquired, err
}

// Fence is the fence of an access that is valid now, or fails with *RefreshDeferredError, naming now, if it is
// not.
func (g *State) Fence(ctx context.Context, key AccessKey) (AccessFence, error) {
	row, err := loadRefresh(ctx, g.db, key)
	if err != nil {
		return AccessFence{}, err
	}
	at := now()
	valid, err := accessValid(ctx, g.db, key, row, at)
	if err != nil {
		return AccessFence{}, err
	}
	if !valid {
		return AccessFence{}, &RefreshDeferredError{Until: at}
	}
	return AccessFence{Key: key, Generation: row.generation, InstallationGeneration: row.validatedInstallationGeneration.Int64}, nil
}

// leased loads the row, failing with *RefreshDeferredError at once unless the lease is still the row's: the
// claim live, and neither the row nor its installation invalidated since.
func leased(ctx context.Context, tx *sql.Tx, refresh RefreshLease) (refreshRow, error) {
	table, _ := refresh.Key.refresh()
	if err := table.Fence(ctx, tx, refresh.held); errors.Is(err, lease.ErrLost) {
		return refreshRow{}, &RefreshDeferredError{Until: now()}
	} else if err != nil {
		return refreshRow{}, err
	}
	row, err := loadRefresh(ctx, tx, refresh.Key)
	if err != nil {
		return refreshRow{}, err
	}
	at := now()
	current := row.generation == refresh.Generation
	if access, ok := refresh.Key.(AccessKey); ok && current {
		generation, err := installationGeneration(ctx, tx, access)
		if err != nil {
			return refreshRow{}, err
		}
		current = refresh.InstallationGeneration != nil && generation == *refresh.InstallationGeneration
	}
	if commit, ok := refresh.Key.(CommitRefreshKey); ok && current {
		generation, headSHA, err := githubCommitParent(ctx, tx, commit)
		if err != nil {
			return refreshRow{}, err
		}
		current = refresh.SubjectGeneration != nil && generation == *refresh.SubjectGeneration && headSHA == refresh.HeadSHA
	}
	if review, ok := refresh.Key.(ReviewRefreshKey); ok && current {
		generation, exists, err := githubReviewParent(ctx, tx, review)
		if err != nil {
			return refreshRow{}, err
		}
		current = exists && refresh.SubjectGeneration != nil && generation == *refresh.SubjectGeneration
	}
	if !current {
		return refreshRow{}, &RefreshDeferredError{Until: at}
	}
	return row, nil
}

// Fail records a failed refresh and releases the lease, and returns when the row may be refreshed again. An
// access row stops being valid. It fails with *RefreshDeferredError, recording nothing, if the lease no longer
// holds.
func (g *State) Fail(ctx context.Context, lease RefreshLease, kind model.SourceFailureKind, message string, retry time.Duration) (time.Time, error) {
	var next time.Time
	err := g.transaction(ctx, func(tx *sql.Tx) error {
		if err := Lock(ctx, tx); err != nil {
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
		table, where, args := refreshWhere(lease.Key)
		kindPlaceholder := len(args) + 1
		set := fmt.Sprintf(`error_kind = $%d, error = $%d, error_since = $%d, error_observed_at = $%d, next_attempt = $%d`,
			kindPlaceholder, kindPlaceholder+1, kindPlaceholder+2, kindPlaceholder+3, kindPlaceholder+4)
		if _, ok := lease.Key.(AccessKey); ok {
			set += fmt.Sprintf(`, valid_until = NULL, checked_at = $%d`, kindPlaceholder+3)
		}
		query := fmt.Sprintf("UPDATE %s SET %s WHERE %s", table, set, where)
		updateArgs := append(append([]any(nil), args...), string(kind), message, since, at, next)
		if _, err := tx.ExecContext(ctx, query, updateArgs...); err != nil {
			return err
		}
		return releaseRefresh(ctx, tx, lease)
	})
	return next, err
}

// releaseRefresh gives back the refresh lease on its row.
func releaseRefresh(ctx context.Context, tx *sql.Tx, refresh RefreshLease) error {
	table, _ := refresh.Key.refresh()
	return table.Release(ctx, tx, refresh.held)
}
