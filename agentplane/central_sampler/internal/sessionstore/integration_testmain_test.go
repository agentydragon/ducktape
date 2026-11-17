package sessionstore

import (
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

const fixturePlatformInstructions = "Fixture Agentplane platform guidance."

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTestsWithDatabase(m, migrations.Up, &integrationDatabase))
}

func newTestStoreWithDSN(t testing.TB) (*Store, string) {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db := postgres.Open(t, dsn)
	return New(db, 30*time.Second), dsn
}

func newTestStore(t testing.TB) *Store {
	t.Helper()
	store, _ := newTestStoreWithDSN(t)
	return store
}
