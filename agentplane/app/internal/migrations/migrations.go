package migrations

import (
	"context"
	"database/sql"
	"embed"

	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
)

//go:embed app/*.sql
var scripts embed.FS

var schema = migrate.Schema{
	Files: scripts, Dir: "app", Table: "agentplane_app_goose_version",
	// Keep the key used by the app's previous migrator when serializing Goose runs.
	LockID: 0x5452_414A,
}

// Up applies the app schema before this process starts serving requests or running workers.
func Up(ctx context.Context, db *sql.DB) error { return schema.Up(ctx, db) }
