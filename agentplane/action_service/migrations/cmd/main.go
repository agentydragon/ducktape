// actions-migrate is an init container, not part of the Action Service's startup.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/action_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/migrate"
)

const databaseURLEnv = "AGENTPLANE_ACTIONS_DATABASE_URL"

func main() {
	args := os.Args[1:]
	if len(args) != 0 {
		fmt.Fprintln(os.Stderr, "usage: actions-migrate")
		os.Exit(2)
	}
	databaseURL := os.Getenv(databaseURLEnv)
	if databaseURL == "" {
		fmt.Fprintln(os.Stderr, databaseURLEnv+" is required")
		os.Exit(2)
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := migrate.Run(ctx, databaseURL, migrations.Up); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
