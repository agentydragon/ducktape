package migrations

import (
	"context"
	"database/sql"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		func(context.Context, *sql.DB) error { return nil },
		func(database *postgres.Database) int {
			integrationDatabase = database
			return m.Run()
		},
	))
}
