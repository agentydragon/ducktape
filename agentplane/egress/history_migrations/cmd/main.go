// egress-history-migrate is an explicit job, not part of the proxy's startup.
package main

import (
	"context"
	"database/sql"
	"flag"
	"fmt"
	"os"

	"github.com/agentydragon/ducktape/agentplane/egress/history_migrations"
	_ "github.com/lib/pq"
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
	db, err := sql.Open("postgres", *databaseURL)
	if err == nil {
		defer db.Close()
		err = migrations.Up(context.Background(), db)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
