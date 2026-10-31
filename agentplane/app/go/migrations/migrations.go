package migrations

import (
	"context"
	"database/sql"
	"embed"
	"io/fs"

	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

//go:embed app/*.sql
var scripts embed.FS

// Keep the key used by the app's previous migrator when serializing Goose runs.
const migrationLockID int64 = 0x5452_414A

// Up applies the app schema before this process starts serving requests or running workers.
func Up(ctx context.Context, db *sql.DB) error {
	provider, err := newProvider(db)
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}

func newProvider(db *sql.DB) (*goose.Provider, error) {
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(migrationLockID))
	if err != nil {
		return nil, err
	}
	files, err := fs.Sub(scripts, "app")
	if err != nil {
		return nil, err
	}
	return goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("agentplane_app_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true))
}
