package migrations

import (
	"context"
	"database/sql"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations/backfill"
)

// migrateNativeStorage is Goose version 9. DDL and phase changes commit together; data batches have
// their own durable cursors so an interrupted init resumes without holding one large transaction.
func migrateNativeStorage(ctx context.Context, db *sql.DB) error {
	var expanded bool
	if err := db.QueryRowContext(ctx, `SELECT to_regclass('notification_storage_cutover') IS NOT NULL`).Scan(&expanded); err != nil {
		return err
	}
	if !expanded {
		if err := applyNativeStoragePhase(ctx, db, "expand", "expanded"); err != nil {
			return err
		}
	}
	var phase string
	if err := db.QueryRowContext(ctx, `SELECT phase FROM notification_storage_cutover WHERE singleton`).Scan(&phase); err != nil {
		return fmt.Errorf("read native storage cutover checkpoint: %w", err)
	}
	if phase == "expanded" {
		for _, step := range []struct {
			name string
			run  func(context.Context, *sql.DB) error
		}{
			{"inbox destinations", backfill.BackfillInboxDestinations},
			{"subscription creation", backfill.BackfillSubscriptionCreations},
			{"event identities", func(ctx context.Context, db *sql.DB) error { return backfill.BackfillEventIdentities(ctx, db, 512) }},
			{"entry content", func(ctx context.Context, db *sql.DB) error { return backfill.BackfillEntryContent(ctx, db, 512) }},
		} {
			if err := step.run(ctx, db); err != nil {
				return fmt.Errorf("backfill %s: %w", step.name, err)
			}
		}
		if err := applyNativeStoragePhase(ctx, db, "fence", "fenced"); err != nil {
			return err
		}
		phase = "fenced"
	}
	if phase == "fenced" {
		if err := backfill.CleanupEntryLegacyContent(ctx, db, 512); err != nil {
			return fmt.Errorf("clean legacy entry content: %w", err)
		}
		for _, step := range []struct {
			name string
			run  func(context.Context, *sql.DB) error
		}{
			{"inbox destination", backfill.ValidateInboxDestinationConstraints},
			{"subscription creation", backfill.ValidateSubscriptionCreationConstraints},
			{"event identity", backfill.ValidateEventIdentityWriterOnly},
		} {
			if err := step.run(ctx, db); err != nil {
				return fmt.Errorf("validate %s: %w", step.name, err)
			}
		}
		return applyNativeStoragePhase(ctx, db, "contract", "complete")
	}
	if phase != "complete" {
		return fmt.Errorf("unknown native storage cutover phase %q", phase)
	}
	// Keep this completion row: Goose records version 9 only after RunDB returns. A crash or history-write
	// failure between these commits must not repeat expansion against the contracted schema.
	return nil
}

func applyNativeStoragePhase(ctx context.Context, db *sql.DB, script, nextPhase string) error {
	ddl, err := scripts.ReadFile("phases/" + script + ".sql")
	if err != nil {
		return err
	}
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, string(ddl)); err != nil {
		return fmt.Errorf("native storage %s: %w", script, err)
	}
	result, err := tx.ExecContext(ctx, `UPDATE notification_storage_cutover SET phase = $1 WHERE singleton`, nextPhase)
	if err != nil {
		return err
	}
	updated, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if updated != 1 {
		return fmt.Errorf("native storage %s updated %d cutover checkpoints, want 1", script, updated)
	}
	return tx.Commit()
}
