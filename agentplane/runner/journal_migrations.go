package runner

import (
	"context"
	"database/sql"
	"embed"
	"io/fs"
	"log/slog"

	"github.com/pressly/goose/v3"
)

const journalMigrationTableName = "runner_goose_version"

//go:embed journal_migrations/*.sql
var journalMigrationFiles embed.FS

func migrateJournalSchema(ctx context.Context, db *sql.DB) error {
	files, err := fs.Sub(journalMigrationFiles, "journal_migrations")
	if err != nil {
		return err
	}
	provider, err := goose.NewProvider(
		goose.DialectSQLite3, db, files,
		goose.WithTableName(journalMigrationTableName),
		goose.WithDisableGlobalRegistry(true),
		goose.WithSlog(slog.Default()),
	)
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}
