package auth

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/internal/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTestsWithDatabase(m, migrations.Up, &integrationDatabase))
}
