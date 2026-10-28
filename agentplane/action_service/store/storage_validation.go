package store

import (
	"context"
	"database/sql"
	"fmt"
)

// ValidateActionStorageConstraints validates the full-table proofs required to retire the legacy JSON columns.
// Run this only as an explicit offline operation after the operator confirms every writer uses the typed-only
// Action Service image; it is intentionally not part of migrations.Up or readiness.
func ValidateActionStorageConstraints(ctx context.Context, db *sql.DB) error {
	for _, constraint := range []struct{ table, name string }{
		{"action_request", "action_request_typed_identity_present"},
		{"action_event", "action_event_actor_proto_present"},
		{"action_decision", "action_decision_policy_evidence_proto_present"},
	} {
		if err := validateActionStorageConstraint(ctx, db, constraint.table, constraint.name); err != nil {
			return err
		}
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
