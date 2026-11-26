package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/agentydragon/ducktape/agentplane/history_service/backfill"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	"github.com/google/uuid"
)

type historyMaintenanceOptions struct {
	session            uuid.UUID
	all                bool
	apply              bool
	bytesPerSecond     int64
	pauseFile          string
	guardFile          string
	maxReplicaLagBytes int64
	minFreeBytes       int64
	guardMaxAge        time.Duration
}

func parseHistoryMaintenanceOptions(command string, args []string) (historyMaintenanceOptions, error) {
	flags := flag.NewFlagSet(command, flag.ContinueOnError)
	session := flags.String("session", "", "one retained Session UUID")
	all := flags.Bool("all", false, "scan every retained Session")
	apply := flags.Bool("apply", false, "commit changes")
	dryRun := flags.Bool("dry-run", false, "preview without writes")
	var bytesPerSecond, maxReplicaLagBytes, minFreeBytes int64
	var pauseFile, guardFile string
	var guardMaxAge time.Duration
	if command == "compact-history" {
		flags.Int64Var(&bytesPerSecond, "bytes-per-second", 1<<20, "maximum source bytes processed per second")
		flags.StringVar(&pauseFile, "pause-file", "", "pause while this file exists")
		flags.StringVar(&guardFile, "guard-file", "", "JSON control file with updated_at, replica_lag_bytes, and free_bytes")
		flags.Int64Var(&maxReplicaLagBytes, "max-replica-lag-bytes", 0, "pause above this replica replay lag (requires --guard-file)")
		flags.Int64Var(&minFreeBytes, "min-free-bytes", 0, "pause below this free-space value (requires --guard-file)")
		flags.DurationVar(&guardMaxAge, "guard-max-age", 60*time.Second, "maximum age of the guard file before pausing")
		guardMaxAge = 60 * time.Second
	}
	if err := flags.Parse(args); err != nil {
		return historyMaintenanceOptions{}, err
	}
	if flags.NArg() != 0 || *apply == *dryRun || *all == (*session != "") {
		return historyMaintenanceOptions{}, errors.New("use exactly one of --session UUID / --all and --dry-run / --apply")
	}
	options := historyMaintenanceOptions{
		all: *all, apply: *apply, bytesPerSecond: bytesPerSecond, pauseFile: pauseFile, guardFile: guardFile,
		maxReplicaLagBytes: maxReplicaLagBytes, minFreeBytes: minFreeBytes, guardMaxAge: guardMaxAge,
	}
	if command == "compact-history" {
		if options.bytesPerSecond <= 0 || options.maxReplicaLagBytes < 0 || options.minFreeBytes < 0 || options.guardMaxAge <= 0 {
			return historyMaintenanceOptions{}, errors.New("invalid repack rate or guard threshold")
		}
		if (options.maxReplicaLagBytes > 0 || options.minFreeBytes > 0) && options.guardFile == "" {
			return historyMaintenanceOptions{}, errors.New("--max-replica-lag-bytes and --min-free-bytes require --guard-file")
		}
	}
	if !*all {
		id, err := uuid.Parse(*session)
		if err != nil || id == uuid.Nil {
			return historyMaintenanceOptions{}, errors.New("session must be a nonzero UUID")
		}
		options.session = id
	}
	return options, nil
}

func runSettleHistory(ctx context.Context, args []string) error {
	options, err := parseHistoryMaintenanceOptions("settle-history", args)
	if err != nil {
		return err
	}
	databaseURL := os.Getenv(settingPrefix + "DATABASE_URL")
	if databaseURL == "" {
		return errors.New("database_url is required")
	}
	db, err := openDatabase(ctx, databaseURL, 2)
	if err != nil {
		return err
	}
	defer db.Close()
	run := func(id uuid.UUID) error {
		result, err := backfill.RunSession(ctx, db, id, options.apply)
		slog.InfoContext(ctx, "historical settlement Session", "session_id", id, "apply", options.apply,
			"items", result.Items, "rows", result.Rows, "logical_payload_bytes", result.PayloadBytes)
		return err
	}
	if !options.all {
		return run(options.session)
	}
	history := storage.NewStore(db, storage.Options{})
	var retry error
	for position := uint64(0); ; {
		sessions, err := history.RecordedSessions(ctx, position, 100)
		if err != nil {
			return err
		}
		if len(sessions) == 0 {
			return retry
		}
		for _, session := range sessions {
			position = session.Position
			if err := run(session.SessionID); err != nil {
				if errors.Is(err, backfill.ErrProjectionBusy) || errors.Is(err, backfill.ErrSessionAdvanced) {
					slog.WarnContext(ctx, "Session needs a later settlement pass", "session_id", session.SessionID, "error", err)
					retry = errors.Join(retry, err)
					continue
				}
				return fmt.Errorf("settling Session %s: %w", session.SessionID, err)
			}
		}
	}
}
