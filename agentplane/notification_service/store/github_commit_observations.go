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

// GitHubCommitRefreshKey is the independently leased repair for one PR, installation, and observation type.
// Keeping check runs and commit statuses separate prevents one endpoint's backoff from delaying the other.
type GitHubCommitRefreshKey struct {
	AppID          int64
	InstallationID int64
	Subject        GitHubSubjectKey
	Event          EventName
}

func (k GitHubCommitRefreshKey) row() (string, string, []any) {
	return "github_commit_refresh", "app_id = $1 AND installation_id = $2 AND repository_id = $3 AND subject_kind = 'pull_request' AND subject_key = $4 AND event = $5",
		[]any{k.AppID, k.InstallationID, k.Subject.RepositoryID, k.Subject.Key, string(k.Event)}
}

func (k GitHubCommitRefreshKey) validate() error {
	if k.AppID <= 0 || k.InstallationID <= 0 {
		return invalid("commit observation refresh must name an App and installation above 0")
	}
	if k.Subject.Kind != "pull_request" {
		return invalid("commit observation refresh must name a pull request")
	}
	if k.Subject.RepositoryID <= 0 {
		return invalid("commit observation refresh must name a repository above 0")
	}
	number, err := strconv.ParseInt(k.Subject.Key, 10, 64)
	if err != nil || number <= 0 {
		return invalid("commit observation refresh must name a positive pull request number")
	}
	if k.Event != EventCheckRun && k.Event != EventStatus {
		return invalid("commit observation refresh event must be check_run or status")
	}
	return nil
}

// GitHubCommitRefreshState is the independent refresh state and its next repair deadline.
type GitHubCommitRefreshState struct {
	GitHubRefreshStatus
	RepairAt time.Time
}

// CommitState returns the durable refresh state. A row that has never been acquired is due now.
func (g *GitHubState) CommitState(ctx context.Context, key GitHubCommitRefreshKey) (GitHubCommitRefreshState, error) {
	if err := key.validate(); err != nil {
		return GitHubCommitRefreshState{}, err
	}
	row, err := loadRefresh(ctx, g.store.db, key)
	if errors.Is(err, sql.ErrNoRows) {
		return GitHubCommitRefreshState{RepairAt: now()}, nil
	}
	if err != nil {
		return GitHubCommitRefreshState{}, err
	}
	refresh, err := row.status()
	if err != nil {
		return GitHubCommitRefreshState{}, err
	}
	at := now()
	repairAt := at
	if row.lastSuccessAt.Valid {
		successDeadline := row.lastSuccessAt.Time.Add(g.freshness)
		if successDeadline.After(repairAt) {
			repairAt = successDeadline
		}
	}
	if row.nextAttempt.Valid && row.nextAttempt.Time.After(repairAt) {
		repairAt = row.nextAttempt.Time
	}
	if row.claim.Valid && row.claimUntil.Valid && row.claimUntil.Time.After(repairAt) {
		repairAt = row.claimUntil.Time
	}
	return GitHubCommitRefreshState{GitHubRefreshStatus: refresh, RepairAt: repairAt.UTC()}, nil
}

// CommitTargets returns only repositories that hold the current PR head SHA. Historical head revisions are
// retained for delayed webhook matching, but are not polled as part of current commit observation recovery.
func (g *GitHubState) CommitTargets(ctx context.Context, subject GitHubSubjectKey) ([]GitHubHeadRevision, error) {
	if subject.Kind != "pull_request" || subject.RepositoryID <= 0 {
		return nil, invalid("commit observation targets need a pull request subject")
	}
	rows, err := g.store.db.QueryContext(ctx, `
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
	var targets []GitHubHeadRevision
	for rows.Next() {
		var target GitHubHeadRevision
		if err := rows.Scan(&target.Repository.ID, &target.Repository.Name, &target.SHA); err != nil {
			return nil, err
		}
		targets = append(targets, target)
	}
	return targets, rows.Err()
}

type githubCheckRunEventPayload struct {
	GitHubCheckRun
	ObservedAt time.Time `json:"observedAt"`
}

type githubCommitStatusEventPayload struct {
	GitHubCommitStatus
	ObservedAt time.Time `json:"observedAt"`
}

func normalizeGitHubCheckRun(run GitHubCheckRun) (GitHubCheckRun, error) {
	if run.RepositoryID <= 0 || !shaPattern.MatchString(run.HeadSHA) || run.ID <= 0 {
		return GitHubCheckRun{}, invalid("check run must name a repository, commit SHA and ID")
	}
	if run.AppID < 0 {
		return GitHubCheckRun{}, invalid("check run App ID must not be negative")
	}
	if err := checkText("check run name", run.Name, 1, 512); err != nil {
		return GitHubCheckRun{}, err
	}
	if err := checkText("check run details URL", run.DetailsURL, 0, 4096); err != nil {
		return GitHubCheckRun{}, err
	}
	switch run.Status {
	case CheckRunQueued, CheckRunWaiting, CheckRunPending, CheckRunRequested, CheckRunInProgress, CheckRunCompleted:
	default:
		return GitHubCheckRun{}, invalid("check run status is invalid")
	}
	if run.Conclusion != nil {
		if err := checkText("check run conclusion", *run.Conclusion, 1, 128); err != nil {
			return GitHubCheckRun{}, err
		}
		if run.Status != CheckRunCompleted {
			return GitHubCheckRun{}, invalid("non-completed check run cannot have a conclusion")
		}
	}
	if run.Status == CheckRunCompleted && run.Conclusion == nil {
		return GitHubCheckRun{}, invalid("completed check run must have a conclusion")
	}
	if run.StartedAt != nil {
		value := run.StartedAt.UTC().Truncate(time.Microsecond)
		run.StartedAt = &value
	}
	if run.CompletedAt != nil {
		value := run.CompletedAt.UTC().Truncate(time.Microsecond)
		run.CompletedAt = &value
	}
	return run, nil
}

func normalizeGitHubCommitStatus(status GitHubCommitStatus) (GitHubCommitStatus, error) {
	if status.RepositoryID <= 0 || !shaPattern.MatchString(status.SHA) || status.ID <= 0 {
		return GitHubCommitStatus{}, invalid("commit status must name a repository, commit SHA and ID")
	}
	if err := checkText("commit status context", status.Context, 1, 512); err != nil {
		return GitHubCommitStatus{}, err
	}
	if err := checkText("commit status target URL", status.TargetURL, 0, 4096); err != nil {
		return GitHubCommitStatus{}, err
	}
	switch status.State {
	case CommitStatusError, CommitStatusFailure, CommitStatusPending, CommitStatusSuccess:
	default:
		return GitHubCommitStatus{}, invalid("commit status state is invalid")
	}
	if status.UpdatedAt != nil {
		value := status.UpdatedAt.UTC().Truncate(time.Microsecond)
		status.UpdatedAt = &value
	}
	return status, nil
}

// observeGitHubCheckRun stores one native check-run state. Equal facts keep their first payload immutable;
// changes append transitions, including a legitimate completed -> in_progress -> completed rerun.
func observeGitHubCheckRun(ctx context.Context, tx *sql.Tx, appID, installationID int64, run GitHubCheckRun, at time.Time) (bool, error) {
	run, err := normalizeGitHubCheckRun(run)
	if err != nil {
		return false, err
	}
	payload, err := json.Marshal(githubCheckRunEventPayload{GitHubCheckRun: run, ObservedAt: at})
	if err != nil {
		return false, err
	}
	return observeGitHubCommitFact(ctx, tx, appID, installationID, EventCheckRun, run.RepositoryID, run.HeadSHA,
		strconv.FormatInt(run.ID, 10), run.ID, string(payload))
}

// observeGitHubCommitStatus stores the newest status row for its context. Older status IDs cannot replace it.
func observeGitHubCommitStatus(ctx context.Context, tx *sql.Tx, appID, installationID int64, status GitHubCommitStatus, at time.Time) (bool, error) {
	status, err := normalizeGitHubCommitStatus(status)
	if err != nil {
		return false, err
	}
	payload, err := json.Marshal(githubCommitStatusEventPayload{GitHubCommitStatus: status, ObservedAt: at})
	if err != nil {
		return false, err
	}
	return observeGitHubCommitFact(ctx, tx, appID, installationID, EventStatus, status.RepositoryID, status.SHA,
		status.Context, status.ID, string(payload))
}

func observeGitHubCommitFact(ctx context.Context, tx *sql.Tx, appID, installationID int64, event EventName,
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
		if event == EventStatus && id <= previousID {
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
func canonicalGitHubCommitFactExpression(event EventName, payload string) string {
	switch event {
	case EventCheckRun:
		return `jsonb_strip_nulls(jsonb_build_object(
			'repositoryId', ` + payload + `->'repositoryId',
			'headSha', ` + payload + `->'headSha',
			'id', ` + payload + `->'id',
			'appId', COALESCE(` + payload + `->'appId', ` + payload + `->'checkAppId'),
			'name', ` + payload + `->'name',
			'status', ` + payload + `->'status',
			'conclusion', ` + payload + `->'conclusion'))`
	case EventStatus:
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

func ensureGitHubCommitRefresh(ctx context.Context, tx *sql.Tx, key GitHubCommitRefreshKey) error {
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
func githubCommitParent(ctx context.Context, q querier, key GitHubCommitRefreshKey) (int64, string, error) {
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
func invalidateGitHubCommitForRevision(ctx context.Context, tx *sql.Tx, appID, repositoryID int64, sha string, event EventName) error {
	if event != EventCheckRun && event != EventStatus {
		return invalid("commit observation invalidation event must be check_run or status")
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

type githubCommitObservationSelection struct {
	CheckRuns              bool
	CompletedCheckRunsOnly bool
	CommitStatuses         bool
}

func commitObservationSelection(source GitHubSource) githubCommitObservationSelection {
	if _, ok := source.Subject.(PullRequestSubject); !ok {
		return githubCommitObservationSelection{}
	}
	selection := githubCommitObservationSelection{CompletedCheckRunsOnly: source.Events == nil}
	for _, filter := range source.Filters() {
		switch filter.Event {
		case EventCheckRun:
			if source.Events == nil || filter.Actions == nil {
				selection.CheckRuns = true
			}
		case EventStatus:
			selection.CommitStatuses = true
		}
	}
	return selection
}

// CheckRunObservationsSelected reports whether generated check-run facts are selected. Explicit action filters
// select only real webhook actions and leave check-run facts as ordinary raw deliveries.
func (s GitHubSource) CheckRunObservationsSelected() bool {
	return commitObservationSelection(s).CheckRuns
}

// CommitStatusObservationsSelected reports whether generated commit-status facts are selected.
func (s GitHubSource) CommitStatusObservationsSelected() bool {
	return commitObservationSelection(s).CommitStatuses
}

func commitObservationClause(alias string, selection githubCommitObservationSelection, args *argList) string {
	var selected []string
	if selection.CheckRuns {
		part := alias + ".event = " + args.add(string(EventCheckRun))
		if selection.CompletedCheckRunsOnly {
			part += " AND " + alias + ".payload->>'status' = " + args.add(string(CheckRunCompleted))
		}
		selected = append(selected, "("+part+")")
	}
	if selection.CommitStatuses {
		selected = append(selected, "("+alias+".event = "+args.add(string(EventStatus))+")")
	}
	if len(selected) == 0 {
		return "FALSE"
	}
	return "(" + strings.Join(selected, " OR ") + ")"
}

// snapshotGitHubCommitObservations stores the current native state for current PR-head revisions at the
// subscription's immutable creation boundary. New transitions have larger sequences and remain replayable.
func snapshotGitHubCommitObservations(ctx context.Context, tx *sql.Tx, subscriptionID uuid.UUID, appID int64,
	subject GitHubSubjectKey, source GitHubSource,
) error {
	selection := commitObservationSelection(source)
	args := argList{subscriptionID, appID, subject.RepositoryID, subject.Kind, subject.Key}
	selected := commitObservationClause("c", selection, &args)
	_, err := tx.ExecContext(ctx, `INSERT INTO github_commit_subscription_snapshot
		(subscription_id, app_id, installation_id, repository_id, event, sequence, payload)
		SELECT $1, c.app_id, c.installation_id, c.repository_id, c.event, c.last_sequence, c.payload
		FROM (
			SELECT DISTINCT ON (app_id, installation_id, repository_id, head_sha, event,
				CASE WHEN event = 'check_run' THEN COALESCE(payload->>'appId', payload->>'checkAppId') END,
				CASE WHEN event = 'check_run' THEN payload->>'name' END,
				CASE WHEN event = 'check_run' AND COALESCE(payload->>'appId', payload->>'checkAppId') IS NULL THEN entity_key END,
				CASE WHEN event = 'status' THEN entity_key END) *
			FROM github_commit_current
			ORDER BY app_id, installation_id, repository_id, head_sha, event,
				CASE WHEN event = 'check_run' THEN COALESCE(payload->>'appId', payload->>'checkAppId') END,
				CASE WHEN event = 'check_run' THEN payload->>'name' END,
				CASE WHEN event = 'check_run' AND COALESCE(payload->>'appId', payload->>'checkAppId') IS NULL THEN entity_key END,
				CASE WHEN event = 'status' THEN entity_key END,
				(payload->>'id')::bigint DESC
		) c
		JOIN github_pull_request p ON p.repository_id = $3 AND p.number::text = $5 AND p.head_sha = c.head_sha
		JOIN github_repository_access a ON a.app_id = c.app_id AND a.installation_id = c.installation_id
			AND a.repository_id = c.repository_id AND a.valid_until > now() AND a.error IS NULL
		JOIN github_installation i ON i.app_id = a.app_id AND i.installation_id = a.installation_id
			AND i.generation = a.validated_installation_generation
		WHERE c.app_id = $2 AND EXISTS (
			SELECT 1 FROM github_subject_revision r WHERE r.repository_id = $3 AND r.kind = $4 AND r.subject_key = $5
				AND r.head_repository_id = c.repository_id AND r.sha = p.head_sha)
			AND `+selected+`
		ON CONFLICT DO NOTHING`, args...)
	return err
}

// recordGitHubCommitObservations appends current-state snapshots then transitions after the immutable creation
// boundary. Inbox copies and generated identities commit together, so replay after restart is durable.
func (s *Store) recordGitHubCommitObservations(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID,
	recording GitHubRecording,
) (int, bool, error) {
	if recording.Subject.Kind != "pull_request" {
		return 0, false, nil
	}
	var creation []byte
	var after int64
	if err := tx.QueryRowContext(ctx, `SELECT creation, github_commit_after_sequence FROM subscription WHERE id = $1`,
		recording.SubscriptionID).Scan(&creation, &after); err != nil {
		return 0, false, err
	}
	var body Subscribe
	if err := decode(creation, &body); err != nil {
		return 0, false, err
	}
	source, ok := body.Source.(GitHubSource)
	if !ok {
		return 0, false, errors.New("commit observation recording needs a GitHub subscription")
	}
	selection := commitObservationSelection(source)
	if !selection.CheckRuns && !selection.CommitStatuses {
		return 0, false, nil
	}
	snapshotArgs := argList{recording.SubscriptionID}
	snapshotAccess := make([]string, 0, len(recording.Fences))
	for _, fence := range recording.Fences {
		snapshotAccess = append(snapshotAccess, "(app_id = "+snapshotArgs.add(fence.Key.AppID)+
			" AND installation_id = "+snapshotArgs.add(fence.Key.InstallationID)+
			" AND repository_id = "+snapshotArgs.add(fence.Key.RepositoryID)+")")
	}
	rows, err := tx.QueryContext(ctx, `SELECT app_id, installation_id, repository_id, event, sequence, payload
		FROM github_commit_subscription_snapshot WHERE subscription_id = $1 AND (`+strings.Join(snapshotAccess, " OR ")+`)
		ORDER BY sequence LIMIT `+snapshotArgs.add(GitHubDeliveryPageSize+1), snapshotArgs...)
	if err != nil {
		return 0, false, err
	}
	type event struct {
		appID        int64
		installation int64
		repositoryID int64
		event        EventName
		sequence     int64
		payload      json.RawMessage
	}
	var snapshots []event
	for rows.Next() {
		var item event
		if err := rows.Scan(&item.appID, &item.installation, &item.repositoryID, &item.event, &item.sequence, &item.payload); err != nil {
			rows.Close()
			return 0, false, err
		}
		snapshots = append(snapshots, item)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return 0, false, err
	}
	more := len(snapshots) > GitHubDeliveryPageSize
	if more {
		snapshots = snapshots[:GitHubDeliveryPageSize]
	}
	count := 0
	for _, item := range snapshots {
		identity := GitHubObservationEvent{AppID: item.appID, RepositoryID: item.repositoryID, Event: item.event, Sequence: item.sequence}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, item.payload); err != nil {
			return 0, false, fmt.Errorf("append GitHub commit observation snapshot: %w", err)
		}
		count++
		if _, err := tx.ExecContext(ctx, `DELETE FROM github_commit_subscription_snapshot
			WHERE subscription_id = $1 AND sequence = $2`, recording.SubscriptionID, item.sequence); err != nil {
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
		accessible = append(accessible, "(e.app_id = "+args.add(fence.Key.AppID)+" AND e.repository_id = "+args.add(fence.Key.RepositoryID)+
			" AND e.installation_id = "+args.add(fence.Key.InstallationID)+")")
	}
	if len(accessible) == 0 {
		return count, false, nil
	}
	selected := commitObservationClause("e", selection, &args)
	repository := args.add(recording.Subject.RepositoryID)
	kind := args.add(recording.Subject.Kind)
	key := args.add(recording.Subject.Key)
	sequence := args.add(after)
	limit := args.add(remaining + 1)
	query := `SELECT e.app_id, e.installation_id, e.repository_id, e.event, e.sequence, e.payload
		FROM github_commit_event e
		WHERE (` + strings.Join(accessible, " OR ") + `) AND ` + selected + `
			AND EXISTS (SELECT 1 FROM github_subject_revision r
				WHERE r.repository_id = ` + repository + ` AND r.kind = ` + kind + ` AND r.subject_key = ` + key + `
					AND r.head_repository_id = e.repository_id AND r.sha = e.head_sha)
			AND e.sequence > ` + sequence + `
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
	var events []event
	for rows.Next() {
		var item event
		if err := rows.Scan(&item.appID, &item.installation, &item.repositoryID, &item.event, &item.sequence, &item.payload); err != nil {
			rows.Close()
			return 0, false, err
		}
		events = append(events, item)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return 0, false, err
	}
	more = len(events) > remaining
	if more {
		events = events[:remaining]
	}
	for _, item := range events {
		identity := GitHubObservationEvent{AppID: item.appID, RepositoryID: item.repositoryID, Event: item.event, Sequence: item.sequence}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, item.payload); err != nil {
			return 0, false, fmt.Errorf("append GitHub commit observation: %w", err)
		}
		count++
	}
	return count, more, nil
}
