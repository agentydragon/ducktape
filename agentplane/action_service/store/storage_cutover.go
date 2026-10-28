package store

import (
	"context"
	"database/sql"
	"fmt"
)

// ValidateActionStorageCutover validates the full-table proofs required to retire the legacy JSON columns and
// records the operator's typed-only-writer attestation. Run this only as an explicit offline operation after every
// writer uses the typed-only Action Service image; it is intentionally not part of migrations.Up or readiness.
func ValidateActionStorageCutover(ctx context.Context, db *sql.DB) error {
	for _, constraint := range []struct{ table, name string }{
		{"action_request", "action_request_typed_identity_present"},
		{"action_event", "action_event_actor_proto_present"},
		{"action_decision", "action_decision_policy_evidence_proto_present"},
	} {
		if err := validateActionStorageConstraint(ctx, db, constraint.table, constraint.name); err != nil {
			return err
		}
	}

	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, "SET LOCAL lock_timeout = '1s'"); err != nil {
		return fmt.Errorf("set Action cutover attestation lock timeout: %w", err)
	}
	result, err := tx.ExecContext(ctx, `UPDATE action_storage_cutover SET typed_only_writers_at = clock_timestamp()
		WHERE singleton IS TRUE`)
	if err != nil {
		return fmt.Errorf("record Action typed-only writer attestation: %w", err)
	}
	if changed, err := result.RowsAffected(); err != nil {
		return fmt.Errorf("count Action typed-only writer attestation: %w", err)
	} else if changed != 1 {
		return fmt.Errorf("record Action typed-only writer attestation: updated %d rows, want 1", changed)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit Action storage validation: %w", err)
	}
	return nil
}

func validateActionStorageConstraint(ctx context.Context, db *sql.DB, table, name string) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, "SET LOCAL lock_timeout = '1s'"); err != nil {
		return fmt.Errorf("set %s.%s lock timeout: %w", table, name, err)
	}
	if _, err := tx.ExecContext(ctx, "ALTER TABLE "+table+" VALIDATE CONSTRAINT "+name); err != nil {
		return fmt.Errorf("validate %s.%s: %w", table, name, err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit validation of %s.%s: %w", table, name, err)
	}
	return nil
}
