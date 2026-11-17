package fanout

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model"
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state"
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
