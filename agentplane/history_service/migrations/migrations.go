// Package migrations owns Goose migrations for the History Service's `sandbox_service` database.
package migrations

import (
	"context"
	"database/sql"
	"embed"

	"github.com/agentydragon/ducktape/agentplane/internal/samplerauditmigration"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/pressly/goose/v3"
)

//go:embed history/*.sql
var scripts embed.FS

var schema = migrate.Schema{
	Files: scripts, Dir: "history", Table: "history_service_goose_version", LockID: 0x4849_5354,
	GoMigrations: []*goose.Migration{goose.NewGoMigration(17,
		&goose.GoFunc{RunDB: samplerauditmigration.History},
		&goose.GoFunc{RunTx: samplerauditmigration.Down})},
}

// Up applies the service-owned migrations before runtime startup.
func Up(ctx context.Context, db *sql.DB) error { return schema.Up(ctx, db) }
