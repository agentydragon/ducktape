package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
)

var eventIdentityWriterOnlyConstraints = []string{
	"entry_identity_shape",
	"entry_identity_provider_required",
	"entry_message_body_ceiling_relational",
}

// ValidateEventIdentityWriterOnly proves the retained identity rows and relational Message JSON bound before the
// identity contract drops event JSON. These potentially large checks run outside the serving Pod init path.
func ValidateEventIdentityWriterOnly(ctx context.Context, db *sql.DB) error {
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

	var phase string
	err = tx.QueryRowContext(ctx, `SELECT phase FROM notification_event_identity_backfill WHERE singleton`).Scan(&phase)
	if errors.Is(err, sql.ErrNoRows) {
		return errors.New("event identity backfill checkpoint is missing; apply migrations through 00022 before validation")
	}
	if err != nil {
		return fmt.Errorf("inspect event identity backfill phase: %w", err)
	}
	if phase != "indexed" && phase != "writer_only" {
		return fmt.Errorf("event identity indexes are not ready for writer-only validation (phase %q); complete backfill-event-identities first", phase)
	}
	var validIndexCount int
	if err := tx.QueryRowContext(ctx, `SELECT count(*) FROM pg_index i
		JOIN pg_class c ON c.oid = i.indexrelid
		JOIN pg_namespace n ON n.oid = c.relnamespace
		WHERE i.indrelid = 'entry'::regclass
		AND n.nspname = current_schema() AND c.relname IN (
			'entry_identity_actions_key', 'entry_identity_github_key', 'entry_identity_github_observation_key',
			'entry_identity_mergeability_sequence_key', 'entry_identity_mergeability_snapshot_key',
			'entry_identity_message_key')
		AND i.indisvalid AND i.indisready AND i.indisunique`).Scan(&validIndexCount); err != nil {
		return fmt.Errorf("inspect event identity natural-key indexes: %w", err)
	}
	if validIndexCount != 6 {
		return fmt.Errorf("event identity writer-only validation requires all 6 natural-key indexes; found %d valid and unique indexes", validIndexCount)
	}

	validated, err := readEventIdentityConstraintProofs(ctx, tx)
	if err != nil {
		return err
	}
	var actions []string
	for _, name := range eventIdentityWriterOnlyConstraints {
		ok, found := validated[name]
		if !found {
			return fmt.Errorf("event identity writer-only proof %s is missing; apply migration 00022 first", name)
		}
		if !ok {
			actions = append(actions, "VALIDATE CONSTRAINT "+name)
		}
	}
	if len(actions) > 0 {
		if _, err := tx.ExecContext(ctx, "ALTER TABLE entry "+strings.Join(actions, ", ")); err != nil {
			return fmt.Errorf("validate retained event identity and Message proofs: %w", err)
		}
	}
	validated, err = readEventIdentityConstraintProofs(ctx, tx)
	if err != nil {
		return err
	}
	for _, name := range eventIdentityWriterOnlyConstraints {
		if !validated[name] {
			return fmt.Errorf("event identity writer-only validation completed without a catalog proof for %s", name)
		}
	}

	if phase == "indexed" {
		result, err := tx.ExecContext(ctx, `UPDATE notification_event_identity_backfill SET phase = 'writer_only'
			WHERE singleton AND phase = 'indexed'`)
		if err != nil {
			return fmt.Errorf("record event identity writer-only completion: %w", err)
		}
		updated, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if updated != 1 {
			return fmt.Errorf("record event identity writer-only completion updated %d checkpoint rows, want 1", updated)
		}
	}
	return tx.Commit()
}

func readEventIdentityConstraintProofs(ctx context.Context, tx *sql.Tx) (map[string]bool, error) {
	rows, err := tx.QueryContext(ctx, `SELECT conname, convalidated FROM pg_constraint
		WHERE conrelid = 'entry'::regclass AND conname IN (
			'entry_identity_shape', 'entry_identity_provider_required', 'entry_message_body_ceiling_relational')`)
	if err != nil {
		return nil, fmt.Errorf("inspect event identity writer-only proofs: %w", err)
	}
	validated := make(map[string]bool, len(eventIdentityWriterOnlyConstraints))
	for rows.Next() {
		var name string
		var proof bool
		if err := rows.Scan(&name, &proof); err != nil {
			rows.Close()
			return nil, err
		}
		validated[name] = proof
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return nil, err
	}
	return validated, nil
}
