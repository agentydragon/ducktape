package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"

	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/google/uuid"
)

func runCompactHistory(ctx context.Context, args []string) error {
	options, err := parseHistoryMaintenanceOptions("compact-history", args)
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
	history := storage.NewStore(db, storage.Options{})
	run := func(id uuid.UUID) error {
		result, err := history.CompactHistory(ctx, id, options.apply)
		slog.InfoContext(ctx, "historical compaction Session", "session_id", id, "apply", options.apply,
			"events", result.Entries, "blocks", result.Blocks, "uncompressed_bytes", result.UncompressedBytes,
			"packed_bytes", result.PackedBytes, "skipped_inside_blocks", result.Skipped)
		return err
	}
	if !options.all {
		return run(options.session)
	}
	for position := uint64(0); ; {
		sessions, err := history.RecordedSessions(ctx, position, 100)
		if err != nil || len(sessions) == 0 {
			return err
		}
		for _, session := range sessions {
			position = session.Position
			if err := run(session.SessionID); err != nil {
				return fmt.Errorf("compacting Session %s: %w", session.SessionID, err)
			}
		}
	}
}
