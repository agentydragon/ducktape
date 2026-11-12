package auth

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/go/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(migrations.Up, func(database *postgres.Database) int {
		integrationDatabase = database
		return m.Run()
	}))
}
