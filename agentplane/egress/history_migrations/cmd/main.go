// egress-history-migrate runs as the egress proxy Pod's migration init container.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"

	"github.com/agentydragon/ducktape/agentplane/egress/history_migrations"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/migrate"
)

func main() {
	databaseURL := flag.String("database-url", "", "egress decision-history PostgreSQL URL")
	flag.Parse()
	if *databaseURL == "" {
		*databaseURL = os.Getenv("DATABASE_URL")
	}
	if *databaseURL == "" {
		fmt.Fprintln(os.Stderr, "egress decision-history database URL is required")
		os.Exit(2)
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	if err := migrate.Run(context.Background(), *databaseURL, migrations.Up); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
