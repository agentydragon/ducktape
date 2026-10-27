package wakeups

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		func(context.Context, *sql.DB) error { return nil },
		func(database *postgres.Database) int {
			integrationDatabase = database
			return m.Run()
		},
	))
}
