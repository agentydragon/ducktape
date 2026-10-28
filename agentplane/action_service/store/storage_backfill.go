package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

const (
	actionStorageBackfillBatchSize  = 512
	actionStorageEvidenceByteBudget = 1 << 20
	actionStorageBatchTimeout       = 30 * time.Second
)

// BackfillActionStorage converts legacy Action identity, actor and policy-evidence values into their typed
// columns. Run it as an explicit offline command after every Action Service writer dual-writes the expanded
// columns. Each batch commits independently; rerunning after interruption safely skips converted rows.
func BackfillActionStorage(ctx context.Context, db *sql.DB) error {
	if err := backfillActionIdentities(ctx, db); err != nil {
		return err
	}
	if err := backfillActionActors(ctx, db); err != nil {
		return err
	}
	if err := backfillActionPolicyEvidence(ctx, db); err != nil {
		return err
	}
	return verifyActionStorageBackfill(ctx, db)
}

func verifyActionStorageBackfill(ctx context.Context, db *sql.DB) error {
	for _, check := range []struct {
		table string
		query string
	}{
		{"action_request", `SELECT EXISTS (SELECT 1 FROM action_request
			WHERE action_group IS NULL OR action_name IS NULL)`},
		{"action_event actor", `SELECT EXISTS (SELECT 1 FROM action_event
			WHERE actor IS NOT NULL AND actor <> 'null'::jsonb AND actor_proto IS NULL)`},
		{"action_decision policy evidence", `SELECT EXISTS (SELECT 1 FROM action_decision
			WHERE policy_evidence IS NOT NULL AND policy_evidence <> 'null'::jsonb AND policy_evidence_proto IS NULL)`},
	} {
		var incomplete bool
		checkCtx, cancel := context.WithTimeout(ctx, actionStorageBatchTimeout)
		err := db.QueryRowContext(checkCtx, check.query).Scan(&incomplete)
		cancel()
		if err != nil {
			return fmt.Errorf("verify %s storage backfill: %w", check.table, err)
		}
		if incomplete {
			return fmt.Errorf("verify %s storage backfill: legacy rows remain without typed values", check.table)
		}
	}
	return nil
}

func beginActionStorageBatch(ctx context.Context, db *sql.DB) (context.Context, *sql.Tx, context.CancelFunc, error) {
	batchCtx, cancel := context.WithTimeout(ctx, actionStorageBatchTimeout)
	tx, err := db.BeginTx(batchCtx, nil)
	if err != nil {
		cancel()
		return nil, nil, nil, err
	}
	for _, statement := range []string{
		"SET LOCAL lock_timeout = '1s'",
		"SET LOCAL statement_timeout = '30s'",
	} {
		if _, err := tx.ExecContext(batchCtx, statement); err != nil {
			_ = tx.Rollback()
			cancel()
			return nil, nil, nil, err
		}
	}
	return batchCtx, tx, cancel, nil
}

func backfillActionIdentities(ctx context.Context, db *sql.DB) error {
	after := uuid.Nil
	first := true
	for {
		next, err := backfillActionIdentityBatch(ctx, db, after, first)
		if err != nil {
			return err
		}
		if next == nil {
			return nil
		}
		after, first = *next, false
	}
}

func backfillActionIdentityBatch(ctx context.Context, db *sql.DB, after uuid.UUID, first bool) (*uuid.UUID, error) {
	batchCtx, tx, cancel, err := beginActionStorageBatch(ctx, db)
	if err != nil {
		return nil, err
	}
	defer cancel()
	defer tx.Rollback()

	query := `SELECT id, action_group, action_name,
		CASE WHEN action_group IS NULL OR action_name IS NULL THEN action::text END
		FROM action_request ORDER BY id LIMIT $1 FOR UPDATE`
	var rows *sql.Rows
	if first {
		rows, err = tx.QueryContext(batchCtx, query, actionStorageBackfillBatchSize)
	} else {
		query = `SELECT id, action_group, action_name,
			CASE WHEN action_group IS NULL OR action_name IS NULL THEN action::text END
			FROM action_request WHERE id > $1::uuid ORDER BY id LIMIT $2 FOR UPDATE`
		rows, err = tx.QueryContext(batchCtx, query, after, actionStorageBackfillBatchSize)
	}
	if err != nil {
		return nil, err
	}
	var last *uuid.UUID
	var ids, groups, names []string
	for rows.Next() {
		var id uuid.UUID
		var group, name sql.NullString
		var legacyAction []byte
		if err := rows.Scan(&id, &group, &name, &legacyAction); err != nil {
			_ = rows.Close()
			return nil, err
		}
		last = &id
		if group.Valid && name.Valid {
			continue
		}
		if legacyAction == nil {
			_ = rows.Close()
			return nil, fmt.Errorf("action_request %s is missing action while typed identity is incomplete", id)
		}
		var identity ActionIdentity
		if err := json.Unmarshal(legacyAction, &identity); err != nil {
			_ = rows.Close()
			return nil, fmt.Errorf("decode action_request %s action: %w", id, err)
		}
		if group.Valid && group.String != identity.Group || name.Valid && name.String != identity.Name {
			_ = rows.Close()
			return nil, fmt.Errorf("action_request %s typed identity disagrees with legacy action", id)
		}
		ids = append(ids, id.String())
		groups = append(groups, identity.Group)
		names = append(names, identity.Name)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return nil, err
	}
	if len(ids) > 0 {
		result, err := tx.ExecContext(batchCtx, `UPDATE action_request AS request
			SET action_group = batch.action_group, action_name = batch.action_name
			FROM unnest($1::uuid[], $2::text[], $3::text[]) AS batch(id, action_group, action_name)
			WHERE request.id = batch.id AND (request.action_group IS NULL OR request.action_name IS NULL)`,
			pq.Array(ids), pq.Array(groups), pq.Array(names))
		if err != nil {
			return nil, err
		}
		if err := requireUpdatedRows(result, len(ids), "action_request typed identity"); err != nil {
			return nil, err
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return last, nil
}

func backfillActionActors(ctx context.Context, db *sql.DB) error {
	afterRequest := uuid.Nil
	afterSequence := int32(0)
	first := true
	for {
		nextRequest, nextSequence, err := backfillActionActorBatch(ctx, db, afterRequest, afterSequence, first)
		if err != nil {
			return err
		}
		if nextRequest == nil {
			return nil
		}
		afterRequest, afterSequence, first = *nextRequest, nextSequence, false
	}
}

func backfillActionActorBatch(ctx context.Context, db *sql.DB, afterRequest uuid.UUID, afterSequence int32, first bool) (*uuid.UUID, int32, error) {
	batchCtx, tx, cancel, err := beginActionStorageBatch(ctx, db)
	if err != nil {
		return nil, 0, err
	}
	defer cancel()
	defer tx.Rollback()

	query := `SELECT request_id, sequence, CASE WHEN actor_proto IS NULL AND actor IS NOT NULL
			AND actor <> 'null'::jsonb THEN actor END
		FROM action_event ORDER BY request_id, sequence LIMIT $1 FOR UPDATE`
	if !first {
		query = `SELECT request_id, sequence, CASE WHEN actor_proto IS NULL AND actor IS NOT NULL
				AND actor <> 'null'::jsonb THEN actor END
			FROM action_event WHERE (request_id, sequence) > ($1::uuid, $2::integer)
			ORDER BY request_id, sequence LIMIT $3 FOR UPDATE`
	}
	var rows *sql.Rows
	if first {
		rows, err = tx.QueryContext(batchCtx, query, actionStorageBackfillBatchSize)
	} else {
		rows, err = tx.QueryContext(batchCtx, query, afterRequest, afterSequence, actionStorageBackfillBatchSize)
	}
	if err != nil {
		return nil, 0, err
	}
	var lastRequest *uuid.UUID
	var lastSequence int32
	var requestIDs []string
	var sequences []int64
	var payloads pq.ByteaArray
	var hasPayload []bool
	for rows.Next() {
		var requestID uuid.UUID
		var sequence int32
		var legacyActor []byte
		if err := rows.Scan(&requestID, &sequence, &legacyActor); err != nil {
			_ = rows.Close()
			return nil, 0, err
		}
		lastRequest, lastSequence = &requestID, sequence
		// SQL NULL and JSON null are already the desired absent actor representation in BYTEA.
		if legacyActor == nil {
			continue
		}
		actor, err := decodeRecordedActor(legacyActor)
		if err != nil {
			_ = rows.Close()
			return nil, 0, fmt.Errorf("decode action_event %s sequence %d actor: %w", requestID, sequence, err)
		}
		encoded, err := encodeRecordedPrincipal(actor)
		if err != nil {
			_ = rows.Close()
			return nil, 0, fmt.Errorf("encode action_event %s sequence %d actor: %w", requestID, sequence, err)
		}
		requestIDs = append(requestIDs, requestID.String())
		sequences = append(sequences, int64(sequence))
		if encoded == nil {
			payloads = append(payloads, []byte{})
			hasPayload = append(hasPayload, false)
		} else {
			payloads = append(payloads, encoded)
			hasPayload = append(hasPayload, true)
		}
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return nil, 0, err
	}
	if len(requestIDs) > 0 {
		result, err := tx.ExecContext(batchCtx, `UPDATE action_event AS event SET actor_proto =
			CASE WHEN batch.has_payload THEN batch.payload ELSE NULL END
			FROM unnest($1::uuid[], $2::bigint[], $3::bytea[], $4::boolean[])
				AS batch(request_id, sequence, payload, has_payload)
			WHERE event.request_id = batch.request_id AND event.sequence = batch.sequence AND event.actor_proto IS NULL`,
			pq.Array(requestIDs), pq.Array(sequences), payloads, pq.Array(hasPayload))
		if err != nil {
			return nil, 0, err
		}
		if err := requireUpdatedRows(result, len(requestIDs), "action_event actor protobuf"); err != nil {
			return nil, 0, err
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, 0, err
	}
	return lastRequest, lastSequence, nil
}

type actionDecisionCursor struct {
	id   uuid.UUID
	need bool
	size int64
}

func backfillActionPolicyEvidence(ctx context.Context, db *sql.DB) error {
	after := uuid.Nil
	first := true
	for {
		next, err := backfillActionPolicyEvidenceBatch(ctx, db, after, first)
		if err != nil {
			return err
		}
		if next == nil {
			return nil
		}
		after, first = *next, false
	}
}

func backfillActionPolicyEvidenceBatch(ctx context.Context, db *sql.DB, after uuid.UUID, first bool) (*uuid.UUID, error) {
	batchCtx, tx, cancel, err := beginActionStorageBatch(ctx, db)
	if err != nil {
		return nil, err
	}
	defer cancel()
	defer tx.Rollback()

	query := `SELECT id, policy_evidence_proto IS NULL AND policy_evidence IS NOT NULL
			AND policy_evidence <> 'null'::jsonb,
		CASE WHEN policy_evidence_proto IS NULL AND policy_evidence IS NOT NULL
			AND policy_evidence <> 'null'::jsonb THEN octet_length(policy_evidence::text) ELSE 0 END
		FROM action_decision ORDER BY id LIMIT $1 FOR UPDATE`
	var rows *sql.Rows
	if first {
		rows, err = tx.QueryContext(batchCtx, query, actionStorageBackfillBatchSize)
	} else {
		query = `SELECT id, policy_evidence_proto IS NULL AND policy_evidence IS NOT NULL
				AND policy_evidence <> 'null'::jsonb,
			CASE WHEN policy_evidence_proto IS NULL AND policy_evidence IS NOT NULL
				AND policy_evidence <> 'null'::jsonb THEN octet_length(policy_evidence::text) ELSE 0 END
			FROM action_decision WHERE id > $1::uuid ORDER BY id LIMIT $2 FOR UPDATE`
		rows, err = tx.QueryContext(batchCtx, query, after, actionStorageBackfillBatchSize)
	}
	if err != nil {
		return nil, err
	}
	candidates := make([]actionDecisionCursor, 0, actionStorageBackfillBatchSize)
	for rows.Next() {
		var row actionDecisionCursor
		if err := rows.Scan(&row.id, &row.need, &row.size); err != nil {
			_ = rows.Close()
			return nil, err
		}
		candidates = append(candidates, row)
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return nil, err
	}
	if len(candidates) == 0 {
		if err := tx.Commit(); err != nil {
			return nil, err
		}
		return nil, nil
	}

	selected := make([]actionDecisionCursor, 0, len(candidates))
	var payloadBytes int64
	for _, row := range candidates {
		if row.need && row.size > 0 && payloadBytes+row.size > actionStorageEvidenceByteBudget && len(selected) > 0 {
			break
		}
		selected = append(selected, row)
		payloadBytes += row.size
		// A single oversized evidence document is processed alone so the cursor always makes progress.
		if row.need && row.size > actionStorageEvidenceByteBudget {
			break
		}
	}
	last := selected[len(selected)-1].id
	var ids pq.StringArray
	for _, row := range selected {
		if row.need {
			ids = append(ids, row.id.String())
		}
	}
	if len(ids) > 0 {
		payloadRows, err := tx.QueryContext(batchCtx, `SELECT decision.id, CASE WHEN decision.policy_evidence_proto IS NULL
			AND decision.policy_evidence IS NOT NULL AND decision.policy_evidence <> 'null'::jsonb
			THEN decision.policy_evidence END FROM action_decision AS decision
			JOIN unnest($1::uuid[]) WITH ORDINALITY AS batch(id, ordinal) ON decision.id = batch.id
			ORDER BY batch.ordinal`, ids)
		if err != nil {
			return nil, err
		}
		var updateIDs []string
		var payloads pq.ByteaArray
		var hasPayload []bool
		for payloadRows.Next() {
			var id uuid.UUID
			var legacyEvidence []byte
			if err := payloadRows.Scan(&id, &legacyEvidence); err != nil {
				_ = payloadRows.Close()
				return nil, err
			}
			var encoded []byte
			if document := jsonValue(legacyEvidence); document != nil {
				var evidence PolicyEvidence
				if err := json.Unmarshal(document, &evidence); err != nil {
					_ = payloadRows.Close()
					return nil, fmt.Errorf("decode action_decision %s policy_evidence: %w", id, err)
				}
				encoded, err = encodePolicyEvidence(&evidence)
				if err != nil {
					_ = payloadRows.Close()
					return nil, fmt.Errorf("encode action_decision %s policy_evidence: %w", id, err)
				}
			}
			updateIDs = append(updateIDs, id.String())
			if encoded == nil {
				payloads = append(payloads, []byte{})
				hasPayload = append(hasPayload, false)
			} else {
				payloads = append(payloads, encoded)
				hasPayload = append(hasPayload, true)
			}
		}
		if err := errors.Join(payloadRows.Err(), payloadRows.Close()); err != nil {
			return nil, err
		}
		if len(updateIDs) != len(ids) {
			return nil, fmt.Errorf("backfill action_decision policy_evidence selected %d rows but read %d", len(ids), len(updateIDs))
		}
		result, err := tx.ExecContext(batchCtx, `UPDATE action_decision AS decision SET policy_evidence_proto =
			CASE WHEN batch.has_payload THEN batch.payload ELSE NULL END
			FROM unnest($1::uuid[], $2::bytea[], $3::boolean[]) AS batch(id, payload, has_payload)
			WHERE decision.id = batch.id AND decision.policy_evidence_proto IS NULL`,
			pq.Array(updateIDs), payloads, pq.Array(hasPayload))
		if err != nil {
			return nil, err
		}
		if err := requireUpdatedRows(result, len(updateIDs), "action_decision policy evidence protobuf"); err != nil {
			return nil, err
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return &last, nil
}

func requireUpdatedRows(result sql.Result, expected int, field string) error {
	updated, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if updated != int64(expected) {
		return fmt.Errorf("backfill %s updated %d of %d selected rows", field, updated, expected)
	}
	return nil
}
