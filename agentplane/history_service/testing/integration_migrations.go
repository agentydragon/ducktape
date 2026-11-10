// Package historytesting contains integration test helpers for the history service.
package historytesting

import (
	"context"
	"database/sql"

	"github.com/agentydragon/ducktape/agentplane/history_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

func SetupIntegrationDatabase(ctx context.Context, db *sql.DB) error {
	if err := postgres.EnsureElectricRole(ctx, db); err != nil {
		return err
	}
	return migrations.Up(ctx, db)
}
