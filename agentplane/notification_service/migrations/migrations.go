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

// Up applies schema changes before this Pod serves. Recreate stops old Notification writers,
// and Goose serializes concurrent init containers with a session lock.
func Up(ctx context.Context, db *sql.DB) error {
	// Keep the lock key used by previous Goose and Alembic migrators.
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
