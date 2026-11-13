// Package migrations owns Goose migrations for the History Service's `sandbox_service` database.
package migrations

import (
	"context"
	"database/sql"
	"embed"

	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
)

//go:embed history/*.sql
var scripts embed.FS

var schema = migrate.Schema{Files: scripts, Dir: "history", Table: "history_service_goose_version", LockID: 0x4849_5354}

// Up applies the service-owned migrations before runtime startup.
func Up(ctx context.Context, db *sql.DB) error { return schema.Up(ctx, db) }
