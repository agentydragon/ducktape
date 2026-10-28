// Package migrations owns Goose migrations for the Action Service's `actions` database.
package migrations

import (
	"context"
	"database/sql"
	"embed"
	"io/fs"

	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

//go:embed actions/*.sql
var scripts embed.FS

// Up applies SQL migrations with Goose in a dedicated init container, never while serving traffic.
func Up(ctx context.Context, db *sql.DB) error {
	// The advisory lock key of the Alembic runner (`database_migrate.py`): the two migrators never run at once.
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(0x4143_544E))
	if err != nil {
		return err
	}
	files, err := fs.Sub(scripts, "actions")
	if err != nil {
		return err
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("action_service_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true))
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}
