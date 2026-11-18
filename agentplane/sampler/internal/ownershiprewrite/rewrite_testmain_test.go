package ownershiprewrite

import (
	"context"
	"database/sql"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	applyMigrations := func(ctx context.Context, db *sql.DB) error { return migrations.Up(ctx, db) }
	os.Exit(postgres.RunIntegrationTestsWithDatabase(m, applyMigrations, &integrationDatabase))
}

func newTestDB(t *testing.T) *sql.DB {
	t.Helper()
	return postgres.Open(t, integrationDatabase.Clone(t))
}
