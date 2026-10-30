package centralsampler

import (
	"context"
	"database/sql"
	"encoding/json"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	migrate := func(ctx context.Context, db *sql.DB) error { return Migrate(ctx, db, nil) }
	os.Exit(postgres.RunIntegrationTests(migrate, func(database *postgres.Database) int {
		integrationDatabase = database
		return m.Run()
	}))
}

func newTestStoreWithDSN(t testing.TB) (*Store, string) {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return New(db), dsn
}

func newTestStore(t testing.TB) *Store {
	t.Helper()
	store, _ := newTestStoreWithDSN(t)
	return store
}

func nativeResponsesRecords(t testing.TB, items ...ResponsesItem) []json.RawMessage {
	t.Helper()
	records := make([]json.RawMessage, len(items))
	for index, item := range items {
		encoded, err := json.Marshal(item)
		if err != nil {
			t.Fatalf("marshal native Responses record %d: %v", index+1, err)
		}
		records[index] = encoded
	}
	return records
}

func responsesItems(t testing.TB, state NativeContext) []ResponsesItem {
	t.Helper()
	responses, ok := state.(*ResponsesContext)
	if !ok {
		if state == nil {
			t.Fatal("native context is nil, want ResponsesContext")
		}
		t.Fatalf("native context is %T, want ResponsesContext", state)
	}
	return responses.Items
}
