package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
)

// ValidateEntryIdentityProviderConstraint proves that every retained entry has a relational source identity.
// The migration adds the check as NOT VALID so this potentially large scan can run outside serving readiness.
func ValidateEntryIdentityProviderConstraint(ctx context.Context, db *sql.DB) error {
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
		WHERE conrelid = 'entry'::regclass AND conname = 'entry_identity_provider_required'`).Scan(&validated)
	if errors.Is(err, sql.ErrNoRows) {
		return fmt.Errorf("entry identity provider proof is missing; apply migration 00015 first")
	}
	if err != nil {
		return fmt.Errorf("inspect entry identity provider proof: %w", err)
	}
	if !validated {
		if _, err := tx.ExecContext(ctx, `ALTER TABLE entry VALIDATE CONSTRAINT entry_identity_provider_required`); err != nil {
			return fmt.Errorf("validate entry identity provider proof: %w", err)
		}
		if err := tx.QueryRowContext(ctx, `SELECT convalidated FROM pg_constraint
			WHERE conrelid = 'entry'::regclass AND conname = 'entry_identity_provider_required'`).Scan(&validated); err != nil {
			return fmt.Errorf("recheck entry identity provider proof: %w", err)
		}
		if !validated {
			return errors.New("entry identity provider validation completed without a catalog proof")
		}
	}
	return tx.Commit()
}
