// Package migrations owns Goose migrations for the MCP edge's `mcp_edge` database.
package migrations

import (
	"context"
	"database/sql"
	"embed"
	"io/fs"

	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

//go:embed mcp_edge/*.sql
var scripts embed.FS

// Up applies authored SQL with Goose before this Pod serves; the lock does not stop other writers.
func Up(ctx context.Context, db *sql.DB) error {
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(0x4D43_5045))
	if err != nil {
		return err
	}
	files, err := fs.Sub(scripts, "mcp_edge")
	if err != nil {
		return err
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("mcp_edge_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true))
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}
