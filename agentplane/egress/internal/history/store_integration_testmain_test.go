package history

import (
	"os"
	"testing"

	migrations "github.com/agentydragon/ducktape/agentplane/egress/internal/history/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTestsWithDatabase(m, migrations.Up, &integrationDatabase))
}
