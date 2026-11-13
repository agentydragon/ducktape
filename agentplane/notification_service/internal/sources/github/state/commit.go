package state

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strconv"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	"github.com/google/uuid"
)

var commitRefresh = refreshTable("github_commit_refresh",
	"app_id", "installation_id", "repository_id", "subject_kind", "subject_key", "event")

// CommitRefreshKey is the independently leased repair for one PR, installation, and observation type.
// Keeping check runs and commit statuses separate prevents one endpoint's backoff from delaying the other.
type CommitRefreshKey struct {
	AppID          int64
	InstallationID int64
	Subject        SubjectKey
	Event          model.EventName
}

func (k CommitRefreshKey) refresh() (lease.Table, []any) {
	return commitRefresh, []any{k.AppID, k.InstallationID, k.Subject.RepositoryID, "pull_request", k.Subject.Key, string(k.Event)}
}

func (k CommitRefreshKey) validate() error {
	if k.AppID <= 0 || k.InstallationID <= 0 {
		return model.Invalid("commit observation refresh must name an App and installation above 0")
	}
	if k.Subject.Kind != "pull_request" {
		return model.Invalid("commit observation refresh must name a pull request")
	}
	if k.Subject.RepositoryID <= 0 {
		return model.Invalid("commit observation refresh must name a repository above 0")
	}
	number, err := strconv.ParseInt(k.Subject.Key, 10, 64)
	if err != nil || number <= 0 {
		return model.Invalid("commit observation refresh must name a positive pull request number")
	}
	if k.Event != model.EventCheckRun && k.Event != model.EventStatus {
		return model.Invalid("commit observation refresh event must be check_run or status")
	}
	return nil
}

// CommitState returns the durable refresh state.
func (g *State) CommitState(ctx context.Context, key CommitRefreshKey) (RefreshStatus, error) {
	if err := key.validate(); err != nil {
		return RefreshStatus{}, err
	}
	row, err := loadRefresh(ctx, g.db, key)
	if errors.Is(err, sql.ErrNoRows) {
		return RefreshStatus{}, nil
	}
	if err != nil {
		return RefreshStatus{}, err
	}
	return row.status()
}

// CommitTargets returns only repositories that hold the current PR head SHA. Historical head revisions are
// retained for delayed webhook matching, but are not polled as part of current commit observation recovery.
func (g *State) CommitTargets(ctx context.Context, subject SubjectKey) ([]model.GitHubHeadRevision, error) {
	if subject.Kind != "pull_request" || subject.RepositoryID <= 0 {
		return nil, model.Invalid("commit observation targets need a pull request subject")
	}
	rows, err := g.db.QueryContext(ctx, `
		SELECT DISTINCT r.head_repository_id, h.full_name, r.sha
		FROM github_pull_request p
		JOIN github_subject_revision r ON r.repository_id = p.repository_id AND r.kind = 'pull_request'
			AND r.subject_key = p.number::text AND r.sha = p.head_sha
		JOIN github_repository h ON h.repository_id = r.head_repository_id
		WHERE p.repository_id = $1 AND p.number = $2
		ORDER BY r.head_repository_id, r.sha`, subject.RepositoryID, subject.Key)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var targets []model.GitHubHeadRevision
	for rows.Next() {
		var target model.GitHubHeadRevision
		if err := rows.Scan(&target.Repository.ID, &target.Repository.Name, &target.SHA); err != nil {
			return nil, err
		}
		targets = append(targets, target)
	}
	return targets, rows.Err()
}

type githubCheckRunEventPayload struct {
	model.GitHubCheckRun
	ObservedAt time.Time `json:"observedAt"`
}

type githubCommitStatusEventPayload struct {
	model.GitHubCommitStatus
	ObservedAt time.Time `json:"observedAt"`
}

// observeGitHubCheckRun stores one native check-run state. Equal facts keep their first payload immutable;
// changes append transitions, including a legitimate completed -> in_progress -> completed rerun.
func observeGitHubCheckRun(ctx context.Context, tx *sql.Tx, appID, installationID int64, run model.GitHubCheckRun, at time.Time) (bool, error) {
	run, err := model.NormalizeGitHubCheckRun(run)
	if err != nil {
		return false, err
	}
	payload, err := json.Marshal(githubCheckRunEventPayload{GitHubCheckRun: run, ObservedAt: at})
	if err != nil {
		return false, err
	}
	return observeGitHubCommitFact(ctx, tx, appID, installationID, model.EventCheckRun, run.RepositoryID, run.HeadSHA,
		strconv.FormatInt(run.ID, 10), run.ID, string(payload))
}

// observeGitHubCommitStatus stores the newest status row for its context. Older status IDs cannot replace it.
func observeGitHubCommitStatus(ctx context.Context, tx *sql.Tx, appID, installationID int64, status model.GitHubCommitStatus, at time.Time) (bool, error) {
	status, err := model.NormalizeGitHubCommitStatus(status)
	if err != nil {
		return false, err
	}
	payload, err := json.Marshal(githubCommitStatusEventPayload{GitHubCommitStatus: status, ObservedAt: at})
	if err != nil {
		return false, err
	}
	return observeGitHubCommitFact(ctx, tx, appID, installationID, model.EventStatus, status.RepositoryID, status.SHA,
		status.Context, status.ID, string(payload))
}

func observeGitHubCommitFact(ctx context.Context, tx *sql.Tx, appID, installationID int64, event model.EventName,
	repositoryID int64, sha, entityKey string, id int64, payload string,
) (bool, error) {
	var previousID int64
	var sameState bool
	var sequence int64
	query := `SELECT (payload->>'id')::bigint,
		` + canonicalGitHubCommitFactExpression(event, "payload") + ` = ` + canonicalGitHubCommitFactExpression(event, "$7::jsonb") + `,
		last_sequence
		FROM github_commit_current
		WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3 AND head_sha = $4
			AND event = $5 AND entity_key = $6`
	err := tx.QueryRowContext(ctx, query, appID, installationID, repositoryID, sha, string(event), entityKey, payload).
		Scan(&previousID, &sameState, &sequence)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return false, err
	}
	found := err == nil
	if found {
		if event == model.EventStatus && id <= previousID {
			return false, nil
		}
		if sameState {
			return false, nil
		}
	}
	if err := tx.QueryRowContext(ctx, `INSERT INTO github_commit_event
		(app_id, installation_id, repository_id, head_sha, event, payload)
		VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING sequence`,
		appID, installationID, repositoryID, sha, string(event), payload).Scan(&sequence); err != nil {
		return false, err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO github_commit_current
		(app_id, installation_id, repository_id, head_sha, event, entity_key, last_sequence, payload)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
		ON CONFLICT (app_id, installation_id, repository_id, head_sha, event, entity_key) DO UPDATE SET
			last_sequence = EXCLUDED.last_sequence, payload = EXCLUDED.payload`,
		appID, installationID, repositoryID, sha, string(event), entityKey, sequence, payload); err != nil {
		return false, err
	}
	return true, nil
}

// canonicalGitHubCommitFactExpression compares the native state fields while accepting the legacy aggregate
// payload keys copied by migration 00007. It intentionally excludes descriptive URLs, timestamps, and the old
// webhook event/action fields so polling and webhooks keep sharing one immutable transition identity.
func canonicalGitHubCommitFactExpression(event model.EventName, payload string) string {
	switch event {
	case model.EventCheckRun:
		return `jsonb_strip_nulls(jsonb_build_object(
			'repositoryId', ` + payload + `->'repositoryId',
			'headSha', ` + payload + `->'headSha',
			'id', ` + payload + `->'id',
			'appId', COALESCE(` + payload + `->'appId', ` + payload + `->'checkAppId'),
			'name', ` + payload + `->'name',
			'status', ` + payload + `->'status',
			'conclusion', ` + payload + `->'conclusion'))`
	case model.EventStatus:
		return `jsonb_strip_nulls(jsonb_build_object(
			'repositoryId', ` + payload + `->'repositoryId',
			'sha', COALESCE(` + payload + `->'sha', ` + payload + `->'headSha'),
			'id', ` + payload + `->'id',
			'context', COALESCE(` + payload + `->'context', ` + payload + `->'name'),
			'state', COALESCE(` + payload + `->'state', ` + payload + `->'status')))`
	default:
		panic("unexpected GitHub commit fact event: " + string(event))
	}
}

func ensureGitHubCommitRefresh(ctx context.Context, tx *sql.Tx, key CommitRefreshKey) error {
	if err := key.validate(); err != nil {
		return err
	}
	_, err := tx.ExecContext(ctx, `INSERT INTO github_commit_refresh
		(app_id, installation_id, repository_id, subject_kind, subject_key, event, generation)
		VALUES ($1, $2, $3, 'pull_request', $4, $5, 0) ON CONFLICT DO NOTHING`,
		key.AppID, key.InstallationID, key.Subject.RepositoryID, key.Subject.Key, string(key.Event))
	return err
}

// githubCommitParent reads the PR refresh generation and current head captured by a commit observation lease.
func githubCommitParent(ctx context.Context, q Querier, key CommitRefreshKey) (int64, string, error) {
	var generation int64
	var headSHA sql.NullString
	err := q.QueryRowContext(ctx, `SELECT s.generation, p.head_sha
		FROM github_subject s LEFT JOIN github_pull_request p ON p.repository_id = s.repository_id
			AND p.number::text = s.subject_key
		WHERE s.repository_id = $1 AND s.kind = $2 AND s.subject_key = $3`,
		key.Subject.RepositoryID, key.Subject.Kind, key.Subject.Key).Scan(&generation, &headSHA)
	if err != nil {
		return 0, "", err
	}
	return generation, headSHA.String, nil
}

func invalidateGitHubCommitForInstallation(ctx context.Context, tx *sql.Tx, appID, installationID int64) error {
	_, err := tx.ExecContext(ctx, `UPDATE github_commit_refresh SET generation = generation + 1, last_success_at = NULL,
		next_attempt = CASE WHEN error IS NOT NULL THEN next_attempt END, claim = NULL, claim_until = NULL
		WHERE app_id = $1 AND installation_id = $2`, appID, installationID)
	return err
}

func invalidateGitHubCommitForPullRequest(ctx context.Context, tx *sql.Tx, repositoryID int64, subjectKey string, appID *int64) error {
	query := `UPDATE github_commit_refresh SET generation = generation + 1, last_success_at = NULL,
		next_attempt = CASE WHEN error IS NOT NULL THEN next_attempt END, claim = NULL, claim_until = NULL
		WHERE repository_id = $1 AND subject_kind = 'pull_request' AND subject_key = $2`
	args := []any{repositoryID, subjectKey}
	if appID != nil {
		query += ` AND app_id = $3`
		args = append(args, *appID)
	}
	_, err := tx.ExecContext(ctx, query, args...)
	return err
}

// invalidateGitHubCommitForRevision fences only the in-flight refresh for a webhook's native category.
func invalidateGitHubCommitForRevision(ctx context.Context, tx *sql.Tx, appID, repositoryID int64, sha string, event model.EventName) error {
	if event != model.EventCheckRun && event != model.EventStatus {
		return model.Invalid("commit observation invalidation event must be check_run or status")
	}
	_, err := tx.ExecContext(ctx, `UPDATE github_commit_refresh c
		SET generation = generation + 1, claim = NULL, claim_until = NULL
		WHERE c.app_id = $1 AND c.event = $4 AND c.claim IS NOT NULL AND EXISTS (
			SELECT 1 FROM github_subject_revision r JOIN github_pull_request p
				ON p.repository_id = r.repository_id AND p.number::text = r.subject_key
			WHERE r.repository_id = c.repository_id AND r.kind = c.subject_kind AND r.subject_key = c.subject_key
				AND r.head_repository_id = $2 AND r.sha = $3 AND p.head_sha = $3)`,
		appID, repositoryID, sha, string(event))
	return err
}
