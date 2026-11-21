package fanout

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	githubstoragecodec "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/storagecodec"

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/google/uuid"
)

// recordMergeability runs under the claimed inbox lock and current access and subject fences.
// Each subscription has its own boundary, so a new subscriber can receive an already-observed conflict
// without replaying historical transitions or changing what existing subscribers receive.
func (c *Coordinator) recordMergeability(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID, subscriptionID uuid.UUID, subject githubstate.SubjectKey, fences []githubstate.AccessFence) (int, bool, error) {
	if subject.Kind != "pull_request" {
		return 0, false, nil
	}
	var after int64
	var initialized bool
	source, err := githubSourceOf(ctx, tx, subscriptionID,
		"github_mergeability_after_sequence, github_mergeability_initialized", &after, &initialized)
	if err != nil {
		return 0, false, err
	}
	selected := false
	for _, filter := range source.Filters() {
		if filter.Event == githubmodel.EventMergeabilityChanged {
			selected = true
		}
	}
	if !selected {
		return 0, false, nil
	}
	number, err := strconv.ParseInt(subject.Key, 10, 64)
	if err != nil {
		return 0, false, err
	}
	var version int64
	var state githubmodel.PullRequestState
	var snapshot githubstoragecodec.MergeabilityPayload
	snapshot.PullRequestNumber = number
	snapshot.PreviousMergeability = githubmodel.MergeabilityUnknown
	err = tx.QueryRowContext(ctx, `SELECT p.snapshot_version, p.state, p.mergeability,
		p.base_ref, p.head_sha, p.base_sha, p.observed_at, r.full_name
		FROM github_pull_request p JOIN github_repository r USING (repository_id)
		WHERE p.repository_id = $1 AND p.number = $2`, subject.RepositoryID, number).
		Scan(&version, &state, &snapshot.Mergeability, &snapshot.BaseRef, &snapshot.HeadSHA,
			&snapshot.BaseSHA, &snapshot.ObservedAt, &snapshot.Repository)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	var access *githubstate.AccessKey
	for _, fence := range fences {
		if fence.Key.RepositoryID == subject.RepositoryID {
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
		access.AppID, access.InstallationID, subject.RepositoryID, number, after, githubmodel.DeliveryPageSize)
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
		identity := sourceadapter.WrapGitHubMergeabilityEvent(githubmodel.MergeabilityEvent{AppID: access.AppID, RepositoryID: access.RepositoryID, PullRequestNumber: number, Sequence: e.sequence})
		input, err := sourceadapter.AppendInputFor(identity, e.payload)
		if err != nil {
			return 0, false, err
		}
		if err := c.inboxes.AppendTx(ctx, tx, inboxID, subscriptionID, input); err != nil {
			return 0, false, fmt.Errorf("append mergeability event: %w", err)
		}
		after = max(after, e.sequence)
	}
	count := len(events)
	if !initialized && count == 0 && state == githubmodel.PullRequestOpen && snapshot.Mergeability == githubmodel.Conflicting {
		payload, err := json.Marshal(snapshot)
		if err != nil {
			return 0, false, err
		}
		identity := sourceadapter.WrapGitHubMergeabilitySnapshotEvent(githubmodel.MergeabilitySnapshotEvent{
			AppID: access.AppID, RepositoryID: access.RepositoryID, PullRequestNumber: number, Version: version,
		})
		input, err := sourceadapter.AppendInputFor(identity, payload)
		if err != nil {
			return 0, false, err
		}
		if err := c.inboxes.AppendTx(ctx, tx, inboxID, subscriptionID, input); err != nil {
			return 0, false, fmt.Errorf("append mergeability snapshot: %w", err)
		}
		count++
	}
	initialized = initialized || count > 0 || state != githubmodel.PullRequestOpen || snapshot.Mergeability != githubmodel.MergeabilityUnknown
	_, err = tx.ExecContext(ctx, `UPDATE subscription SET github_mergeability_after_sequence = $2,
		github_mergeability_initialized = $3 WHERE id = $1`, subscriptionID, after, initialized)
	return count, len(events) == githubmodel.DeliveryPageSize, err
}
