package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

func commitObservationClause(alias string, selection model.GitHubCommitObservationSelection, args *argList) string {
	var selected []string
	if selection.CheckRuns {
		part := alias + ".event = " + args.add(string(model.EventCheckRun))
		if selection.CompletedCheckRunsOnly {
			part += " AND " + alias + ".payload->>'status' = " + args.add(string(model.CheckRunCompleted))
		}
		selected = append(selected, "("+part+")")
	}
	if selection.CommitStatuses {
		selected = append(selected, "("+alias+".event = "+args.add(string(model.EventStatus))+")")
	}
	if len(selected) == 0 {
		return "FALSE"
	}
	return "(" + strings.Join(selected, " OR ") + ")"
}

// snapshotGitHubCommitObservations stores the current native state for current PR-head revisions at the
// subscription's immutable creation boundary. New transitions have larger sequences and remain replayable.
func snapshotGitHubCommitObservations(ctx context.Context, tx *sql.Tx, subscriptionID uuid.UUID, appID int64,
	subject githubstate.SubjectKey, source model.GitHubSource,
) error {
	selection := source.CommitObservationSelection()
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
	var creationProto sql.Null[[]byte]
	var sourceProvider sql.NullString
	var idempotencyKey string
	var after int64
	if err := tx.QueryRowContext(ctx, `SELECT creation_proto, source_provider, idempotency_key, github_commit_after_sequence
		FROM subscription WHERE id = $1`, recording.SubscriptionID).
		Scan(&creationProto, &sourceProvider, &idempotencyKey, &after); err != nil {
		return 0, false, err
	}
	body, err := storagecodec.DecodeStoredSubscriptionCreation(recording.SubscriptionID, creationProto, sourceProvider, idempotencyKey)
	if err != nil {
		return 0, false, err
	}
	source, ok := body.Source.(model.GitHubSource)
	if !ok {
		return 0, false, errors.New("commit observation recording needs a GitHub subscription")
	}
	selection := source.CommitObservationSelection()
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
		event        model.EventName
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
		identity := model.GitHubObservationEvent{AppID: item.appID, RepositoryID: item.repositoryID, Event: item.event, Sequence: item.sequence}
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
				WHERE m.subscription_id = ` + args.add(recording.SubscriptionID) + `
					AND existing.identity_provider = 'github_observation'
					AND existing.github_app_id = e.app_id AND existing.github_repository_id = e.repository_id
					AND existing.github_event = e.event AND existing.github_observation_sequence = e.sequence)
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
		identity := model.GitHubObservationEvent{AppID: item.appID, RepositoryID: item.repositoryID, Event: item.event, Sequence: item.sequence}
		if err := s.AppendEvent(ctx, tx, inboxID, recording.SubscriptionID, identity, item.payload); err != nil {
			return 0, false, fmt.Errorf("append GitHub commit observation: %w", err)
		}
		count++
	}
	return count, more, nil
}
