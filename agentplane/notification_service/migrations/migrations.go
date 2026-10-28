// Package migrations owns Goose migrations for the Notification Service's `notifications` database.
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

// Up applies authored SQL with Goose in a dedicated init container, never while serving traffic.
func Up(ctx context.Context, db *sql.DB) error {
	provider, err := newProvider(db)
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}

// UpTo applies authored SQL through version in the same provider used by Up. Integration tests use it to build a
// predecessor schema and exercise an upgrade against populated rows.
func UpTo(ctx context.Context, db *sql.DB, version int64) error {
	provider, err := newProvider(db)
	if err != nil {
		return err
	}
	_, err = provider.UpTo(ctx, version)
	return err
}

func newProvider(db *sql.DB) (*goose.Provider, error) {
	// The advisory lock key the retired Alembic runner took, kept so that no two migrators run at once.
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(0x4E4F_5446))
	if err != nil {
		return nil, err
	}
	files, err := fs.Sub(scripts, "notifications")
	if err != nil {
		return nil, err
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("notification_service_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true))
	if err != nil {
		return nil, err
	}
	return provider, nil
}
