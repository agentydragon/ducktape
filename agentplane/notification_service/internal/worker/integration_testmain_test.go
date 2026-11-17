package worker

import (
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var database *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTestsWithDatabase(m, migrations.Up, &database))
}
