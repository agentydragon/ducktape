// actions-migrate is an init container, not part of the Action Service's startup.
package main

import (
	"context"
	"database/sql"
	"fmt"
	"log/slog"
	"os"

	"github.com/agentydragon/ducktape/agentplane/action_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/migrate"
)

const databaseURLEnv = "AGENTPLANE_ACTIONS_DATABASE_URL"

func main() {
	args := os.Args[1:]
	backfill := false
	switch {
	case len(args) == 0:
	case len(args) == 2 && args[0] == "backfill-storage" && args[1] == "--confirm-all-writers-dual-write":
		backfill = true
	case len(args) > 0 && args[0] == "backfill-storage":
		fmt.Fprintln(os.Stderr, "backfill-storage requires --confirm-all-writers-dual-write after every Action Service writer has rolled out")
		os.Exit(2)
	default:
		fmt.Fprintln(os.Stderr, "usage: actions-migrate [backfill-storage --confirm-all-writers-dual-write]")
		os.Exit(2)
	}
	databaseURL := os.Getenv(databaseURLEnv)
	if databaseURL == "" {
		fmt.Fprintln(os.Stderr, databaseURLEnv+" is required")
		os.Exit(2)
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	up := migrations.Up
	if backfill {
		up = func(ctx context.Context, db *sql.DB) error {
			if err := migrations.Up(ctx, db); err != nil {
				return err
			}
			return store.BackfillActionStorage(ctx, db)
		}
	}
	if err := migrate.Run(context.Background(), databaseURL, up); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
