package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
)

// GitHubReviewRefreshKey independently leases the review history for one PR and installation.
type GitHubReviewRefreshKey struct {
	AppID          int64
	InstallationID int64
	Subject        GitHubSubjectKey
}

// GitHubReviewRefreshState reports the review poll's independent repair deadline.
type GitHubReviewRefreshState struct {
	GitHubRefreshStatus
	RepairAt time.Time
}

// ReviewsState returns the durable review refresh state. A row that has never been acquired is due now.
func (g *GitHubState) ReviewsState(ctx context.Context, key GitHubReviewRefreshKey) (GitHubReviewRefreshState, error) {
	if err := key.validate(); err != nil {
		return GitHubReviewRefreshState{}, err
	}
	row, err := loadRefresh(ctx, g.store.db, key)
	if errors.Is(err, sql.ErrNoRows) {
		return GitHubReviewRefreshState{RepairAt: now()}, nil
	}
	if err != nil {
		return GitHubReviewRefreshState{}, err
	}
	status, err := row.status()
	if err != nil {
		return GitHubReviewRefreshState{}, err
	}
	at := now()
	repairAt := at
	if row.lastSuccessAt.Valid {
		deadline := row.lastSuccessAt.Time.Add(g.freshness)
		if deadline.After(repairAt) {
			repairAt = deadline
		}
	}
	if row.nextAttempt.Valid && row.nextAttempt.Time.After(repairAt) {
		repairAt = row.nextAttempt.Time
	}
	if row.claim.Valid && row.claimUntil.Valid && row.claimUntil.Time.After(repairAt) {
		repairAt = row.claimUntil.Time
	}
	return GitHubReviewRefreshState{GitHubRefreshStatus: status, RepairAt: repairAt.UTC()}, nil
}

func (k GitHubReviewRefreshKey) row() (string, string, []any) {
	return "github_review_refresh", "app_id = $1 AND installation_id = $2 AND repository_id = $3 AND subject_kind = 'pull_request' AND subject_key = $4",
		[]any{k.AppID, k.InstallationID, k.Subject.RepositoryID, k.Subject.Key}
}

func (k GitHubReviewRefreshKey) validate() error {
	if k.AppID <= 0 || k.InstallationID <= 0 || k.Subject.RepositoryID <= 0 || k.Subject.Kind != "pull_request" {
		return invalid("review refresh must name an App, installation and pull request")
	}
	number, err := strconv.ParseInt(k.Subject.Key, 10, 64)
	if err != nil || number <= 0 {
		return invalid("review refresh must name a positive pull request number")
	}
	return nil
}

type githubPRStatePayload struct {
	PullRequestNumber int64 `json:"pullRequestNumber"`
	GitHubReviewObservation
	ObservedAt time.Time `json:"observedAt"`
}

type githubPRLifecyclePayload struct {
	GitHubPRLifecycleObservation
	ObservedAt time.Time `json:"observedAt"`
}

func ensureGitHubReviewRefresh(ctx context.Context, tx *sql.Tx, key GitHubReviewRefreshKey) error {
	if err := key.validate(); err != nil {
		return err
	}
	_, err := tx.ExecContext(ctx, `INSERT INTO github_review_refresh
		(app_id, installation_id, repository_id, subject_kind, subject_key, generation)
		VALUES ($1, $2, $3, 'pull_request', $4, 0) ON CONFLICT DO NOTHING`,
		key.AppID, key.InstallationID, key.Subject.RepositoryID, key.Subject.Key)
	return err
}

func githubReviewParent(ctx context.Context, q querier, key GitHubReviewRefreshKey) (int64, bool, error) {
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

func normalizeGitHubReview(review GitHubReviewObservation) (GitHubReviewObservation, error) {
	if review.ID <= 0 || review.Reviewer.ID <= 0 {
		return GitHubReviewObservation{}, invalid("review observation must name a review and reviewer above 0")
	}
	if err := checkText("reviewer login", review.Reviewer.Login, 1, 255); err != nil {
		return GitHubReviewObservation{}, err
	}
	if !shaPattern.MatchString(review.CommitSHA) {
		return GitHubReviewObservation{}, invalid("review observation commit SHA is invalid")
	}
	if err := checkText("review body", review.Body, 0, 65536); err != nil {
		return GitHubReviewObservation{}, err
	}
	if err := checkText("review HTML URL", review.HTMLURL, 0, 4096); err != nil {
		return GitHubReviewObservation{}, err
	}
	var expectedAction string
	switch review.State {
	case ReviewApproved, ReviewChangesRequested, ReviewCommented:
		expectedAction = "submitted"
	case ReviewDismissed:
		expectedAction = "dismissed"
	default:
		return GitHubReviewObservation{}, invalid("review observation state is invalid")
	}
	if review.Action == nil {
		review.Action = &expectedAction
	} else if *review.Action != "submitted" && *review.Action != "dismissed" && *review.Action != "edited" {
		return GitHubReviewObservation{}, invalid("review observation action is invalid")
	}
	if review.SubmittedAt != nil {
		value := review.SubmittedAt.UTC().Truncate(time.Microsecond)
		review.SubmittedAt = &value
	}
	return review, nil
}

func normalizeGitHubPRLifecycle(observation GitHubPRLifecycleObservation) (GitHubPRLifecycleObservation, error) {
	if observation.PullRequestNumber <= 0 || !shaPattern.MatchString(observation.HeadSHA) {
		return GitHubPRLifecycleObservation{}, invalid("pull request lifecycle must name a positive PR and commit SHA")
	}
	switch observation.State {
	case GitHubPullRequestOpen, GitHubPullRequestClosed, GitHubPullRequestMerged:
	default:
		return GitHubPRLifecycleObservation{}, invalid("pull request lifecycle state is invalid")
	}
	if err := checkText("pull request base ref", observation.BaseRef, 1, 255); err != nil {
		return GitHubPRLifecycleObservation{}, err
	}
	if observation.Action != nil && !actionPattern.MatchString(*observation.Action) {
		return GitHubPRLifecycleObservation{}, invalid("pull request lifecycle action is invalid")
	}
	return observation, nil
}

// observeGitHubPRState stores one review or lifecycle fact and appends a transition only when the normalized
// state changes. Descriptive URL/login and observed timestamps do not make an otherwise identical webhook and
// REST observation distinct. Current payload is never rewritten under an existing event identity.
func observeGitHubPRState(ctx context.Context, tx *sql.Tx, appID, installationID, repositoryID int64,
	pullRequestNumber, entityID int64, event EventName, payload any,
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
	if found && event == EventPullRequestReview && previousState == string(ReviewDismissed) &&
		payload.(githubPRStatePayload).State != ReviewDismissed {
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

func prStateCanonicalExpressions(event EventName) (string, string) {
	if event == EventPullRequest {
		return "jsonb_strip_nulls(payload - 'observedAt' - 'action')", "jsonb_strip_nulls($7::jsonb - 'observedAt' - 'action')"
	}
	return "jsonb_strip_nulls((payload #- '{reviewer,login}') - 'observedAt' - 'htmlUrl' - 'submittedAt' - 'action' - 'body')",
		"jsonb_strip_nulls(($7::jsonb #- '{reviewer,login}') - 'observedAt' - 'htmlUrl' - 'submittedAt' - 'action' - 'body')"
}

func githubPRStateCurrentAction(ctx context.Context, tx *sql.Tx, appID, installationID, repositoryID,
	pullRequestNumber, entityID int64, event EventName, payload any,
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
	pullRequestNumber int64, review GitHubReviewObservation, at time.Time,
) (bool, error) {
	var err error
	review, err = normalizeGitHubReview(review)
	if err != nil {
		return false, err
	}
	changed, err := observeGitHubPRState(ctx, tx, appID, installationID, repositoryID, pullRequestNumber,
		review.ID, EventPullRequestReview,
		githubPRStatePayload{PullRequestNumber: pullRequestNumber, GitHubReviewObservation: review, ObservedAt: at})
	return changed, err
}

func observeGitHubPRLifecycle(ctx context.Context, tx *sql.Tx, appID, installationID, repositoryID int64,
	observation GitHubPRLifecycleObservation, at time.Time,
) (bool, error) {
	var err error
	observation, err = normalizeGitHubPRLifecycle(observation)
	if err != nil {
		return false, err
	}
	payload := githubPRLifecyclePayload{GitHubPRLifecycleObservation: observation, ObservedAt: at}
	if observation.State == GitHubPullRequestOpen && observation.Action == nil {
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
		observation.PullRequestNumber, EventPullRequest, payload)
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

func mustPRNumber(subject GitHubSubjectKey) int64 {
	number, _ := strconv.ParseInt(subject.Key, 10, 64)
	return number
}

func prStateFilterClause(alias string, filters []EventFilter, args *argList) string {
	selected := make([]string, 0, len(filters))
	for _, filter := range filters {
		if filter.Event != EventPullRequest && filter.Event != EventPullRequestReview {
			continue
		}
		part := alias + ".event = " + args.add(string(filter.Event))
		if filter.Actions != nil {
			actions := make([]string, len(filter.Actions))
			for i, action := range filter.Actions {
				actions[i] = args.add(action)
			}
			part += " AND " + alias + ".payload->>'action' IN (" + strings.Join(actions, ", ") + ")"
		}
		selected = append(selected, "("+part+")")
	}
	if len(selected) == 0 {
		return "FALSE"
	}
	return "(" + strings.Join(selected, " OR ") + ")"
}

func prStateFilters(source GitHubSource) []EventFilter {
	filters := source.Filters()
	selected := make([]EventFilter, 0, len(filters))
	for _, filter := range filters {
		if filter.Event == EventPullRequest || filter.Event == EventPullRequestReview {
			selected = append(selected, filter)
		}
	}
	return selected
}

func snapshotGitHubPRState(ctx context.Context, tx *sql.Tx, subscriptionID uuid.UUID, appID, installationID int64,
	subject GitHubSubjectKey, filters []EventFilter,
) error {
	if subject.Kind != "pull_request" {
		return nil
	}
	args := argList{subscriptionID, appID, installationID, subject.RepositoryID, mustPRNumber(subject)}
	selected := prStateFilterClause("c", filters, &args)
	_, err := tx.ExecContext(ctx, `INSERT INTO github_pr_state_subscription_snapshot
		(subscription_id, app_id, installation_id, repository_id, pull_request_number, event, sequence, payload)
		SELECT $1, c.app_id, c.installation_id, c.repository_id, c.pull_request_number, c.event, c.last_sequence, c.payload
		FROM github_pr_state_current c
		JOIN github_repository_access a ON a.app_id = c.app_id AND a.installation_id = c.installation_id
			AND a.repository_id = c.repository_id AND a.valid_until > now() AND a.error IS NULL
		JOIN github_installation i ON i.app_id = a.app_id AND i.installation_id = a.installation_id
			AND i.generation = a.validated_installation_generation
		WHERE c.app_id = $2 AND c.installation_id = $3 AND c.repository_id = $4 AND c.pull_request_number = $5
			AND c.last_sequence > 0 AND `+selected+`
		ON CONFLICT DO NOTHING`, args...)
	return err
}

func (s *Store) recordGitHubPRState(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID,
	recording GitHubRecording,
) (int, bool, error) {
	if recording.Subject.Kind != "pull_request" {
		return 0, false, nil
	}
	var creation []byte
	var after int64
	if err := tx.QueryRowContext(ctx, `SELECT creation, github_pr_state_after_sequence FROM subscription WHERE id = $1`,
		recording.SubscriptionID).Scan(&creation, &after); err != nil {
		return 0, false, err
	}
	var body Subscribe
	if err := decode(creation, &body); err != nil {
		return 0, false, err
	}
	source, ok := body.Source.(GitHubSource)
	if !ok {
		return 0, false, errors.New("PR state recording needs a GitHub subscription")
	}
	filters := prStateFilters(source)
	if len(filters) == 0 {
		return 0, false, nil
	}
	var snapshotArgs argList = []any{recording.SubscriptionID}
	access := make([]string, 0, len(recording.Fences))
	for _, fence := range recording.Fences {
		access = append(access, "(app_id = "+snapshotArgs.add(fence.Key.AppID)+" AND installation_id = "+
			snapshotArgs.add(fence.Key.InstallationID)+" AND repository_id = "+snapshotArgs.add(fence.Key.RepositoryID)+")")
	}
	if len(access) == 0 {
		return 0, false, nil
	}
	rows, err := tx.QueryContext(ctx, `SELECT app_id, installation_id, repository_id, event, sequence, payload
		FROM github_pr_state_subscription_snapshot WHERE subscription_id = $1 AND (`+strings.Join(access, " OR ")+`)
		ORDER BY sequence LIMIT `+snapshotArgs.add(GitHubDeliveryPageSize+1), snapshotArgs...)
	if err != nil {
		return 0, false, err
	}
	type item struct {
		app, installation, repository int64
		event                         EventName
		sequence                      int64
		payload                       json.RawMessage
	}
	var snapshots []item
	for rows.Next() {
		var value item
		if err := rows.Scan(&value.app, &value.installation, &value.repository, &value.event, &value.sequence, &value.payload); err != nil {
			rows.Close()
			return 0, false, err
		}
		snapshots = append(snapshots, value)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return 0, false, err
	}
	more := len(snapshots) > GitHubDeliveryPageSize
	if more {
		snapshots = snapshots[:GitHubDeliveryPageSize]
	}
	count := 0
	for _, value := range snapshots {
		identity := GitHubObservationEvent{AppID: value.app, RepositoryID: value.repository, Event: value.event, Sequence: value.sequence}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, value.payload); err != nil {
			return 0, false, fmt.Errorf("append PR state snapshot: %w", err)
		}
		count++
		if _, err := tx.ExecContext(ctx, `DELETE FROM github_pr_state_subscription_snapshot WHERE subscription_id = $1 AND sequence = $2`,
			recording.SubscriptionID, value.sequence); err != nil {
			return 0, false, err
		}
	}
	if more || count == GitHubDeliveryPageSize {
		return count, true, nil
	}
	remaining := GitHubDeliveryPageSize - count
	var args argList
	accessible := make([]string, 0, len(recording.Fences))
	for _, fence := range recording.Fences {
		accessible = append(accessible, "(e.app_id = "+args.add(fence.Key.AppID)+" AND e.installation_id = "+
			args.add(fence.Key.InstallationID)+" AND e.repository_id = "+args.add(fence.Key.RepositoryID)+")")
	}
	selected := prStateFilterClause("e", filters, &args)
	repository, number, afterSequence := args.add(recording.Subject.RepositoryID), args.add(mustPRNumber(recording.Subject)), args.add(after)
	limit := args.add(remaining + 1)
	query := `SELECT e.app_id, e.installation_id, e.repository_id, e.event, e.sequence, e.payload
		FROM github_pr_state_event e WHERE (` + strings.Join(accessible, " OR ") + `) AND ` + selected + `
		AND e.repository_id = ` + repository + ` AND e.pull_request_number = ` + number + ` AND e.sequence > ` + afterSequence + `
		AND NOT EXISTS (SELECT 1 FROM subscription_match m JOIN entry existing
			ON existing.inbox_id = ` + args.add(inboxID) + ` AND existing.cursor = m.cursor
			WHERE m.subscription_id = ` + args.add(recording.SubscriptionID) + ` AND existing.event = jsonb_build_object(
				'provider', 'github_observation', 'app_id', e.app_id, 'repository_id', e.repository_id,
				'event', e.event, 'sequence', e.sequence))
		ORDER BY e.sequence LIMIT ` + limit
	rows, err = tx.QueryContext(ctx, query, args...)
	if err != nil {
		return 0, false, err
	}
	var events []item
	for rows.Next() {
		var value item
		if err := rows.Scan(&value.app, &value.installation, &value.repository, &value.event, &value.sequence, &value.payload); err != nil {
			rows.Close()
			return 0, false, err
		}
		events = append(events, value)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return 0, false, err
	}
	more = len(events) > remaining
	if more {
		events = events[:remaining]
	}
	for _, value := range events {
		identity := GitHubObservationEvent{AppID: value.app, RepositoryID: value.repository, Event: value.event, Sequence: value.sequence}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, value.payload); err != nil {
			return 0, false, fmt.Errorf("append PR state event: %w", err)
		}
		count++
	}
	return count, more, nil
}
