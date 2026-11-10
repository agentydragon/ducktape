package journal

import (
	"context"
	"database/sql"
	"embed"
	"io/fs"

	"github.com/pressly/goose/v3"
	"go.uber.org/zap"
)

const journalMigrationTableName = "runner_goose_version"

type gooseZapLogger struct{ logger *zap.SugaredLogger }

func (l gooseZapLogger) Printf(format string, args ...any) { l.logger.Infof(format, args...) }
func (l gooseZapLogger) Fatalf(format string, args ...any) { l.logger.Fatalf(format, args...) }

//go:embed migrations/*.sql
var journalMigrationFiles embed.FS

func migrateJournalSchema(ctx context.Context, db *sql.DB) error {
	files, err := fs.Sub(journalMigrationFiles, "migrations")
	if err != nil {
		return err
	}
	provider, err := goose.NewProvider(
		goose.DialectSQLite3, db, files,
		goose.WithTableName(journalMigrationTableName),
		goose.WithDisableGlobalRegistry(true),
		goose.WithLogger(gooseZapLogger{logger: zap.L().Sugar()}),
	)
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}
