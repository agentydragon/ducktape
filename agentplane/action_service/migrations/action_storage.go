package migrations

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	"github.com/lib/pq"
)

const actionStorageBatchSize = 512

// migrateActionStorage runs inside Goose's version transaction. Its table locks exclude writers
// through the cutover; a failed conversion leaves both the old schema and Goose version intact.
func migrateActionStorage(ctx context.Context, tx *sql.Tx) error {
	for _, statement := range []string{
		`ALTER TABLE action_request ADD COLUMN action_group TEXT, ADD COLUMN action_name TEXT`,
		`ALTER TABLE action_event ADD COLUMN actor_proto BYTEA`,
		`ALTER TABLE action_decision ADD COLUMN policy_evidence_proto BYTEA`,
	} {
		if _, err := tx.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("expand Action storage: %w", err)
		}
	}
	if err := migrateActionIdentities(ctx, tx); err != nil {
		return err
	}
	if err := migrateActionActors(ctx, tx); err != nil {
		return err
	}
	if err := migrateActionEvidence(ctx, tx); err != nil {
		return err
	}
	// The SELECTs above visit every old row. Check the final predicate before irreversibly dropping its source.
	for _, check := range []struct{ label, query string }{
		{"action_request", `SELECT EXISTS (SELECT 1 FROM action_request WHERE action_group IS NULL OR action_name IS NULL)`},
		{"action_event", `SELECT EXISTS (SELECT 1 FROM action_event WHERE actor IS NOT NULL AND actor <> 'null'::jsonb AND actor_proto IS NULL)`},
		{"action_decision", `SELECT EXISTS (SELECT 1 FROM action_decision WHERE policy_evidence IS NOT NULL AND policy_evidence <> 'null'::jsonb AND policy_evidence_proto IS NULL)`},
	} {
		var incomplete bool
		if err := tx.QueryRowContext(ctx, check.query).Scan(&incomplete); err != nil {
			return fmt.Errorf("validate %s typed storage: %w", check.label, err)
		}
		if incomplete {
			return fmt.Errorf("validate %s typed storage: unconverted rows remain", check.label)
		}
	}
	for _, statement := range []string{
		`ALTER TABLE action_request ALTER COLUMN action_group SET NOT NULL, ALTER COLUMN action_name SET NOT NULL`,
		`ALTER TABLE action_request DROP COLUMN action`,
		`ALTER TABLE action_event DROP COLUMN actor`,
		`ALTER TABLE action_decision DROP COLUMN policy_evidence`,
	} {
		if _, err := tx.ExecContext(ctx, statement); err != nil {
			return fmt.Errorf("contract Action storage: %w", err)
		}
	}
	return nil
}

func migrateActionIdentities(ctx context.Context, tx *sql.Tx) error {
	var after any
	for {
		rows, err := tx.QueryContext(ctx, `SELECT id::text, action::text FROM action_request
			WHERE ($1::uuid IS NULL OR id > $1::uuid) ORDER BY id LIMIT $2`, after, actionStorageBatchSize)
		if err != nil {
			return fmt.Errorf("read action_request identities: %w", err)
		}
		var ids, groups, names []string
		for rows.Next() {
			var id string
			var legacy []byte
			if err := rows.Scan(&id, &legacy); err != nil {
				_ = rows.Close()
				return err
			}
			identity, err := decodeHistoricalActionIdentity(legacy)
			if err != nil {
				_ = rows.Close()
				return fmt.Errorf("action_request %s action: %w", id, err)
			}
			ids, groups, names = append(ids, id), append(groups, identity.Group), append(names, identity.Name)
			after = id
		}
		if err := errors.Join(rows.Err(), rows.Close()); err != nil {
			return fmt.Errorf("read action_request identities: %w", err)
		}
		if len(ids) == 0 {
			return nil
		}
		result, err := tx.ExecContext(ctx, `UPDATE action_request AS request
			SET action_group = batch.action_group, action_name = batch.action_name
			FROM unnest($1::uuid[], $2::text[], $3::text[]) AS batch(id, action_group, action_name)
			WHERE request.id = batch.id`, pq.Array(ids), pq.Array(groups), pq.Array(names))
		if err != nil {
			return fmt.Errorf("write action_request identities: %w", err)
		}
		if err := requireActionStorageRows(result, len(ids), "action_request identities"); err != nil {
			return err
		}
	}
}

func migrateActionActors(ctx context.Context, tx *sql.Tx) error {
	var afterRequest any
	var afterSequence int32
	for {
		rows, err := tx.QueryContext(ctx, `SELECT request_id::text, sequence, actor::text
			FROM action_event WHERE ($1::uuid IS NULL OR (request_id, sequence) > ($1::uuid, $2::integer))
			ORDER BY request_id, sequence LIMIT $3`, afterRequest, afterSequence, actionStorageBatchSize)
		if err != nil {
			return fmt.Errorf("read action_event actors: %w", err)
		}
		var ids []string
		var sequences []int64
		var payloads pq.ByteaArray
		var visited int
		for rows.Next() {
			var id string
			var sequence int32
			var legacy []byte
			if err := rows.Scan(&id, &sequence, &legacy); err != nil {
				_ = rows.Close()
				return err
			}
			visited++
			afterRequest, afterSequence = id, sequence
			if legacy == nil || string(legacy) == "null" {
				continue
			}
			payload, err := decodeHistoricalActor(legacy)
			if err != nil {
				_ = rows.Close()
				return fmt.Errorf("action_event %s sequence %d actor: %w", id, sequence, err)
			}
			ids, sequences = append(ids, id), append(sequences, int64(sequence))
			payloads = append(payloads, payload)
		}
		if err := errors.Join(rows.Err(), rows.Close()); err != nil {
			return fmt.Errorf("read action_event actors: %w", err)
		}
		if visited == 0 {
			return nil
		}
		if len(ids) == 0 {
			continue
		}
		result, err := tx.ExecContext(ctx, `UPDATE action_event AS event SET actor_proto = batch.payload
			FROM unnest($1::uuid[], $2::integer[], $3::bytea[]) AS batch(request_id, sequence, payload)
			WHERE event.request_id = batch.request_id AND event.sequence = batch.sequence`,
			pq.Array(ids), pq.Array(sequences), payloads)
		if err != nil {
			return fmt.Errorf("write action_event actors: %w", err)
		}
		if err := requireActionStorageRows(result, len(ids), "action_event actors"); err != nil {
			return err
		}
	}
}

func migrateActionEvidence(ctx context.Context, tx *sql.Tx) error {
	var after any
	for {
		rows, err := tx.QueryContext(ctx, `SELECT id::text, policy_evidence::text FROM action_decision
			WHERE ($1::uuid IS NULL OR id > $1::uuid) ORDER BY id LIMIT $2`, after, actionStorageBatchSize)
		if err != nil {
			return fmt.Errorf("read action_decision evidence: %w", err)
		}
		var ids []string
		var payloads pq.ByteaArray
		var visited int
		for rows.Next() {
			var id string
			var legacy []byte
			if err := rows.Scan(&id, &legacy); err != nil {
				_ = rows.Close()
				return err
			}
			visited++
			after = id
			if legacy == nil || string(legacy) == "null" {
				continue
			}
			payload, err := decodeHistoricalEvidence(legacy)
			if err != nil {
				_ = rows.Close()
				return fmt.Errorf("action_decision %s policy_evidence: %w", id, err)
			}
			ids, payloads = append(ids, id), append(payloads, payload)
		}
		if err := errors.Join(rows.Err(), rows.Close()); err != nil {
			return fmt.Errorf("read action_decision evidence: %w", err)
		}
		if visited == 0 {
			return nil
		}
		if len(ids) == 0 {
			continue
		}
		result, err := tx.ExecContext(ctx, `UPDATE action_decision AS decision SET policy_evidence_proto = batch.payload
			FROM unnest($1::uuid[], $2::bytea[]) AS batch(id, payload) WHERE decision.id = batch.id`,
			pq.Array(ids), payloads)
		if err != nil {
			return fmt.Errorf("write action_decision evidence: %w", err)
		}
		if err := requireActionStorageRows(result, len(ids), "action_decision evidence"); err != nil {
			return err
		}
	}
}

func requireActionStorageRows(result sql.Result, expected int, label string) error {
	count, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if count != int64(expected) {
		return fmt.Errorf("%s: updated %d of %d rows", label, count, expected)
	}
	return nil
}
