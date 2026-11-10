package sessiondelegation

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/runner_gateway/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var dispatcherDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(migrations.Up, func(database *postgres.Database) int {
		dispatcherDatabase = database
		return m.Run()
	}))
}
