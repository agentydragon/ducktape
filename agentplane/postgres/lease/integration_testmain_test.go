package lease

import (
	"context"
	"database/sql"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		func(ctx context.Context, db *sql.DB) error {
			_, err := db.ExecContext(ctx, `
				CREATE TABLE test_row (id uuid PRIMARY KEY, holder uuid, until timestamptz,
					generation bigint NOT NULL DEFAULT 0);
				CREATE TABLE test_owned_lease (scope text, id uuid, holder uuid NOT NULL, until timestamptz NOT NULL,
					PRIMARY KEY (scope, id))`)
			return err
		},
		func(database *postgres.Database) int {
			integrationDatabase = database
			return m.Run()
		},
	))
}
