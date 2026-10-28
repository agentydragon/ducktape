package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
)

var subscriptionCreationConstraints = []string{
	"subscription_creation_storage_pair",
	"subscription_source_provider",
	"subscription_typed_source_state",
	"subscription_creation_typed_required",
}

// ValidateSubscriptionCreationConstraints proves the typed storage invariants after every writer has moved to
// typed-only storage. Each constraint is validated and committed independently so a long scan does not hold a
// transaction open across other proofs. It is safe to rerun after interruption.
func ValidateSubscriptionCreationConstraints(ctx context.Context, db *sql.DB) error {
	return validateStorageConstraints(ctx, db, "subscription", subscriptionCreationConstraints)
}

func validateStorageConstraints(ctx context.Context, db *sql.DB, table string, constraints []string) error {
	for _, constraint := range constraints {
		if err := validateStorageConstraint(ctx, db, table, constraint); err != nil {
			return fmt.Errorf("validate %s constraint %s: %w", table, constraint, err)
		}
	}
	return nil
}

func validateStorageConstraint(ctx context.Context, db *sql.DB, table, constraint string) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, `SET LOCAL lock_timeout = '1s'`); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, `SET LOCAL statement_timeout = '30min'`); err != nil {
		return err
	}

	var validated bool
	err = tx.QueryRowContext(ctx, `SELECT convalidated FROM pg_constraint
		WHERE conrelid = $1::regclass AND conname = $2`, table, constraint).Scan(&validated)
	if errors.Is(err, sql.ErrNoRows) {
		return fmt.Errorf("constraint is missing; apply the typed-only storage migration first")
	}
	if err != nil {
		return fmt.Errorf("inspect validation state: %w", err)
	}
	if !validated {
		// Table and constraint are selected only from static allowlists above, so they cannot contain user input.
		if _, err := tx.ExecContext(ctx, `ALTER TABLE `+table+` VALIDATE CONSTRAINT `+constraint); err != nil {
			return fmt.Errorf("run VALIDATE CONSTRAINT: %w", err)
		}
		if err := tx.QueryRowContext(ctx, `SELECT convalidated FROM pg_constraint
			WHERE conrelid = $1::regclass AND conname = $2`, table, constraint).Scan(&validated); err != nil {
			return fmt.Errorf("recheck validation state: %w", err)
		}
		if !validated {
			return errors.New("constraint validation completed without a validated catalog proof")
		}
	}
	return tx.Commit()
}
