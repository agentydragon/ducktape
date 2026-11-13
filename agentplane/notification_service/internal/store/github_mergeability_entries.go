package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"time"

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// recordMergeability runs under the claimed inbox lock and current access/subject fences in RecordGitHub.
// Each subscription has its own boundary, so a new subscriber can receive an already-observed conflict
// without replaying historical transitions or changing what existing subscribers receive.
func (s *Store) recordMergeability(ctx context.Context, tx *sql.Tx, inboxID uuid.UUID, recording GitHubRecording) (int, bool, error) {
	if recording.Subject.Kind != "pull_request" {
		return 0, false, nil
	}
	var creationProto sql.Null[[]byte]
	var sourceProvider sql.NullString
	var idempotencyKey string
	var after int64
	var initialized bool
	if err := tx.QueryRowContext(ctx, `SELECT creation_proto, source_provider, idempotency_key,
		github_mergeability_after_sequence, github_mergeability_initialized
		FROM subscription WHERE id = $1`, recording.SubscriptionID).
		Scan(&creationProto, &sourceProvider, &idempotencyKey, &after, &initialized); err != nil {
		return 0, false, err
	}
	body, err := storagecodec.DecodeStoredSubscriptionCreation(recording.SubscriptionID, creationProto, sourceProvider, idempotencyKey)
	if err != nil {
		return 0, false, err
	}
	source, ok := body.Source.(model.GitHubSource)
	if !ok {
		return 0, false, errors.New("mergeability recording needs a GitHub subscription")
	}
	selected := false
	for _, filter := range source.Filters() {
		if filter.Event == model.EventMergeabilityChanged {
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
	var state model.GitHubPullRequestState
	var snapshot storagecodec.MergeabilityPayload
	snapshot.PullRequestNumber = number
	snapshot.PreviousMergeability = model.GitHubMergeabilityUnknown
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
	var access *githubstate.AccessKey
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
		identity := model.GitHubMergeabilityEvent{AppID: access.AppID, RepositoryID: access.RepositoryID, PullRequestNumber: number, Sequence: e.sequence}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, e.payload); err != nil {
			return 0, false, fmt.Errorf("append mergeability event: %w", err)
		}
		after = max(after, e.sequence)
	}
	count := len(events)
	if !initialized && count == 0 && state == model.GitHubPullRequestOpen && snapshot.Mergeability == model.GitHubConflicting {
		payload, err := json.Marshal(snapshot)
		if err != nil {
			return 0, false, err
		}
		identity := model.GitHubMergeabilitySnapshotEvent{
			AppID: access.AppID, RepositoryID: access.RepositoryID, PullRequestNumber: number, Version: version,
		}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, payload); err != nil {
			return 0, false, fmt.Errorf("append mergeability snapshot: %w", err)
		}
		count++
	}
	initialized = initialized || count > 0 || state != model.GitHubPullRequestOpen || snapshot.Mergeability != model.GitHubMergeabilityUnknown
	_, err = tx.ExecContext(ctx, `UPDATE subscription SET github_mergeability_after_sequence = $2,
		github_mergeability_initialized = $3 WHERE id = $1`, recording.SubscriptionID, after, initialized)
	return count, len(events) == GitHubDeliveryPageSize, err
}
