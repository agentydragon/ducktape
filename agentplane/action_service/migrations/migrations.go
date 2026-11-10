// Package migrations owns Goose migrations for the Action Service's `actions` database.
package migrations

import (
	"context"
	"database/sql"
	"embed"

	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
)

//go:embed actions/*.sql
var scripts embed.FS

var schema = migrate.Schema{
	Files: scripts, Dir: "actions", Table: "action_service_goose_version",
	// Retain the advisory lock key shared with the retired Alembic migration path.
	LockID: 0x4143_544E,
}

// Up applies this database's SQL migrations with Goose before the service starts serving traffic.
func Up(ctx context.Context, db *sql.DB) error { return schema.Up(ctx, db) }
