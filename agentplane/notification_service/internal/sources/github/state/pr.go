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
)

var reviewRefresh = refreshTable("github_review_refresh", "app_id", "installation_id", "repository_id", "subject_kind", "subject_key")

// ReviewRefreshKey independently leases the review history for one PR and installation.
type ReviewRefreshKey struct {
	AppID          int64
	InstallationID int64
	Subject        SubjectKey
}

// ReviewsState returns the durable review refresh state.
func (g *State) ReviewsState(ctx context.Context, key ReviewRefreshKey) (RefreshStatus, error) {
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

func (k ReviewRefreshKey) refresh() (lease.Table, []any) {
	return reviewRefresh, []any{k.AppID, k.InstallationID, k.Subject.RepositoryID, "pull_request", k.Subject.Key}
}

func (k ReviewRefreshKey) validate() error {
	if k.AppID <= 0 || k.InstallationID <= 0 || k.Subject.RepositoryID <= 0 || k.Subject.Kind != "pull_request" {
		return model.Invalid("review refresh must name an App, installation and pull request")
	}
	number, err := strconv.ParseInt(k.Subject.Key, 10, 64)
	if err != nil || number <= 0 {
		return model.Invalid("review refresh must name a positive pull request number")
	}
	return nil
}

type githubPRStatePayload struct {
	PullRequestNumber int64 `json:"pullRequestNumber"`
	model.GitHubReviewObservation
	ObservedAt time.Time `json:"observedAt"`
}

type githubPRLifecyclePayload struct {
	model.GitHubPRLifecycleObservation
	ObservedAt time.Time `json:"observedAt"`
}

func ensureGitHubReviewRefresh(ctx context.Context, tx *sql.Tx, key ReviewRefreshKey) error {
	if err := key.validate(); err != nil {
		return err
	}
	_, err := tx.ExecContext(ctx, `INSERT INTO github_review_refresh
		(app_id, installation_id, repository_id, subject_kind, subject_key, generation)
		VALUES ($1, $2, $3, 'pull_request', $4, 0) ON CONFLICT DO NOTHING`,
		key.AppID, key.InstallationID, key.Subject.RepositoryID, key.Subject.Key)
	return err
}

func githubReviewParent(ctx context.Context, q Querier, key ReviewRefreshKey) (int64, bool, error) {
	var generation int64
	var exists bool
	err := q.QueryRowContext(ctx, `SELECT s.generation, p.repository_id IS NOT NULL
		FROM github_subject s LEFT JOIN github_pull_request p ON p.repository_id = s.repository_id
			AND p.number::text = s.subject_key
		WHERE s.repository_id = $1 AND s.kind = 'pull_request' AND s.subject_key = $2`,
		key.Subject.RepositoryID, key.Subject.Key).Scan(&generation, &exists)
	return generation, exists, err
}

func invalidateGitHubReviewForInstallation(ctx context.Context, tx *sql.Tx, appID, installationID int64) error {
	_, err := tx.ExecContext(ctx, `UPDATE github_review_refresh SET generation = generation + 1, last_success_at = NULL,
		next_attempt = CASE WHEN error IS NOT NULL THEN next_attempt END, claim = NULL, claim_until = NULL
		WHERE app_id = $1 AND installation_id = $2`, appID, installationID)
	return err
}

func invalidateGitHubReviewForPullRequest(ctx context.Context, tx *sql.Tx, repositoryID int64, number int64) error {
	_, err := tx.ExecContext(ctx, `UPDATE github_review_refresh SET generation = generation + 1, last_success_at = NULL,
		next_attempt = CASE WHEN error IS NOT NULL THEN next_attempt END, claim = NULL, claim_until = NULL
		WHERE repository_id = $1 AND subject_kind = 'pull_request' AND subject_key = $2`, repositoryID, strconv.FormatInt(number, 10))
	return err
}

func invalidateOtherGitHubReviewRefreshes(ctx context.Context, tx *sql.Tx, appID, installationID,
	repositoryID, number int64,
) error {
	_, err := tx.ExecContext(ctx, `UPDATE github_review_refresh SET generation = generation + 1, last_success_at = NULL,
		next_attempt = CASE WHEN error IS NOT NULL THEN next_attempt END, claim = NULL, claim_until = NULL
		WHERE repository_id = $3 AND subject_kind = 'pull_request' AND subject_key = $4
			AND NOT (app_id = $1 AND installation_id = $2)`,
		appID, installationID, repositoryID, strconv.FormatInt(number, 10))
	return err
}

// observeGitHubPRState stores one review or lifecycle fact and appends a transition only when the normalized
// state changes. Descriptive URL/login and observed timestamps do not make an otherwise identical webhook and
// REST observation distinct. Current payload is never rewritten under an existing event identity.
func observeGitHubPRState(ctx context.Context, tx *sql.Tx, appID, installationID, repositoryID int64,
	pullRequestNumber, entityID int64, event model.EventName, payload any,
) (bool, error) {
	encoded, err := json.Marshal(payload)
	if err != nil {
		return false, err
	}
	canonical, rightCanonical := prStateCanonicalExpressions(event)
	var sameState bool
	var previousState string
	err = tx.QueryRowContext(ctx, `SELECT payload->>'state', `+canonical+` = `+rightCanonical+`, last_sequence
		FROM github_pr_state_current WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3
			AND pull_request_number = $4 AND event = $5 AND entity_id = $6`,
		appID, installationID, repositoryID, pullRequestNumber, event, entityID, string(encoded)).
		Scan(&previousState, &sameState, new(int64))
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return false, err
	}
	found := err == nil
	if found && event == model.EventPullRequestReview && previousState == string(model.ReviewDismissed) &&
		payload.(githubPRStatePayload).State != model.ReviewDismissed {
		return false, nil
	}
	if found && sameState {
		return false, nil
	}
	var sequence int64
	if err := tx.QueryRowContext(ctx, `INSERT INTO github_pr_state_event
		(app_id, installation_id, repository_id, pull_request_number, event, payload)
		VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING sequence`,
		appID, installationID, repositoryID, pullRequestNumber, event, string(encoded)).Scan(&sequence); err != nil {
		return false, err
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO github_pr_state_current
		(app_id, installation_id, repository_id, pull_request_number, event, entity_id, last_sequence, payload)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
		ON CONFLICT (app_id, installation_id, repository_id, pull_request_number, event, entity_id)
		DO UPDATE SET last_sequence = EXCLUDED.last_sequence, payload = EXCLUDED.payload`,
		appID, installationID, repositoryID, pullRequestNumber, event, entityID, sequence, string(encoded))
	return true, err
}

func prStateCanonicalExpressions(event model.EventName) (string, string) {
	if event == model.EventPullRequest {
		return "jsonb_strip_nulls(payload - 'observedAt' - 'action')", "jsonb_strip_nulls($7::jsonb - 'observedAt' - 'action')"
	}
	return "jsonb_strip_nulls((payload #- '{reviewer,login}') - 'observedAt' - 'htmlUrl' - 'submittedAt' - 'action' - 'body')",
		"jsonb_strip_nulls(($7::jsonb #- '{reviewer,login}') - 'observedAt' - 'htmlUrl' - 'submittedAt' - 'action' - 'body')"
}

func githubPRStateCurrentAction(ctx context.Context, tx *sql.Tx, appID, installationID, repositoryID,
	pullRequestNumber, entityID int64, event model.EventName, payload any,
) (bool, sql.NullString, error) {
	encoded, err := json.Marshal(payload)
	if err != nil {
		return false, sql.NullString{}, err
	}
	left, right := prStateCanonicalExpressions(event)
	var same bool
	var action sql.NullString
	err = tx.QueryRowContext(ctx, `SELECT `+left+` = `+right+`, NULLIF(payload->>'action', '')
		FROM github_pr_state_current WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3
			AND pull_request_number = $4 AND event = $5 AND entity_id = $6`,
		appID, installationID, repositoryID, pullRequestNumber, event, entityID, string(encoded)).Scan(&same, &action)
	if errors.Is(err, sql.ErrNoRows) {
		return false, sql.NullString{}, nil
	}
	return same, action, err
}

func observeGitHubPRReview(ctx context.Context, tx *sql.Tx, appID, installationID, repositoryID,
	pullRequestNumber int64, review model.GitHubReviewObservation, at time.Time,
) (bool, error) {
	var err error
	review, err = model.NormalizeGitHubReview(review)
	if err != nil {
		return false, err
	}
	changed, err := observeGitHubPRState(ctx, tx, appID, installationID, repositoryID, pullRequestNumber,
		review.ID, model.EventPullRequestReview,
		githubPRStatePayload{PullRequestNumber: pullRequestNumber, GitHubReviewObservation: review, ObservedAt: at})
	return changed, err
}

func observeGitHubPRLifecycle(ctx context.Context, tx *sql.Tx, appID, installationID, repositoryID int64,
	observation model.GitHubPRLifecycleObservation, at time.Time,
) (bool, error) {
	var err error
	observation, err = model.NormalizeGitHubPRLifecycle(observation)
	if err != nil {
		return false, err
	}
	payload := githubPRLifecyclePayload{GitHubPRLifecycleObservation: observation, ObservedAt: at}
	if observation.State == model.GitHubPullRequestOpen && observation.Action == nil {
		var exists bool
		err := tx.QueryRowContext(ctx, `SELECT EXISTS (SELECT 1 FROM github_pr_state_current
			WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3 AND pull_request_number = $4
				AND event = 'pull_request' AND entity_id = $4)`,
			appID, installationID, repositoryID, observation.PullRequestNumber).Scan(&exists)
		if err != nil {
			return false, err
		}
		if !exists {
			encoded, err := json.Marshal(payload)
			if err != nil {
				return false, err
			}
			_, err = tx.ExecContext(ctx, `INSERT INTO github_pr_state_current
				(app_id, installation_id, repository_id, pull_request_number, event, entity_id, last_sequence, payload)
				VALUES ($1, $2, $3, $4, 'pull_request', $4, 0, $5::jsonb) ON CONFLICT DO NOTHING`,
				appID, installationID, repositoryID, observation.PullRequestNumber, string(encoded))
			return false, err
		}
	}
	changed, err := observeGitHubPRState(ctx, tx, appID, installationID, repositoryID, observation.PullRequestNumber,
		observation.PullRequestNumber, model.EventPullRequest, payload)
	if err != nil || !changed {
		return changed, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE github_subject SET generation = generation + 1
		WHERE repository_id = $1 AND kind = 'pull_request' AND subject_key = $2`,
		repositoryID, strconv.FormatInt(observation.PullRequestNumber, 10)); err != nil {
		return false, err
	}
	if err := invalidateGitHubReviewForPullRequest(ctx, tx, repositoryID, observation.PullRequestNumber); err != nil {
		return false, err
	}
	return true, nil
}

func PRNumber(subject SubjectKey) int64 {
	number, _ := strconv.ParseInt(subject.Key, 10, 64)
	return number
}
