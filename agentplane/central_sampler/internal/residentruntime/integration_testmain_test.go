package residentruntime

import (
	"os"
	"testing"
	"time"

	runtimetesting "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/residentruntime/testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	if runtimetesting.RunCodeModePeer() {
		return
	}
	os.Exit(postgres.RunIntegrationTestsWithDatabase(m, migrations.Up, &integrationDatabase))
}

func newTestStoreWithDSN(t testing.TB) (*sessionstore.Store, string) {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db := postgres.Open(t, dsn)
	return sessionstore.New(db, 30*time.Second), dsn
}

func newTestStore(t testing.TB) *sessionstore.Store {
	t.Helper()
	store, _ := newTestStoreWithDSN(t)
	return store
}
