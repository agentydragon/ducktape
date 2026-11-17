package ownershiprewrite

import (
	"context"
	"database/sql"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	applyMigrations := func(ctx context.Context, db *sql.DB) error { return migrations.Up(ctx, db) }
	os.Exit(postgres.RunIntegrationTestsWithDatabase(m, applyMigrations, &integrationDatabase))
}

func newTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}
