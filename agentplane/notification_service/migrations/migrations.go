// Package migrations owns Goose migrations for the Notification Service's notifications database.
package migrations

import (
	"context"
	"database/sql"
	"embed"
	"io/fs"

	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

//go:embed notifications/*.sql
var scripts embed.FS

// Up applies schema changes before the service starts. Goose serializes concurrent service starts with a session lock.
func Up(ctx context.Context, db *sql.DB) error {
	// Retain the advisory lock key shared with the previous Goose and Alembic migration paths.
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(0x4E4F_5446))
	if err != nil {
		return err
	}
	files, err := fs.Sub(scripts, "notifications")
	if err != nil {
		return err
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("notification_service_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true))
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}
