package centralsampler

import (
	"context"
	"database/sql"
	"embed"

	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
)

//go:embed schema/*.sql
var migrationFiles embed.FS

var schema = migrate.Schema{Files: migrationFiles, Dir: "schema", Table: "central_sampler_goose_version", LockID: 7241821}

// Migrate applies the sampler-owned Goose SQL migrations.
func Migrate(ctx context.Context, db *sql.DB) error { return schema.Up(ctx, db) }
