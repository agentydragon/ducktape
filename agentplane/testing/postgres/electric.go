package postgres

import (
	"context"
	"database/sql"
)

// EnsureElectricRole creates the cluster-wide Electric login that migrations grant on, as the cluster does before any migrator runs.
func EnsureElectricRole(ctx context.Context, db *sql.DB) error {
	_, err := db.ExecContext(ctx, `DO $$ BEGIN
		IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'electric') THEN
			CREATE ROLE electric LOGIN REPLICATION PASSWORD 'electric';
		END IF;
	END $$`)
	return err
}
