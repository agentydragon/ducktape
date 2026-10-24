package auth

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/migrations"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	database, err := postgres.Start(ctx, migrations.Up)
	cancel()
	if err != nil {
		fmt.Fprintln(os.Stderr, "start PostgreSQL fixture:", err)
		os.Exit(1)
	}
	integrationDatabase = database
	code := m.Run()
	if err := database.Close(); err != nil {
		fmt.Fprintln(os.Stderr, "close PostgreSQL fixture:", err)
		if code == 0 {
			code = 1
		}
	}
	os.Exit(code)
}
