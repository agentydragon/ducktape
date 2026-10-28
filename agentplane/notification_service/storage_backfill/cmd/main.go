// notification-destination-backfill copies retained destination_ref JSON into relational columns.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/migrate"
)

const (
	databaseURLEnv     = "AGENTPLANE_NOTIFICATIONS_DATABASE_URL"
	requiredWriterGate = "notification-destination-dual-write-v1"
)

func main() {
	writerGate := flag.String("writer-gate", "", "required rollout gate: "+requiredWriterGate)
	flag.Parse()
	if *writerGate != requiredWriterGate {
		fmt.Fprintf(os.Stderr, "run only after every Notification Service writer is at the dual-write rollout gate; pass --writer-gate=%s\n", requiredWriterGate)
		os.Exit(2)
	}
	databaseURL := os.Getenv(databaseURLEnv)
	if databaseURL == "" {
		fmt.Fprintln(os.Stderr, databaseURLEnv+" is required")
		os.Exit(2)
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	if err := migrate.Run(context.Background(), databaseURL, store.BackfillInboxDestinations); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
