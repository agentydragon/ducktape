package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
)

func runCompactJournal(ctx context.Context, args []string) error {
	flags := flag.NewFlagSet("compact-journal", flag.ContinueOnError)
	session := flags.String("session", "", "compact one sampler Session ID")
	all := flags.Bool("all", false, "compact every sampler Session")
	apply := flags.Bool("apply", false, "commit journal blocks")
	dryRun := flags.Bool("dry-run", false, "preview journal blocks without writing")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != 0 || *all == (*session != "") || *apply == *dryRun {
		return errors.New("select exactly one of --session ID / --all and --dry-run / --apply")
	}
	databaseURL := os.Getenv(samplerDatabaseURLEnv)
	if databaseURL == "" {
		return fmt.Errorf("%s is required", samplerDatabaseURLEnv)
	}
	connectCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	db, err := postgres.Open(connectCtx, databaseURL, 1, 1)
	cancel()
	if err != nil {
		return fmt.Errorf("open sampler database: %v", safeErrorType(err))
	}
	defer db.Close()
	store := sessionstore.New(db, 30*time.Second, nil)
	run := func(id string) error {
		result, err := store.CompactJournal(ctx, id, *apply)
		if err != nil {
			return fmt.Errorf("compact Session %q: %w", id, err)
		}
		slog.InfoContext(ctx, "sampler journal compaction", "session_id", id, "apply", *apply,
			"events", result.Events, "blocks", result.Blocks,
			"stored_bytes", result.StoredBytes, "packed_bytes", result.PackedBytes)
		return nil
	}
	if !*all {
		return run(*session)
	}
	for after := ""; ; {
		ids, err := store.ListJournalSessions(ctx, after, 100)
		if err != nil || len(ids) == 0 {
			return err
		}
		for _, id := range ids {
			if err := run(id); err != nil {
				return err
			}
		}
		after = ids[len(ids)-1]
	}
}
