package contentdb

import (
	"database/sql"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTestsWithDatabase(m, migrations.Up, &integrationDatabase))
}

func openTestDB(t testing.TB) *sql.DB {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	return postgres.Open(t, dsn)
}
