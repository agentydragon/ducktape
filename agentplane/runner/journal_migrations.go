package runner

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
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
		goose.WithGoMigrations(goose.NewGoMigration(2, &goose.GoFunc{RunTx: migrateCommandJournalData}, nil)),
		goose.WithDisableGlobalRegistry(true),
		goose.WithSlog(slog.Default()),
	)
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}

// migrateCommandJournalData wraps old flat correlation maps in the versioned command record.
// The original map remains available to adapter recovery code during the compatibility window.
func migrateCommandJournalData(ctx context.Context, tx *sql.Tx) error {
	rows, err := tx.QueryContext(ctx, "SELECT command_id, native_correlation FROM command")
	if err != nil {
		return err
	}
	type update struct {
		commandID string
		encoded   string
	}
	var updates []update
	for rows.Next() {
		var commandID string
		var raw string
		if err := rows.Scan(&commandID, &raw); err != nil {
			_ = rows.Close()
			return err
		}
		data, legacy, err := decodeCommandJournalData(raw)
		if err != nil {
			_ = rows.Close()
			return fmt.Errorf("command %q: %w", commandID, err)
		}
		if legacy {
			encoded, err := encodeCommandJournalData(data)
			if err != nil {
				_ = rows.Close()
				return err
			}
			updates = append(updates, update{commandID: commandID, encoded: encoded})
		}
	}
	if err := rows.Err(); err != nil {
		_ = rows.Close()
		return err
	}
	if err := rows.Close(); err != nil {
		return err
	}
	for _, item := range updates {
		if _, err := tx.ExecContext(ctx, "UPDATE command SET native_correlation = ? WHERE command_id = ?", item.encoded, item.commandID); err != nil {
			return err
		}
	}
	return nil
}
