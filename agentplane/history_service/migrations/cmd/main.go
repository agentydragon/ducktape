// history-migrate is an init container, not part of the History Service's startup.
package main

import (
	"context"
	"database/sql"
	"fmt"
	"os"

	"github.com/agentydragon/ducktape/agentplane/history_service/migrations"
	_ "github.com/lib/pq"
)

const databaseURLEnv = "AGENTPLANE_HISTORY_SERVICE_DATABASE_URL"

func main() {
	databaseURL := os.Getenv(databaseURLEnv)
	if databaseURL == "" {
		fmt.Fprintln(os.Stderr, databaseURLEnv+" is required")
		os.Exit(2)
	}
	db, err := sql.Open("postgres", databaseURL)
	if err == nil {
		defer db.Close()
		err = migrations.Up(context.Background(), db)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
