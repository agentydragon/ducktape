// Package migrations owns Goose migrations for the Dispatcher database.
package migrations

import (
	"context"
	"database/sql"
	"embed"

	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
)

//go:embed dispatcher/*.sql
var scripts embed.FS

var schema = migrate.Schema{
	Files: scripts, Dir: "dispatcher", Table: "runner_gateway_dispatcher_goose_version", LockID: 0x4453_5043,
}

// Up applies the service-owned migrations before runtime startup.
func Up(ctx context.Context, db *sql.DB) error { return schema.Up(ctx, db) }
