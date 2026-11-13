package centralsampler

import (
	"context"
	"database/sql"
	"embed"

	"github.com/agentydragon/ducktape/agentplane/internal/samplerauditmigration"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/pressly/goose/v3"
)

//go:embed schema/*.sql
var migrationFiles embed.FS

var schema = migrate.Schema{
	Files: migrationFiles, Dir: "schema", Table: "central_sampler_goose_version", LockID: 7241821,
	GoMigrations: []*goose.Migration{goose.NewGoMigration(14,
		&goose.GoFunc{RunDB: samplerauditmigration.Sampler},
		&goose.GoFunc{RunTx: samplerauditmigration.Down})},
}

// Migrate applies the sampler-owned Goose migrations.
func Migrate(ctx context.Context, db *sql.DB) error { return schema.Up(ctx, db) }
