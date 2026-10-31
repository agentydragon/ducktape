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

// Up applies this database's SQL migrations with Goose before the service starts serving traffic.
func Up(ctx context.Context, db *sql.DB) error {
	// Retain the advisory lock key shared with the retired Alembic migration path.
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
