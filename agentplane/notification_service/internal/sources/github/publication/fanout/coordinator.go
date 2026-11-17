// Package fanout matches committed GitHub facts and publishes them to inboxes.
package fanout

import (
	"context"
	"database/sql"
	"encoding/json"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/google/uuid"
)

type Coordinator struct {
	db      *sql.DB
	runner  *transaction.Runner
	inboxes *store.Store
}

func New(db *sql.DB, runner *transaction.Runner, inboxes *store.Store) *Coordinator {
	return &Coordinator{db: db, runner: runner, inboxes: inboxes}
}

func (c *Coordinator) append(ctx context.Context, tx transaction.Tx, inboxID, subscriptionID uuid.UUID,
	event sourceadapter.EventIdentity, payload json.RawMessage,
) error {
	input, err := sourceadapter.AppendInputFor(event, payload)
	if err != nil {
		return err
	}
	return c.inboxes.AppendTx(ctx, tx, inboxID, subscriptionID, input)
}

func now() time.Time { return time.Now().UTC().Truncate(time.Microsecond) }
