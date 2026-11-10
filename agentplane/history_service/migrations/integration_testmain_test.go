package migrations

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
)

var migrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	database, err := postgres.Start(ctx, func(ctx context.Context, db *sql.DB) error {
		if err := postgres.EnsureElectricRole(ctx, db); err != nil {
			return err
		}
		return nil
	})
	cancel()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	migrationDatabase = database

	code := m.Run()
	if err := database.Close(); err != nil {
		fmt.Fprintln(os.Stderr, "close PostgreSQL fixture:", err)
		if code == 0 {
			code = 1
		}
	}
	os.Exit(code)
}
