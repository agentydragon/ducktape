// notifications-migrate is an init container, not part of the Notification Service's startup.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/migrate"
)

const databaseURLEnv = "AGENTPLANE_NOTIFICATIONS_DATABASE_URL"

func main() {
	databaseURL := os.Getenv(databaseURLEnv)
	if databaseURL == "" {
		fmt.Fprintln(os.Stderr, databaseURLEnv+" is required")
		os.Exit(2)
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if len(os.Args) != 1 {
		fmt.Fprintf(os.Stderr, "unknown command %q\n", os.Args[1])
		fmt.Fprintln(os.Stderr, "commands: (default) migrate")
		os.Exit(2)
	}
	if err := migrate.Run(ctx, databaseURL, migrations.Up); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
