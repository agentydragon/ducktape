package storage

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/history_service/testing"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		historytesting.SetupIntegrationDatabase,
		func(database *postgres.Database) int {
			integrationDatabase = database
			return m.Run()
		},
	))
}
