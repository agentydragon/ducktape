package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"

	"github.com/agentydragon/ducktape/agentplane/history_service/backfill"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/google/uuid"
)

type settlementOptions struct {
	session uuid.UUID
	all     bool
	apply   bool
}

func parseSettlementOptions(args []string) (settlementOptions, error) {
	flags := flag.NewFlagSet("settle-history", flag.ContinueOnError)
	session := flags.String("session", "", "one retained Session UUID")
	all := flags.Bool("all", false, "scan every retained Session")
	apply := flags.Bool("apply", false, "commit eligible settlements")
	dryRun := flags.Bool("dry-run", false, "report eligible settlements without writes")
	if err := flags.Parse(args); err != nil {
		return settlementOptions{}, err
	}
	if flags.NArg() != 0 || *apply == *dryRun || *all == (*session != "") {
		return settlementOptions{}, errors.New("use exactly one of --session UUID / --all and --dry-run / --apply")
	}
	options := settlementOptions{all: *all, apply: *apply}
	if !*all {
		id, err := uuid.Parse(*session)
		if err != nil || id == uuid.Nil {
			return settlementOptions{}, errors.New("session must be a nonzero UUID")
		}
		options.session = id
	}
	return options, nil
}

func runSettleHistory(ctx context.Context, args []string) error {
	options, err := parseSettlementOptions(args)
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
