package contentdb

import (
	"database/sql"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(m.Run, migrations.Up, &integrationDatabase))
}

func openTestDB(t testing.TB) *sql.DB {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	return postgres.Open(t, dsn)
}
