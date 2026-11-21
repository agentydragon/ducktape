package fanout

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/google/uuid"
)

// recordObservationSnapshots consumes a page in the caller's recording transaction, so inbox
// copies and snapshot deletion commit together. table is a literal snapshot table name at each call site.
// A full page leaves transition replay for the next attempt, even without a lookahead row.
func (c *Coordinator) recordObservationSnapshots(ctx context.Context, tx transaction.Tx, inboxID uuid.UUID,
	subscriptionID uuid.UUID, fences []githubstate.AccessFence, table string,
) (int, bool, error) {
	if len(fences) == 0 {
		return 0, false, nil
	}
	args := argList{subscriptionID}
	accessible := observationAccessClause(&args, fences)
	limit := args.add(githubmodel.DeliveryPageSize + 1)
	rows, err := tx.QueryContext(ctx, `SELECT e.app_id, e.installation_id, e.repository_id, e.event, e.sequence, e.payload
		FROM `+table+` e WHERE e.subscription_id = $1 AND (`+accessible+`)
		ORDER BY e.sequence LIMIT `+limit, args...)
	if err != nil {
		return 0, false, err
	}
	snapshots, err := readGitHubObservations(rows)
	if err != nil {
		return 0, false, err
	}
	if len(snapshots) > githubmodel.DeliveryPageSize {
		snapshots = snapshots[:githubmodel.DeliveryPageSize]
	}
	for _, item := range snapshots {
		input, err := sourceadapter.AppendInputFor(item.identity, item.payload)
		if err != nil {
			return 0, false, err
		}
		if err := c.inboxes.AppendTx(ctx, tx, inboxID, subscriptionID, input); err != nil {
			return 0, false, fmt.Errorf("append GitHub observation snapshot from %s: %w", table, err)
		}
		if _, err := tx.ExecContext(ctx, `DELETE FROM `+table+` WHERE subscription_id = $1 AND sequence = $2`,
			subscriptionID, item.identity.Sequence); err != nil {
			return 0, false, err
		}
	}
	return len(snapshots), len(snapshots) == githubmodel.DeliveryPageSize, nil
}

// observationAccessClause matches an observation row aliased e by the full access tuple of any of the fences.
func observationAccessClause(args *argList, fences []githubstate.AccessFence) string {
	clauses := make([]string, len(fences))
	for i, fence := range fences {
		key := fence.Key
		clauses[i] = fmt.Sprintf("(e.app_id = %s AND e.installation_id = %s AND e.repository_id = %s)",
			args.add(key.AppID), args.add(key.InstallationID), args.add(key.RepositoryID))
	}
	return strings.Join(clauses, " OR ")
}

// notRecorded is the condition that no entry the subscription matched in the inbox already has the identity of the
// observation event aliased e.
func notRecorded(args *argList, inboxID, subscriptionID uuid.UUID) string {
	return `NOT EXISTS (SELECT 1 FROM subscription_match m JOIN entry existing
			ON existing.inbox_id = ` + args.add(inboxID) + ` AND existing.cursor = m.cursor
			WHERE m.subscription_id = ` + args.add(subscriptionID) + `
				AND existing.identity_provider = 'github_observation'
				AND existing.github_app_id = e.app_id AND existing.github_repository_id = e.repository_id
				AND existing.github_event = e.event AND existing.github_observation_sequence = e.sequence)`
}

// githubSourceOf reads the GitHub source a subscription was created with, and into dests the further columns of its
// row that the caller names.
func githubSourceOf(ctx context.Context, tx transaction.Tx, subscriptionID uuid.UUID, columns string, dests ...any) (sourceadapter.GitHubSource, error) {
	var creationProto []byte
	var sourceProvider, idempotencyKey string
	if err := tx.QueryRowContext(ctx, `SELECT creation_proto, source_provider, idempotency_key, `+columns+`
		FROM subscription WHERE id = $1`, subscriptionID).
		Scan(append([]any{&creationProto, &sourceProvider, &idempotencyKey}, dests...)...); err != nil {
		return sourceadapter.GitHubSource{}, err
	}
	body, err := sourceadapter.DecodeStoredSubscriptionCreation(subscriptionID, creationProto, sourceProvider, idempotencyKey)
	if err != nil {
		return sourceadapter.GitHubSource{}, err
	}
	source, ok := body.Source.(sourceadapter.GitHubSource)
	if !ok {
		return sourceadapter.GitHubSource{}, fmt.Errorf("subscription %s is not a GitHub subscription", subscriptionID)
	}
	return source, nil
}

func (c *Coordinator) appendObservationPage(ctx context.Context, tx transaction.Tx, inboxID, subscriptionID uuid.UUID,
	rows *sql.Rows, snapshotCount int, operation string,
) (int, bool, error) {
	events, err := readGitHubObservations(rows)
	if err != nil {
		return 0, false, err
	}
	remaining := githubmodel.DeliveryPageSize - snapshotCount
	more := len(events) > remaining
	events = events[:min(len(events), remaining)]
	for _, event := range events {
		input, err := sourceadapter.AppendInputFor(event.identity, event.payload)
		if err != nil {
			return 0, false, err
		}
		if err := c.inboxes.AppendTx(ctx, tx, inboxID, subscriptionID, input); err != nil {
			return 0, false, fmt.Errorf("%s: %w", operation, err)
		}
	}
	return snapshotCount + len(events), more, nil
}

// readGitHubObservations drains and closes rows before any appends use the transaction's connection.
func readGitHubObservations(rows *sql.Rows) ([]githubObservation, error) {
	var observations []githubObservation
	for rows.Next() {
		var item githubObservation
		var installation int64
		if err := rows.Scan(&item.identity.AppID, &installation, &item.identity.RepositoryID,
			&item.identity.Event, &item.identity.Sequence, &item.payload); err != nil {
			rows.Close()
			return nil, err
		}
		observations = append(observations, item)
	}
	return observations, errors.Join(rows.Err(), rows.Close())
}

type githubObservation struct {
	identity sourceadapter.GitHubObservationEvent
	payload  json.RawMessage
}
