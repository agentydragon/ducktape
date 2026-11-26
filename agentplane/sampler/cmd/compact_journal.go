package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
)

func runCompactJournal(ctx context.Context, args []string) error {
	flags := flag.NewFlagSet("compact-journal", flag.ContinueOnError)
	session := flags.String("session", "", "compact one sampler Session ID")
	all := flags.Bool("all", false, "compact every retained sampler Session")
	apply := flags.Bool("apply", false, "commit journal replacements")
	dryRun := flags.Bool("dry-run", false, "preview journal replacements without writing")
	batchDelay := flags.Duration("batch-delay", 50*time.Millisecond, "minimum delay between completed batches")
	pauseFile := flags.String("pause-file", "", "pause at batch boundaries while this file exists")
	busyRetries := flags.Int("busy-retries", 3, "retries for Session lock contention or changed inputs")
	retryDelay := flags.Duration("retry-delay", 250*time.Millisecond, "delay between contention retries")
	defaults := eventblock.DefaultProfile(128)
	targetBytes := flags.Int("event-block-target-bytes", defaults.TargetBytes, "event block target frame bytes; match the live sampler profile")
	targetEntries := flags.Int("event-block-target-entries", defaults.TargetEntries, "event block target entry count; match the live sampler profile")
	level := flags.Int("event-block-level", defaults.Level, "stock zstd compression level (1-4); match the live sampler profile")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != 0 || *all == (*session != "") || *apply == *dryRun {
		return errors.New("select exactly one of --session ID / --all and --dry-run / --apply")
	}
	if *batchDelay < 0 || *batchDelay > time.Minute || *busyRetries < 0 || *busyRetries > 10 || *retryDelay < 0 || *retryDelay > time.Minute {
		return errors.New("batch delay or contention retry settings are outside supported bounds")
	}
	profile := eventblock.Profile{TargetBytes: *targetBytes, TargetEntries: *targetEntries, Level: *level}
	if _, err := eventblock.NewPacker(profile); err != nil {
		return fmt.Errorf("invalid event block profile: %w", err)
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
	store := sessionstore.New(db, 30*time.Second, &samplersettings.Settings{
		EventBlockProfile: profile,
	})
	options := sessionstore.JournalCompactionOptions{
		BatchDelay: *batchDelay, PauseFile: *pauseFile,
		ContentionRetries: *busyRetries, RetryDelay: *retryDelay,
	}
	run := func(id string) error {
		result, err := store.CompactJournalWithOptions(ctx, id, *apply, options)
		level := slog.InfoContext
		if err != nil {
			level = slog.WarnContext
		}
		level(ctx, "sampler journal repack", "session_id", id, "apply", *apply,
			"profile_target_bytes", profile.TargetBytes, "profile_target_entries", profile.TargetEntries, "profile_level", profile.Level,
			"opening_cursor", result.OpeningCursor, "completed", result.Completed, "batches", result.Batches,
			"events", result.Events, "blocks", result.Blocks, "stored_bytes", result.StoredBytes,
			"packed_bytes", result.PackedBytes, "lock_hold_total_ms", result.LockHoldTotal.Milliseconds(),
			"lock_hold_max_ms", result.LockHoldMax.Milliseconds(), "error_type", safeErrorType(err))
		if err != nil {
			return err
		}
		return nil
	}
	if !*all {
		if err := run(*session); err != nil {
			return fmt.Errorf("repack Session %q: %w", *session, err)
		}
		return nil
	}
	var deferred, failed int
	var failures []error
	seenSession := false
	for after := ""; ; {
		ids, err := store.ListJournalSessions(ctx, after, 100)
		if err != nil || len(ids) == 0 {
			if err != nil {
				return err
			}
			break
		}
		for _, id := range ids {
			if seenSession && options.BatchDelay > 0 {
				if err := waitCLIContext(ctx, options.BatchDelay); err != nil {
					return err
				}
			}
			seenSession = true
			if err := run(id); err != nil {
				if errors.Is(err, sessionstore.ErrJournalBusy) {
					deferred++
					slog.WarnContext(ctx, "sampler journal Session deferred after bounded contention retries", "session_id", id, "error", err)
					continue
				}
				failed++
				failures = append(failures, fmt.Errorf("Session %q: %w", id, err))
				slog.ErrorContext(ctx, "sampler journal Session repack failed", "session_id", id, "error", err)
			}
		}
		after = ids[len(ids)-1]
	}
	slog.InfoContext(ctx, "sampler journal repack fleet pass finished", "apply", *apply, "deferred_busy_sessions", deferred, "failed_sessions", failed)
	return errors.Join(failures...)
}

func waitCLIContext(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
