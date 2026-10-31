// Package migrations owns Goose migrations for the Dispatcher database.
package migrations

import (
	"context"
	"database/sql"
	"embed"
	"io/fs"

	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

//go:embed dispatcher/*.sql
var scripts embed.FS

// Up applies authored SQL with Goose before this Gateway Pod serves; older Pods may still write.
func Up(ctx context.Context, db *sql.DB) error {
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(0x4453_5043))
	if err != nil {
		return err
	}
	files, err := fs.Sub(scripts, "dispatcher")
	if err != nil {
		return err
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("runner_gateway_dispatcher_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true))
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}
