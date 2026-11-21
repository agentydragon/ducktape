package sessionstore

import (
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/sampler/migrations"
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

func claim(t *testing.T, store *Store, session *Session) Owner {
	t.Helper()
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	return owner
}

func claimedSession(t *testing.T) (*Store, *Session, Owner) {
	t.Helper()
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	return store, session, claim(t, store, session)
}

func expireLease(t *testing.T, store *Store, session *Session) {
	t.Helper()
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1`, session.ConversationID); err != nil {
		t.Fatal(err)
	}
}
