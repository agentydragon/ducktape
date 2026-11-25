package migrations

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var migrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(m.Run, postgres.EnsureElectricRole, &migrationDatabase))
}
