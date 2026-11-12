package migrate

import (
	"context"
	"database/sql"
	"io/fs"

	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

// Schema is one PostgreSQL database's embedded Goose migrations. Table and LockID are part of the deployed database:
// changing either makes the next startup see an empty history or race another migrator.
type Schema struct {
	// Files holds the SQL migrations under Dir.
	Files  fs.FS
	Dir    string
	Table  string
	LockID int64
}

// Provider is the Goose provider for the schema on db: a PostgreSQL session lock serializes migrators, and the
// global registry is disabled so only Files and explicitly supplied Go migrations apply.
func (s Schema) Provider(db *sql.DB, migrations ...*goose.Migration) (*goose.Provider, error) {
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(s.LockID))
	if err != nil {
		return nil, err
	}
	files, err := fs.Sub(s.Files, s.Dir)
	if err != nil {
		return nil, err
	}
	return goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName(s.Table), goose.WithSessionLocker(locker), goose.WithDisableGlobalRegistry(true), goose.WithGoMigrations(migrations...))
}

// Up applies the schema's pending migrations. It is the up function Run takes.
func (s Schema) Up(ctx context.Context, db *sql.DB) error {
	provider, err := s.Provider(db)
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}
