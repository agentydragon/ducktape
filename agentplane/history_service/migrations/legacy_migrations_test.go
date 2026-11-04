package migrations

import (
	"database/sql"
	"embed"
	"io/fs"

	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

// These files recreate predecessor schemas for cutover tests; the serving binary embeds only v11.
//
//go:embed legacy/*.sql
var legacyScripts embed.FS

func legacyProvider(db *sql.DB) (*goose.Provider, error) {
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(0x4849_5354))
	if err != nil {
		return nil, err
	}
	files, err := fs.Sub(legacyScripts, "legacy")
	if err != nil {
		return nil, err
	}
	backfill := goose.NewGoMigration(4, &goose.GoFunc{RunDB: backfillEventKinds}, &goose.GoFunc{RunDB: rollbackEventKindBackfill})
	entities := goose.NewGoMigration(7, &goose.GoFunc{RunTx: migrateThreadEntities}, &goose.GoFunc{RunTx: rollbackThreadEntities})
	return goose.NewProvider(goose.DialectPostgres, db, files,
		goose.WithTableName("history_service_goose_version"), goose.WithSessionLocker(locker),
		goose.WithDisableGlobalRegistry(true), goose.WithGoMigrations(backfill, entities, sessionPlacement, repairLegacySamplerSummary))
}
