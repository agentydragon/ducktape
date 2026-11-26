package wakeups

import (
	"context"
	"database/sql"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		m.Run,
		func(context.Context, *sql.DB) error { return nil },
		&integrationDatabase,
	))
}
