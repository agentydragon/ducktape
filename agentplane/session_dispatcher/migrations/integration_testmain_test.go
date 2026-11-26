package migrations

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(m.Run, postgres.NoMigrations, &integrationDatabase))
}
