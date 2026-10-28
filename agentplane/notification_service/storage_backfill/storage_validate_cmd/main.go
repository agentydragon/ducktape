// notification-storage-validate validates a named notification storage proof after its writer rollout gate.
package main

import (
	"context"
	"database/sql"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/migrate"
)

const databaseURLEnv = "AGENTPLANE_NOTIFICATIONS_DATABASE_URL"

const (
	subscriptionCreationProof = "subscription-creation"

	subscriptionCreationWriterGate = "notification-subscription-creation-typed-only-v1"
)

func main() {
	proof := flag.String("proof", "", "proof to validate: subscription-creation or inbox-destination")
	writerGate := flag.String("writer-gate", "", "required exact rollout gate for the selected proof")
	flag.Parse()
	var requiredWriterGate string
	var validate func(context.Context, *sql.DB) error
	switch *proof {
	case subscriptionCreationProof:
		requiredWriterGate = subscriptionCreationWriterGate
		validate = store.ValidateSubscriptionCreationConstraints
	default:
		fmt.Fprintf(os.Stderr, "pass --proof=%s\n", subscriptionCreationProof)
		os.Exit(2)
	}
	if *writerGate != requiredWriterGate {
		fmt.Fprintf(os.Stderr, "run only after every Notification Service writer is at the selected typed-only rollout gate; pass --writer-gate=%s\n", requiredWriterGate)
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
	if err := migrate.Run(ctx, databaseURL, validate); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
