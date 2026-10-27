package reads

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/history_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		func(ctx context.Context, db *sql.DB) error {
			if err := postgres.EnsureElectricRole(ctx, db); err != nil {
				return err
			}
			return migrations.Up(ctx, db)
		},
		func(database *postgres.Database) int {
			integrationDatabase = database
			return m.Run()
		},
	))
}
