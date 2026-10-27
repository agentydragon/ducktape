package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/google/uuid"
)

// GitHubPullRequestState uses GitHub's PullRequestState vocabulary.
type GitHubPullRequestState string

const (
	GitHubPullRequestOpen   GitHubPullRequestState = "OPEN"
	GitHubPullRequestClosed GitHubPullRequestState = "CLOSED"
	GitHubPullRequestMerged GitHubPullRequestState = "MERGED"
)

// GitHubMergeableState uses GitHub's MergeableState vocabulary.
type GitHubMergeableState string

const (
	GitHubMergeable           GitHubMergeableState = "MERGEABLE"
	GitHubConflicting         GitHubMergeableState = "CONFLICTING"
	GitHubMergeabilityUnknown GitHubMergeableState = "UNKNOWN"
)

// GitHubPullRequestObservation is one API read of a PR and the revisions GitHub assessed.
type GitHubPullRequestObservation struct {
	BaseRef      string
	HeadSHA      string
	BaseSHA      string
	State        GitHubPullRequestState
	Mergeability GitHubMergeableState
}

// A pending computation is successful source contact, with an earlier durable repair deadline.
const mergeabilityRetry = 5 * time.Second

type mergeabilityPayload struct {
	Repository           string               `json:"repository"`
	PullRequestNumber    int64                `json:"pullRequestNumber"`
	BaseRef              string               `json:"baseRef"`
	HeadSHA              string               `json:"headSha"`
	BaseSHA              string               `json:"baseSha"`
	PreviousMergeability GitHubMergeableState `json:"previousMergeability"`
	Mergeability         GitHubMergeableState `json:"mergeability"`
	ObservedAt           time.Time            `json:"observedAt"`
}

// observePullRequest runs under the subject lease and access fences checked by Succeed. Its event and
// observation commit together; inbox fan-out can retry independently without losing a transition.
func (s *Store) observePullRequest(ctx context.Context, tx *sql.Tx, subject GitHubSubjectKey, success GitHubSuccess, at time.Time) error {
	if subject.Kind != "pull_request" || len(success.Fences) == 0 {
		return errors.New("a pull request observation needs its subject and repository access fence")
	}
	number, err := strconv.ParseInt(subject.Key, 10, 64)
	if err != nil {
		return err
	}
	access := success.Fences[0].Key
	if access.RepositoryID != subject.RepositoryID {
		return errors.New("pull request observation access does not match its repository")
	}
	observation := *success.PullRequest
	switch observation.State {
	case GitHubPullRequestOpen, GitHubPullRequestClosed, GitHubPullRequestMerged:
	default:
		return fmt.Errorf("invalid pull request state %q", observation.State)
	}
	switch observation.Mergeability {
	case GitHubMergeable, GitHubConflicting, GitHubMergeabilityUnknown:
	default:
		return fmt.Errorf("invalid pull request mergeability %q", observation.Mergeability)
	}
	previous := GitHubMergeabilityUnknown
	if err := tx.QueryRowContext(ctx, `SELECT last_settled_mergeability FROM github_pull_request WHERE repository_id = $1 AND number = $2`,
		subject.RepositoryID, number).Scan(&previous); err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	pending := observation.State == GitHubPullRequestOpen && observation.Mergeability == GitHubMergeabilityUnknown
	settled := previous
	if observation.State == GitHubPullRequestOpen && observation.Mergeability != GitHubMergeabilityUnknown {
		settled = observation.Mergeability
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO github_pull_request AS p (repository_id, number, base_ref, head_sha, base_sha, state, mergeability, last_settled_mergeability, observed_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
		ON CONFLICT (repository_id, number) DO UPDATE SET base_ref = EXCLUDED.base_ref,
			head_sha = EXCLUDED.head_sha, base_sha = EXCLUDED.base_sha, state = EXCLUDED.state,
			mergeability = EXCLUDED.mergeability, last_settled_mergeability = EXCLUDED.last_settled_mergeability,
			observed_at = EXCLUDED.observed_at,
			snapshot_version = p.snapshot_version + CASE WHEN
				(p.base_ref, p.head_sha, p.base_sha, p.state, p.mergeability) IS DISTINCT FROM
				(EXCLUDED.base_ref, EXCLUDED.head_sha, EXCLUDED.base_sha, EXCLUDED.state, EXCLUDED.mergeability)
				THEN 1 ELSE 0 END`,
		subject.RepositoryID, number, observation.BaseRef, observation.HeadSHA, observation.BaseSHA,
		observation.State, observation.Mergeability, settled, at); err != nil {
		return err
	}
	if pending {
		_, err := tx.ExecContext(ctx, `UPDATE github_subject SET next_attempt = $4
			WHERE repository_id = $1 AND kind = $2 AND subject_key = $3`,
			subject.RepositoryID, subject.Kind, subject.Key, at.Add(mergeabilityRetry))
		return err
	}
	if observation.State != GitHubPullRequestOpen || previous == settled || (previous == GitHubMergeabilityUnknown && settled == GitHubMergeable) {
		return nil
	}
	var repository string
	if err := tx.QueryRowContext(ctx, `SELECT full_name FROM github_repository WHERE repository_id = $1`, subject.RepositoryID).Scan(&repository); err != nil {
		return err
	}
	payload, err := json.Marshal(mergeabilityPayload{
		Repository: repository, PullRequestNumber: number, BaseRef: observation.BaseRef,
		HeadSHA: observation.HeadSHA, BaseSHA: observation.BaseSHA, PreviousMergeability: previous,
		Mergeability: settled, ObservedAt: at,
	})
	if err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `INSERT INTO github_mergeability_event
		(app_id, installation_id, repository_id, pull_request_number, payload) VALUES ($1, $2, $3, $4, $5::jsonb)`,
		access.AppID, access.InstallationID, subject.RepositoryID, number, string(payload)); err != nil {
		return err
	}
	if err := wakeGitHubInboxes(ctx, tx, access.AppID); err != nil {
		return err
	}
	return notify(ctx, tx)
}

// invalidateMergeability fences first observations and in-flight reads whose base may have changed.
// The saved base is authoritative only while no refresh is running; a retarget may race that read.
func invalidateMergeability(ctx context.Context, tx *sql.Tx, ingest GitHubIngest) error {
	for _, subject := range ingest.Subjects {
		switch {
		case ingest.Event == string(EventPullRequest) && subject.Kind == "pull_request":
			if _, err := tx.ExecContext(ctx, `UPDATE github_subject
				SET generation = generation + 1, last_success_at = NULL,
					next_attempt = CASE WHEN error IS NOT NULL THEN next_attempt END, claim = NULL, claim_until = NULL
				WHERE repository_id = $1 AND kind = $2 AND subject_key = $3`,
				subject.RepositoryID, subject.Kind, subject.Key); err != nil {
				return err
			}
		case subject.Kind == "branch" && (ingest.Event == string(EventPush) || ingest.Event == string(EventCreate) || ingest.Event == string(EventDelete)):
			if _, err := tx.ExecContext(ctx, `UPDATE github_subject s
				SET generation = generation + 1, last_success_at = NULL,
					next_attempt = CASE WHEN error IS NOT NULL THEN next_attempt END, claim = NULL, claim_until = NULL
				WHERE s.repository_id = $1 AND s.kind = 'pull_request' AND (
					s.claim IS NOT NULL
					OR NOT EXISTS (SELECT 1 FROM github_pull_request p WHERE p.repository_id = s.repository_id AND p.number::text = s.subject_key)
					OR EXISTS (SELECT 1 FROM github_pull_request p WHERE p.repository_id = s.repository_id
						AND p.number::text = s.subject_key AND p.state = 'OPEN' AND p.base_ref = $2))`,
				subject.RepositoryID, subject.Key); err != nil {
				return err
			}
		}
	}
	return nil
}

// recordMergeability runs under the claimed inbox lock and current access/subject fences in RecordGitHub.
// Each subscription has its own boundary, so a new subscriber can receive an already-observed conflict
// without replaying historical transitions or changing what existing subscribers receive.
func (s *Store) recordMergeability(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, recording GitHubRecording) (int, bool, error) {
	if recording.Subject.Kind != "pull_request" {
		return 0, false, nil
	}
	var creation []byte
	var after int64
	var initialized bool
	if err := tx.QueryRowContext(ctx, `SELECT creation, github_mergeability_after_sequence, github_mergeability_initialized
		FROM subscription WHERE id = $1`, recording.SubscriptionID).Scan(&creation, &after, &initialized); err != nil {
		return 0, false, err
	}
	var body Subscribe
	if err := decode(creation, &body); err != nil {
		return 0, false, err
	}
	source, ok := body.Source.(GitHubSource)
	if !ok {
		return 0, false, errors.New("mergeability recording needs a GitHub subscription")
	}
	selected := false
	for _, filter := range source.Filters() {
		if filter.Event == EventMergeabilityChanged {
			selected = true
		}
	}
	if !selected {
		return 0, false, nil
	}
	number, err := strconv.ParseInt(recording.Subject.Key, 10, 64)
	if err != nil {
		return 0, false, err
	}
	var version int64
	var state GitHubPullRequestState
	var snapshot mergeabilityPayload
	snapshot.PullRequestNumber = number
	snapshot.PreviousMergeability = GitHubMergeabilityUnknown
	err = tx.QueryRowContext(ctx, `SELECT p.snapshot_version, p.state, p.mergeability,
		p.base_ref, p.head_sha, p.base_sha, p.observed_at, r.full_name
		FROM github_pull_request p JOIN github_repository r USING (repository_id)
		WHERE p.repository_id = $1 AND p.number = $2`, recording.Subject.RepositoryID, number).
		Scan(&version, &state, &snapshot.Mergeability, &snapshot.BaseRef, &snapshot.HeadSHA,
			&snapshot.BaseSHA, &snapshot.ObservedAt, &snapshot.Repository)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	var access *GitHubAccessKey
	for _, fence := range recording.Fences {
		if fence.Key.RepositoryID == recording.Subject.RepositoryID {
			key := fence.Key
			access = &key
			break
		}
	}
	if access == nil {
		return 0, false, errors.New("mergeability recording needs its repository access")
	}
	rows, err := tx.QueryContext(ctx, `SELECT sequence, payload FROM github_mergeability_event
		WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3 AND pull_request_number = $4
			AND sequence > $5 ORDER BY sequence LIMIT $6`,
		access.AppID, access.InstallationID, recording.Subject.RepositoryID, number, after, GitHubDeliveryPageSize)
	if err != nil {
		return 0, false, err
	}
	type event struct {
		sequence int64
		payload  json.RawMessage
	}
	var events []event
	for rows.Next() {
		var e event
		if err := rows.Scan(&e.sequence, &e.payload); err != nil {
			rows.Close()
			return 0, false, err
		}
		events = append(events, e)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return 0, false, err
	}
	for _, e := range events {
		identity := GitHubMergeabilityEvent{AppID: access.AppID, RepositoryID: access.RepositoryID, PullRequestNumber: number, Sequence: e.sequence}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, e.payload); err != nil {
			return 0, false, fmt.Errorf("append mergeability event: %w", err)
		}
		after = max(after, e.sequence)
	}
	count := len(events)
	if !initialized && count == 0 && state == GitHubPullRequestOpen && snapshot.Mergeability == GitHubConflicting {
		payload, err := json.Marshal(snapshot)
		if err != nil {
			return 0, false, err
		}
		identity := GitHubMergeabilitySnapshotEvent{
			AppID: access.AppID, RepositoryID: access.RepositoryID, PullRequestNumber: number, Version: version,
		}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, payload); err != nil {
			return 0, false, fmt.Errorf("append mergeability snapshot: %w", err)
		}
		count++
	}
	initialized = initialized || count > 0 || state != GitHubPullRequestOpen || snapshot.Mergeability != GitHubMergeabilityUnknown
	_, err = tx.ExecContext(ctx, `UPDATE subscription SET github_mergeability_after_sequence = $2,
		github_mergeability_initialized = $3 WHERE id = $1`, recording.SubscriptionID, after, initialized)
	return count, len(events) == GitHubDeliveryPageSize, err
}
