// Package migrations owns Goose migrations for the History Service's `sandbox_service` database.
package migrations

import (
	"context"
	"database/sql"
	"embed"
	"io/fs"

	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

//go:embed history/*.sql
var scripts embed.FS

// Up applies the service-owned migrations before runtime startup.
func Up(ctx context.Context, db *sql.DB) error {
	provider, err := newProvider(db)
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}

func newProvider(db *sql.DB) (*goose.Provider, error) {
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(0x4849_5354))
	if err != nil {
		return nil, err
	}
	files, err := fs.Sub(scripts, "history")
	if err != nil {
		return nil, err
	}
	return goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("history_service_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true))
}
