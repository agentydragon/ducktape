// notification-event-identity-validate validates relational entry identity before its contract migration.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/migrate"
)

const (
	databaseURLEnv  = "AGENTPLANE_NOTIFICATIONS_DATABASE_URL"
	writerGateValue = "notification-event-identity-writer-only-v1"
)

func main() {
	writerGate := flag.String("writer-gate", "", "required rollout gate: "+writerGateValue)
	flag.Parse()
	if flag.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "notification-event-identity-validate accepts only --writer-gate")
		os.Exit(2)
	}
	if *writerGate != writerGateValue {
		fmt.Fprintf(os.Stderr, "run only after every Notification Service writer is at the event identity writer-only gate; pass --writer-gate=%s\n", writerGateValue)
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
	if err := migrate.Run(ctx, databaseURL, store.ValidateEventIdentityWriterOnly); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
