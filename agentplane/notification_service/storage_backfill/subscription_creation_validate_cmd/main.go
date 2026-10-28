// notification-subscription-creation-validate validates the typed subscription storage constraints.
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
	databaseURLEnv     = "AGENTPLANE_NOTIFICATIONS_DATABASE_URL"
	requiredWriterGate = "notification-subscription-creation-typed-only-v1"
)

func main() {
	writerGate := flag.String("writer-gate", "", "required rollout gate: "+requiredWriterGate)
	flag.Parse()
	if *writerGate != requiredWriterGate {
		fmt.Fprintf(os.Stderr, "run only after every Notification Service writer is at the typed-only rollout gate; pass --writer-gate=%s\n", requiredWriterGate)
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
	if err := migrate.Run(ctx, databaseURL, store.ValidateSubscriptionCreationConstraints); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
