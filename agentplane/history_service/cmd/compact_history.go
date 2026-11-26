package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
)

type repackGuardState struct {
	UpdatedAt       time.Time `json:"updated_at"`
	ReplicaLagBytes int64     `json:"replica_lag_bytes"`
	FreeBytes       int64     `json:"free_bytes"`
	Pause           bool      `json:"pause"`
	Reason          string    `json:"reason"`
}

type repackController struct {
	options historyMaintenanceOptions
	next    time.Time
	paused  string
}

func newRepackController(options historyMaintenanceOptions) *repackController {
	return &repackController{options: options}
}

func (c *repackController) beforeBatch(ctx context.Context, sourceBytes int64) error {
	for {
		reason, err := c.pauseReason()
		if err != nil {
			return err
		}
		if reason == "" {
			if c.paused != "" {
				slog.InfoContext(ctx, "History repack resumed", "previous_pause_reason", c.paused)
				c.paused = ""
			}
			break
		}
		if reason != c.paused {
			slog.WarnContext(ctx, "History repack paused by operator guard", "reason", reason)
			c.paused = reason
		}
		timer := time.NewTimer(5 * time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
	if sourceBytes <= 0 {
		return nil
	}
	now := time.Now()
	start := now
	if c.next.After(start) {
		start = c.next
	}
	duration := time.Duration(float64(time.Second) * float64(sourceBytes) / float64(c.options.bytesPerSecond))
	c.next = start.Add(duration)
	if delay := start.Sub(now); delay > 0 {
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
	return nil
}

func (c *repackController) pauseReason() (string, error) {
	if c.options.pauseFile != "" {
		if _, err := os.Stat(c.options.pauseFile); err == nil {
			return "pause file exists: " + c.options.pauseFile, nil
		} else if !errors.Is(err, os.ErrNotExist) {
			return "", fmt.Errorf("checking repack pause file: %w", err)
		}
	}
	if c.options.guardFile == "" {
		return "", nil
	}
	content, err := os.ReadFile(c.options.guardFile)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return "guard file is missing", nil
		}
		return "", fmt.Errorf("reading repack guard file: %w", err)
	}
	var guard repackGuardState
	if err := json.Unmarshal(content, &guard); err != nil {
		return "guard file is invalid JSON", nil
	}
	age := time.Since(guard.UpdatedAt)
	if guard.UpdatedAt.IsZero() || age > c.options.guardMaxAge || age < -30*time.Second {
		return "guard file is stale", nil
	}
	if guard.Pause {
		if guard.Reason != "" {
			return "guard requested pause: " + guard.Reason, nil
		}
		return "guard requested pause", nil
	}
	if c.options.maxReplicaLagBytes > 0 && guard.ReplicaLagBytes > c.options.maxReplicaLagBytes {
		return fmt.Sprintf("replica lag %d exceeds limit %d bytes", guard.ReplicaLagBytes, c.options.maxReplicaLagBytes), nil
	}
	if c.options.minFreeBytes > 0 && guard.FreeBytes < c.options.minFreeBytes {
		return fmt.Sprintf("free space %d is below limit %d bytes", guard.FreeBytes, c.options.minFreeBytes), nil
	}
	return "", nil
}

func runCompactHistory(ctx context.Context, args []string) error {
	options, err := parseHistoryMaintenanceOptions("compact-history", args)
	if err != nil {
		return err
	}
	c, err := loadConfig()
	if err != nil {
		return err
	}
	packers, err := c.Ingress.packers()
	if err != nil {
		return fmt.Errorf("configure History repack profile: %w", err)
	}
	db, err := openDatabaseWithApplicationName(ctx, c.DatabaseURL, 2, "history-repack")
	if err != nil {
		return err
	}
	defer db.Close()
	history := storage.NewStore(db, storage.Options{EventPackers: packers, RepackBatchBudget: options.batchBudget})
	controller := newRepackController(options)
	var result storage.CompactionResult
	if options.all {
		result, err = history.RepackAll(ctx, options.apply, controller.beforeBatch)
	} else {
		result, err = history.RepackSession(ctx, options.session, options.apply, controller.beforeBatch)
	}
	slog.InfoContext(ctx, "History online repack finished", "session_id", options.session,
		"all_sessions", options.all, "apply", options.apply, "batch_transaction_budget", options.batchBudget,
		"entries", result.Entries,
		"new_versioned_blocks", result.Blocks, "source_blocks", result.SourceBlocks,
		"legacy_source_blocks", result.LegacyBlocks, "versioned_source_blocks", result.VersionedSourceBlocks,
		"uncompressed_bytes", result.UncompressedBytes, "packed_bytes", result.PackedBytes,
		"left_ordinary", result.Skipped)
	return err
}

func runInventoryHistory(ctx context.Context, args []string) error {
	if len(args) != 0 {
		return errors.New("inventory-history takes no arguments")
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
	inventory, err := storage.NewStore(db, storage.Options{}).InventoryEventBlocks(ctx)
	if err != nil {
		return err
	}
	return json.NewEncoder(os.Stdout).Encode(inventory)
}
