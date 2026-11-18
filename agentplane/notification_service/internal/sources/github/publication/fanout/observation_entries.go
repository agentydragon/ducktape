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
	args := argList{subscriptionID}
	access := make([]string, 0, len(fences))
	for _, fence := range fences {
		access = append(access, "(app_id = "+args.add(fence.Key.AppID)+" AND installation_id = "+
			args.add(fence.Key.InstallationID)+" AND repository_id = "+args.add(fence.Key.RepositoryID)+")")
	}
	if len(access) == 0 {
		return 0, false, nil
	}
	rows, err := tx.QueryContext(ctx, `SELECT app_id, installation_id, repository_id, event, sequence, payload
		FROM `+table+` WHERE subscription_id = $1 AND (`+strings.Join(access, " OR ")+`)
		ORDER BY sequence LIMIT `+args.add(githubmodel.DeliveryPageSize+1), args...)
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

type observationAccessOrder bool

const (
	installationBeforeRepository observationAccessOrder = false
	repositoryBeforeInstallation observationAccessOrder = true
)

// observationAccessClause binds each event's full access tuple in the query's parameter order.
func observationAccessClause(args *argList, fences []githubstate.AccessFence, order observationAccessOrder) string {
	clauses := make([]string, len(fences))
	for i, fence := range fences {
		key := fence.Key
		firstColumn, secondColumn := "installation_id", "repository_id"
		firstID, secondID := key.InstallationID, key.RepositoryID
		if order == repositoryBeforeInstallation {
			firstColumn, secondColumn = secondColumn, firstColumn
			firstID, secondID = secondID, firstID
		}
		clauses[i] = fmt.Sprintf("(e.app_id = %s AND e.%s = %s AND e.%s = %s)",
			args.add(key.AppID), firstColumn, args.add(firstID), secondColumn, args.add(secondID))
	}
	return strings.Join(clauses, " OR ")
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
