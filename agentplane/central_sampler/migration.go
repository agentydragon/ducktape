package centralsampler

import (
	"context"
	"database/sql"
	"embed"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/pressly/goose/v3"
)

//go:embed schema/*.sql
var migrationFiles embed.FS

var schema = migrate.Schema{Files: migrationFiles, Dir: "schema", Table: "central_sampler_goose_version", LockID: 7241821}

// Migrate applies SQL migrations and the single native-content conversion migration.
func Migrate(ctx context.Context, db *sql.DB) error { return migrateSampler(ctx, db, false) }

// MigrateDuringMaintenance is used by this binary's --migrate-only invocation.
// Existing databases require an explicit writer cutover; a rolling server start
// must not retire columns while an older sampler is still using them.
func MigrateDuringMaintenance(ctx context.Context, db *sql.DB) error {
	return migrateSampler(ctx, db, true)
}

func migrateSampler(ctx context.Context, db *sql.DB, maintenance bool) error {
	convert := func(ctx context.Context, db *sql.DB) error {
		var populated bool
		if err := db.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_session)`).Scan(&populated); err != nil {
			return err
		}
		if populated && !maintenance {
			return fmt.Errorf("native content migration requires stopped sampler writers and --migrate-only")
		}
		var leased bool
		if err := db.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM sampler_conversation WHERE owner IS NOT NULL AND lease_until>now())`).Scan(&leased); err != nil {
			return err
		}
		if leased {
			return fmt.Errorf("stop sampler writers and wait for their leases before native content migration")
		}
		return migrateContentStore(ctx, db)
	}
	provider, err := schema.Provider(db, goose.NewGoMigration(13, &goose.GoFunc{RunDB: convert}, nil))
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}
