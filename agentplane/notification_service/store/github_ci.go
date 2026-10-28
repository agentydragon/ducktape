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

// GitHubCIRefreshKey is the independently leased CI repair for one PR and installation. The installation is
// part of the key because a refresh made with one installation's authority must not delay another's.
type GitHubCIRefreshKey struct {
	AppID          int64
	InstallationID int64
	Subject        GitHubSubjectKey
}

func (k GitHubCIRefreshKey) row() (string, string, []any) {
	return "github_ci_refresh", "app_id = $1 AND installation_id = $2 AND repository_id = $3 AND subject_kind = 'pull_request' AND subject_key = $4",
		[]any{k.AppID, k.InstallationID, k.Subject.RepositoryID, k.Subject.Key}
}

func (k GitHubCIRefreshKey) validate() error {
	if k.AppID <= 0 || k.InstallationID <= 0 {
		return invalid("CI refresh must name an App and installation above 0")
	}
	if k.Subject.Kind != "pull_request" {
		return invalid("CI refresh must name a pull request")
	}
	if k.Subject.RepositoryID <= 0 {
		return invalid("CI refresh must name a repository above 0")
	}
	number, err := strconv.ParseInt(k.Subject.Key, 10, 64)
	if err != nil || number <= 0 {
		return invalid("CI refresh must name a positive pull request number")
	}
	return nil
}

// GitHubCIRefreshState is the independent CI poll state and its next repair deadline.
type GitHubCIRefreshState struct {
	GitHubRefreshStatus
	RepairAt time.Time
}

// CIState returns the durable CI refresh state. A row that has never been acquired is due now.
func (g *GitHubState) CIState(ctx context.Context, key GitHubCIRefreshKey) (GitHubCIRefreshState, error) {
	if err := key.validate(); err != nil {
		return GitHubCIRefreshState{}, err
	}
	row, err := loadRefresh(ctx, g.store.db, key)
	if errors.Is(err, sql.ErrNoRows) {
		return GitHubCIRefreshState{RepairAt: now()}, nil
	}
	if err != nil {
		return GitHubCIRefreshState{}, err
	}
	refresh, err := row.status()
	if err != nil {
		return GitHubCIRefreshState{}, err
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
	return GitHubCIRefreshState{GitHubRefreshStatus: refresh, RepairAt: repairAt.UTC()}, nil
}

// CITargets returns only repositories that hold the current PR head SHA. Historical head revisions are retained
// for delayed webhook matching, but are not polled as part of current CI recovery.
func (g *GitHubState) CITargets(ctx context.Context, subject GitHubSubjectKey) ([]GitHubHeadRevision, error) {
	if subject.Kind != "pull_request" || subject.RepositoryID <= 0 {
		return nil, invalid("CI targets need a pull request subject")
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

type githubCIEventPayload struct {
	GitHubCIObservation
	ObservedAt time.Time `json:"observedAt"`
}

// normalizeGitHubCIObservation accepts the check-run lifecycle states recoverable from the Commit Checks API,
// and commit statuses. Non-state check-run actions remain raw webhook events.
func normalizeGitHubCIObservation(observation GitHubCIObservation) (GitHubCIObservation, bool, error) {
	if observation.RepositoryID <= 0 || !shaPattern.MatchString(observation.HeadSHA) || observation.ID <= 0 {
		return GitHubCIObservation{}, false, invalid("CI observation must name a repository, commit SHA and ID")
	}
	if observation.CheckAppID < 0 {
		return GitHubCIObservation{}, false, invalid("CI check App ID must not be negative")
	}
	if observation.Event != EventCheckRun && observation.Event != EventStatus {
		return GitHubCIObservation{}, false, invalid("CI observation event must be check_run or status")
	}
	if err := checkText("CI observation name", observation.Name, 1, 512); err != nil {
		return GitHubCIObservation{}, false, err
	}
	if err := checkText("CI observation status", observation.Status, 1, 128); err != nil {
		return GitHubCIObservation{}, false, err
	}
	if err := checkText("CI observation details URL", observation.DetailsURL, 0, 4096); err != nil {
		return GitHubCIObservation{}, false, err
	}
	if observation.Conclusion != nil {
		if err := checkText("CI observation conclusion", *observation.Conclusion, 1, 128); err != nil {
			return GitHubCIObservation{}, false, err
		}
	}
	if observation.Event == EventCheckRun {
		switch observation.Status {
		case "queued", "waiting", "pending", "requested", "in_progress", "completed":
		default:
			return observation, false, invalid("check-run observation status is invalid")
		}
		action := "created"
		if observation.Status == "in_progress" {
			action = "in_progress"
		} else if observation.Status == "completed" {
			action = "completed"
		}
		if observation.Action != nil && *observation.Action != action {
			return GitHubCIObservation{}, false, invalid("check-run observation action does not match its status")
		}
		observation.Action = &action
		if observation.Status != "completed" && observation.Conclusion != nil {
			return GitHubCIObservation{}, false, invalid("non-completed check-run observation cannot have a conclusion")
		}
	} else {
		// Commit statuses have no webhook action selector.
		observation.Action = nil
	}
	if observation.StartedAt != nil {
		value := observation.StartedAt.UTC().Truncate(time.Microsecond)
		observation.StartedAt = &value
	}
	if observation.CompletedAt != nil {
		value := observation.CompletedAt.UTC().Truncate(time.Microsecond)
		observation.CompletedAt = &value
	}
	if observation.UpdatedAt != nil {
		value := observation.UpdatedAt.UTC().Truncate(time.Microsecond)
		observation.UpdatedAt = &value
	}
	return observation, true, nil
}

// observeGitHubCI stores the latest normalized state and appends one generated transition only when that
// entity's canonical state changes. githubLock serializes this with both webhook ingestion and polling, so
// either observation order produces the same transition sequence. The caller wakes matching inboxes.
func observeGitHubCI(ctx context.Context, tx *sql.Tx, appID, installationID int64, observation GitHubCIObservation, at time.Time) (bool, error) {
	observation, accepted, err := normalizeGitHubCIObservation(observation)
	if err != nil || !accepted {
		return false, err
	}
	entityKey := strconv.FormatInt(observation.ID, 10)
	if observation.Event == EventStatus {
		entityKey = observation.Name
	}
	payload, err := json.Marshal(githubCIEventPayload{GitHubCIObservation: observation, ObservedAt: at})
	if err != nil {
		return false, err
	}
	var previousID int64
	var previousUpdated sql.NullTime
	var sameState bool
	var sequence int64
	err = tx.QueryRowContext(ctx, `SELECT (payload->>'id')::bigint,
		NULLIF(payload->>'updatedAt', '')::timestamptz,
		jsonb_strip_nulls(payload - 'detailsUrl' - 'startedAt' - 'completedAt' - 'updatedAt' - 'observedAt') =
			jsonb_strip_nulls($7::jsonb - 'detailsUrl' - 'startedAt' - 'completedAt' - 'updatedAt' - 'observedAt'),
		last_sequence
		FROM github_ci_current
		WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3 AND head_sha = $4
			AND event = $5 AND entity_key = $6`,
		appID, installationID, observation.RepositoryID, observation.HeadSHA, observation.Event, entityKey, string(payload)).
		Scan(&previousID, &previousUpdated, &sameState, &sequence)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return false, err
	}
	found := err == nil
	if found {
		if observation.Event == EventStatus && observation.ID <= previousID {
			return false, nil
		}
		if observation.Event == EventCheckRun {
			if observation.UpdatedAt != nil && previousUpdated.Valid && observation.UpdatedAt.Before(previousUpdated.Time) {
				return false, nil
			}
		}
		if sameState {
			return false, nil
		}
	}
	changed := !found || !sameState
	if changed {
		if err := tx.QueryRowContext(ctx, `INSERT INTO github_ci_event
			(app_id, installation_id, repository_id, head_sha, event, payload)
			VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING sequence`,
			appID, installationID, observation.RepositoryID, observation.HeadSHA, observation.Event, string(payload)).Scan(&sequence); err != nil {
			return false, err
		}
	}
	_, err = tx.ExecContext(ctx, `INSERT INTO github_ci_current
		(app_id, installation_id, repository_id, head_sha, event, entity_key, last_sequence, payload)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
		ON CONFLICT (app_id, installation_id, repository_id, head_sha, event, entity_key) DO UPDATE SET
			last_sequence = EXCLUDED.last_sequence, payload = EXCLUDED.payload`,
		appID, installationID, observation.RepositoryID, observation.HeadSHA, observation.Event, entityKey,
		sequence, string(payload))
	return changed, err
}

func ensureGitHubCIRefresh(ctx context.Context, tx *sql.Tx, key GitHubCIRefreshKey) error {
	if err := key.validate(); err != nil {
		return err
	}
	_, err := tx.ExecContext(ctx, `INSERT INTO github_ci_refresh
		(app_id, installation_id, repository_id, subject_kind, subject_key, generation)
		VALUES ($1, $2, $3, 'pull_request', $4, 0) ON CONFLICT DO NOTHING`,
		key.AppID, key.InstallationID, key.Subject.RepositoryID, key.Subject.Key)
	return err
}

// githubCIParent reads the PR refresh generation and current head captured by a CI lease.
func githubCIParent(ctx context.Context, q querier, key GitHubCIRefreshKey) (int64, string, error) {
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

func invalidateGitHubCIForInstallation(ctx context.Context, tx *sql.Tx, appID, installationID int64) error {
	_, err := tx.ExecContext(ctx, `UPDATE github_ci_refresh SET generation = generation + 1, last_success_at = NULL,
		next_attempt = CASE WHEN error IS NOT NULL THEN next_attempt END, claim = NULL, claim_until = NULL
		WHERE app_id = $1 AND installation_id = $2`, appID, installationID)
	return err
}

func invalidateGitHubCIForPullRequest(ctx context.Context, tx *sql.Tx, repositoryID int64, subjectKey string, appID *int64) error {
	query := `UPDATE github_ci_refresh SET generation = generation + 1, last_success_at = NULL,
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

// ciFilterClause returns the SQL predicate for the selected CI event filters.
func ciFilterClause(alias string, filters []EventFilter, args *argList) string {
	selected := make([]string, 0, len(filters))
	for _, filter := range filters {
		if filter.Event != EventCheckRun && filter.Event != EventStatus {
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

// snapshotGitHubCI gives a new PR subscription the currently observed state for the revisions already known at
// its creation boundary. New transitions have larger sequences and remain independently replayable.
func snapshotGitHubCI(ctx context.Context, tx *sql.Tx, subscriptionID uuid.UUID, appID int64, subject GitHubSubjectKey, filters []EventFilter) error {
	args := argList{subscriptionID, appID, subject.RepositoryID, subject.Kind, subject.Key}
	selected := ciFilterClause("c", filters, &args)
	_, err := tx.ExecContext(ctx, `INSERT INTO github_ci_subscription_snapshot
		(subscription_id, app_id, installation_id, repository_id, event, sequence, payload)
		SELECT $1, c.app_id, c.installation_id, c.repository_id, c.event, c.last_sequence, c.payload
		FROM (
			SELECT DISTINCT ON (app_id, installation_id, repository_id, head_sha, event,
				CASE WHEN event = 'check_run' AND payload->>'checkAppId' IS NOT NULL THEN payload->>'checkAppId' END,
				CASE WHEN event = 'check_run' AND payload->>'checkAppId' IS NOT NULL THEN payload->>'name' ELSE entity_key END) *
			FROM github_ci_current WHERE app_id = $2
			ORDER BY app_id, installation_id, repository_id, head_sha, event,
				CASE WHEN event = 'check_run' AND payload->>'checkAppId' IS NOT NULL THEN payload->>'checkAppId' END,
				CASE WHEN event = 'check_run' AND payload->>'checkAppId' IS NOT NULL THEN payload->>'name' ELSE entity_key END,
				CASE WHEN event = 'check_run' THEN (payload->>'id')::bigint ELSE 0 END DESC
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

func ciFilters(source GitHubSource) []EventFilter {
	filters := source.Filters()
	selected := make([]EventFilter, 0, len(filters))
	for _, filter := range filters {
		if filter.Event == EventCheckRun || filter.Event == EventStatus {
			selected = append(selected, filter)
		}
	}
	return selected
}

// recordGitHubCI appends a new subscription's current-state snapshot and then transitions after its immutable
// creation boundary. Inbox copies and per-subscription event identities commit together, so replay after restart
// cannot lose or duplicate generated identities.
func (s *Store) recordGitHubCI(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, recording GitHubRecording) (int, bool, error) {
	if recording.Subject.Kind != "pull_request" {
		return 0, false, nil
	}
	var creation []byte
	var after int64
	if err := tx.QueryRowContext(ctx, `SELECT creation, github_ci_after_sequence FROM subscription WHERE id = $1`,
		recording.SubscriptionID).Scan(&creation, &after); err != nil {
		return 0, false, err
	}
	var body Subscribe
	if err := decode(creation, &body); err != nil {
		return 0, false, err
	}
	source, ok := body.Source.(GitHubSource)
	if !ok {
		return 0, false, errors.New("CI recording needs a GitHub subscription")
	}
	filters := ciFilters(source)
	if len(filters) == 0 {
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
		FROM github_ci_subscription_snapshot WHERE subscription_id = $1 AND (`+strings.Join(snapshotAccess, " OR ")+`)
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
		var e event
		if err := rows.Scan(&e.appID, &e.installation, &e.repositoryID, &e.event, &e.sequence, &e.payload); err != nil {
			rows.Close()
			return 0, false, err
		}
		snapshots = append(snapshots, e)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return 0, false, err
	}
	more := len(snapshots) > GitHubDeliveryPageSize
	if more {
		snapshots = snapshots[:GitHubDeliveryPageSize]
	}
	count := 0
	for _, e := range snapshots {
		identity := GitHubObservationEvent{AppID: e.appID, RepositoryID: e.repositoryID, Event: e.event, Sequence: e.sequence}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, e.payload); err != nil {
			return 0, false, fmt.Errorf("append GitHub CI snapshot: %w", err)
		}
		count++
		if _, err := tx.ExecContext(ctx, `DELETE FROM github_ci_subscription_snapshot
			WHERE subscription_id = $1 AND sequence = $2`, recording.SubscriptionID, e.sequence); err != nil {
			return 0, false, err
		}
	}
	if more {
		return count, true, nil
	}
	if count == GitHubDeliveryPageSize {
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
	selected := ciFilterClause("e", filters, &args)
	repository := args.add(recording.Subject.RepositoryID)
	kind := args.add(recording.Subject.Kind)
	key := args.add(recording.Subject.Key)
	sequence := args.add(after)
	limit := args.add(remaining + 1)
	query := `SELECT e.app_id, e.installation_id, e.repository_id, e.event, e.sequence, e.payload
		FROM github_ci_event e
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
		var e event
		if err := rows.Scan(&e.appID, &e.installation, &e.repositoryID, &e.event, &e.sequence, &e.payload); err != nil {
			rows.Close()
			return 0, false, err
		}
		events = append(events, e)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return 0, false, err
	}
	more = len(events) > remaining
	if more {
		events = events[:remaining]
	}
	for _, e := range events {
		identity := GitHubObservationEvent{AppID: e.appID, RepositoryID: e.repositoryID, Event: e.event, Sequence: e.sequence}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, e.payload); err != nil {
			return 0, false, fmt.Errorf("append GitHub CI event: %w", err)
		}
		count++
	}
	return count, more, nil
}

// A newer webhook observation fences reads already in flight. A settled refresh deadline stays put:
// ordinary CI activity must not make every subscriber poll the complete check set again.
func invalidateGitHubCIForRevision(ctx context.Context, tx *sql.Tx, appID, repositoryID int64, sha string) error {
	_, err := tx.ExecContext(ctx, `UPDATE github_ci_refresh c
  SET generation = generation + 1, claim = NULL, claim_until = NULL
  WHERE c.app_id = $1 AND c.claim IS NOT NULL AND EXISTS (
   SELECT 1 FROM github_subject_revision r JOIN github_pull_request p
    ON p.repository_id = r.repository_id AND p.number::text = r.subject_key
   WHERE r.repository_id = c.repository_id AND r.kind = c.subject_kind AND r.subject_key = c.subject_key
    AND r.head_repository_id = $2 AND r.sha = $3 AND p.head_sha = $3)`, appID, repositoryID, sha)
	return err
}
