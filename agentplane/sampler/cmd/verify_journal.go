package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
)

func runVerifyJournal(ctx context.Context, args []string) error {
	if len(args) != 0 {
		return errors.New("verify-journal does not accept arguments")
	}
	databaseURL := os.Getenv(samplerDatabaseURLEnv)
	if databaseURL == "" {
		return fmt.Errorf("%s is required", samplerDatabaseURLEnv)
	}
	connectCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	db, err := postgres.Open(connectCtx, strings.Replace(databaseURL, "postgresql+asyncpg://", "postgresql://", 1), 1, 1)
	cancel()
	if err != nil {
		return fmt.Errorf("open sampler database: %v", safeErrorType(err))
	}
	defer db.Close()

	result, err := sessionstore.New(db, 30*time.Second, nil).VerifyJournalInventory(ctx)
	if err != nil {
		return fmt.Errorf("scan sampler journal inventory: %w", err)
	}
	var sessions, legacyBlocks, currentBlocks, ordinaryRows, legacyRows int64
	var unsupported, invalid, gaps, missingSequences, invalidRanges int64
	for _, owner := range result.Owners {
		sessions += owner.Sessions
		legacyBlocks += owner.LegacyGzipBlocks
		currentBlocks += owner.CurrentBlocks
		ordinaryRows += owner.OrdinaryRows
		legacyRows += owner.LegacyGzipRows
		unsupported += owner.UnsupportedBlocks + owner.UnsupportedRows
		invalid += owner.InvalidBlocks + owner.InvalidRows
		gaps += owner.CoverageGaps
		missingSequences += owner.MissingSequences
		invalidRanges += owner.InvalidRanges
		slog.InfoContext(ctx, "sampler journal owner inventory", "owner", owner.Owner,
			"sessions", owner.Sessions, "ordinary_rows", owner.OrdinaryRows,
			"legacy_gzip_rows", owner.LegacyGzipRows, "current_blocks", owner.CurrentBlocks,
			"legacy_gzip_blocks", owner.LegacyGzipBlocks, "unsupported_rows", owner.UnsupportedRows,
			"unsupported_blocks", owner.UnsupportedBlocks, "invalid_rows", owner.InvalidRows,
			"invalid_blocks", owner.InvalidBlocks, "coverage_gaps", owner.CoverageGaps,
			"missing_sequences", owner.MissingSequences, "invalid_ranges", owner.InvalidRanges)
	}
	for _, issue := range result.Issues {
		slog.WarnContext(ctx, "sampler journal inventory issue", "owner", issue.Owner,
			"session_id", issue.SessionID, "storage", issue.Storage, "classification", issue.Classification,
			"first_sequence", issue.First, "last_sequence", issue.Last, "reason", issue.Reason)
	}
	verifyErr := sessionstore.JournalInventoryError(result)
	slog.InfoContext(ctx, "sampler journal inventory complete", "snapshot", "repeatable_read",
		"sessions", sessions, "owners", len(result.Owners), "ordinary_rows", ordinaryRows,
		"legacy_gzip_rows", legacyRows, "current_blocks", currentBlocks,
		"legacy_gzip_blocks", legacyBlocks, "unsupported_records", unsupported,
		"invalid_records", invalid, "coverage_gaps", gaps, "missing_sequences", missingSequences,
		"invalid_ranges", invalidRanges, "legacy_blocks_zero", legacyBlocks == 0, "verification_passed", verifyErr == nil)
	return verifyErr
}
