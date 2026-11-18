package projection

import (
	"database/sql"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/history_service/testing"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTestsWithDatabase(m, historytesting.SetupIntegrationDatabase, &integrationDatabase))
}

func testDB(t *testing.T) *sql.DB { return openDB(t, "") }

// openDB opens the fixture database; a non-empty applicationName names its connections in pg_stat_activity.
func openDB(t *testing.T, applicationName string) *sql.DB {
	t.Helper()
	return postgres.Open(t, postgres.URLWithApplicationName(t, integrationDatabase.URL, applicationName).String())
}
