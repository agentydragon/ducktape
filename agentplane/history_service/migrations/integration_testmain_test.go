package migrations

import (
	"context"
	"database/sql"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var migrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(func(ctx context.Context, db *sql.DB) error {
		return postgres.EnsureElectricRole(ctx, db)
	}, func(database *postgres.Database) int {
		migrationDatabase = database
		return m.Run()
	}))
}
