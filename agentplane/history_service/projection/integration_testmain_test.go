package projection

import (
	"database/sql"
	"net/url"
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

func testDB(t *testing.T) *sql.DB { return openDB(t, "") }

// openDB opens the fixture database; a non-empty applicationName names its connections in pg_stat_activity.
func openDB(t *testing.T, applicationName string) *sql.DB {
	t.Helper()
	target, err := url.Parse(integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	if applicationName != "" {
		query := target.Query()
		query.Set("application_name", applicationName)
		target.RawQuery = query.Encode()
	}
	db := postgres.Open(t, target.String())
	return db
}
