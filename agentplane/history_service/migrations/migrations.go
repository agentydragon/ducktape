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

// Up applies history schema migrations with Goose before this Pod serves; older replicas may still write.
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
	backfill := goose.NewGoMigration(
		4,
		&goose.GoFunc{RunDB: backfillEventKinds},
		&goose.GoFunc{RunDB: rollbackEventKindBackfill},
	)
	return goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("history_service_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true), goose.WithGoMigrations(backfill))
}
