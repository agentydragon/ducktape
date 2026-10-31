// Package migrations owns Goose migrations for the egress decision-history database.
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
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(0x4547_5244))
	if err != nil {
		return err
	}
	files, err := fs.Sub(scripts, "history")
	if err != nil {
		return err
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("egress_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true))
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}
