// notifications-migrate is an init container, not part of the Notification Service's startup.
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

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/migrate"
)

const (
	databaseURLEnv                       = "AGENTPLANE_NOTIFICATIONS_DATABASE_URL"
	requiredEventIdentityWriterGate      = "notification-event-identity-dual-write-v1"
	requiredEntryContentWriterGate       = "notification-entry-typed-payload-dual-write-v1"
	defaultEntryContentBackfillBatchSize = 512
	maxEntryContentBackfillBatchSize     = 4096
)

func main() {
	databaseURL := os.Getenv(databaseURLEnv)
	if databaseURL == "" {
		fmt.Fprintln(os.Stderr, databaseURLEnv+" is required")
		os.Exit(2)
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var run func(context.Context, *sql.DB) error
	switch {
	case len(os.Args) == 1:
		run = migrations.Up
	case os.Args[1] == "backfill-event-identities":
		flags := flag.NewFlagSet(os.Args[1], flag.ContinueOnError)
		writerGate := flags.String("writer-gate", "", "required rollout gate: "+requiredEventIdentityWriterGate)
		batchSize := flags.Int("batch-size", 512, "maximum entry identities per short transaction")
		if err := flags.Parse(os.Args[2:]); err != nil {
			os.Exit(2)
		}
		if flags.NArg() != 0 || *batchSize < 1 || *batchSize > 4096 {
			fmt.Fprintln(os.Stderr, "backfill-event-identities accepts --writer-gate=... and --batch-size=N where N is between 1 and 4096")
			os.Exit(2)
		}
		if *writerGate != requiredEventIdentityWriterGate {
			fmt.Fprintf(os.Stderr, "run only after every Notification Service writer is at the dual-write rollout gate; pass --writer-gate=%s\n", requiredEventIdentityWriterGate)
			os.Exit(2)
		}
		run = func(ctx context.Context, db *sql.DB) error {
			return store.BackfillEventIdentities(ctx, db, *batchSize)
		}
	case os.Args[1] == "backfill-entry-content":
		flags := flag.NewFlagSet(os.Args[1], flag.ContinueOnError)
		writerGate := flags.String("writer-gate", "", "required rollout gate: "+requiredEntryContentWriterGate)
		batchSize := flags.Int("batch-size", defaultEntryContentBackfillBatchSize, "maximum entry keys per short transaction")
		if err := flags.Parse(os.Args[2:]); err != nil {
			os.Exit(2)
		}
		if flags.NArg() != 0 || *batchSize < 1 || *batchSize > maxEntryContentBackfillBatchSize {
			fmt.Fprintf(os.Stderr, "backfill-entry-content accepts --writer-gate=... and --batch-size=N where N is between 1 and %d\n", maxEntryContentBackfillBatchSize)
			os.Exit(2)
		}
		if *writerGate != requiredEntryContentWriterGate {
			fmt.Fprintf(os.Stderr, "run only after every Notification Service writer and cleanup worker is at the dual-write rollout gate; pass --writer-gate=%s\n", requiredEntryContentWriterGate)
			os.Exit(2)
		}
		run = func(ctx context.Context, db *sql.DB) error {
			return store.BackfillEntryContent(ctx, db, *batchSize)
		}
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n", os.Args[1])
		fmt.Fprintln(os.Stderr, "commands: (default) migrate, backfill-event-identities, backfill-entry-content")
		os.Exit(2)
	}
	if err := migrate.Run(ctx, databaseURL, run); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
