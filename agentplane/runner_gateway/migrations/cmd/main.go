// runner-gateway-queue-migrate is an explicit job, not part of the serving process startup.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"

	"github.com/agentydragon/ducktape/agentplane/postgres/go/migrate"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/migrations"
)

func main() {
	databaseURL := flag.String("database-url", "", "Dispatcher PostgreSQL URL")
	flag.Parse()
	if *databaseURL == "" {
		*databaseURL = os.Getenv("DATABASE_URL")
	}
	if *databaseURL == "" {
		fmt.Fprintln(os.Stderr, "Dispatcher database URL is required")
		os.Exit(2)
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	if err := migrate.Run(context.Background(), *databaseURL, migrations.Up); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
