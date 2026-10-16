// runner-gateway-queue-migrate is an explicit job, not part of the serving process startup.
package main

import (
	"context"
	"database/sql"
	"flag"
	"fmt"
	"os"

	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/migrations"
	_ "github.com/lib/pq"
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
