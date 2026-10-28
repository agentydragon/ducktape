package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

func newDestinationBackfillDatabase(t *testing.T) (*sql.DB, context.Context) {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db, t.Context()
}

func TestBackfillInboxDestinationsPreservesPythonRowsAndSubscriptionIdempotency(t *testing.T) {
	db, ctx := newDestinationBackfillDatabase(t)
	owner := ServiceAccount{Namespace: "test-namespace", Name: "test-destination-backfill"}
	ref := DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-uid"}
	creation := Subscribe{
		DestinationRef: ref, SessionID: "test-session", IdempotencyKey: "test-key", LifetimeDays: 7,
		Source: ActionsSource{RequestID: uuid.New(), AfterSequence: 3},
	}
	creationJSON, err := json.Marshal(creation)
	if err != nil {
		t.Fatal(err)
	}
	inboxID := uuid.New()
	if _, err := db.ExecContext(ctx, `INSERT INTO inbox (
		id, owner_namespace, owner_name, destination_key, destination_ref, session_id, last_cursor, acknowledged,
		covered, expired_through, retired, updated_at, next_attempt, claim_until)
		VALUES ($1, $2, $3, $4, $5::jsonb, $6, 0, 0, 0, 0, false, now(), now(), now())`,
		inboxID, owner.Namespace, owner.Name, DestinationKey(ref, creation.SessionID),
		`{"namespace":"test-namespace","name":"test-sandbox","uid":"test-uid"}`, creation.SessionID); err != nil {
		t.Fatal(err)
	}
	subscriptionID := uuid.New()
	if _, err := db.ExecContext(ctx, `INSERT INTO subscription (
		id, inbox_id, idempotency_key, creation, version, actions_after_sequence, generation, cancelled, expires_at)
		VALUES ($1, $2, $3, $4::jsonb, 1, $5, 0, false, $6)`,
		subscriptionID, inboxID, creation.IdempotencyKey, string(creationJSON), creation.Source.(ActionsSource).AfterSequence,
		time.Now().Add(24*time.Hour)); err != nil {
		t.Fatal(err)
	}
	store := New(db, defaultQuotas())
	assertLegacyInboxReadableAndIdempotent(t, ctx, store, owner, ref, creation, inboxID)

	if err := BackfillInboxDestinations(ctx, db); err != nil {
		t.Fatal(err)
	}
	var namespace, name, uid string
	if err := db.QueryRowContext(ctx, `SELECT destination_namespace, destination_name, destination_uid
		FROM inbox WHERE id = $1`, inboxID).Scan(&namespace, &name, &uid); err != nil {
		t.Fatal(err)
	}
	if got := (DestinationRef{Namespace: namespace, Name: name, UID: uid}); got != ref {
		t.Fatalf("backfilled destination = %+v, want %+v", got, ref)
	}

	inboxes, err := store.ListInboxes(ctx, Owner{Account: owner, SandboxUID: ref.UID})
	if err != nil || len(inboxes) != 1 || inboxes[0].ID != inboxID || inboxes[0].DestinationRef != ref {
		t.Fatalf("authorized inbox list = %+v, %v", inboxes, err)
	}
	if _, err := store.Subscribe(ctx, owner, creation, nil); err != nil {
		t.Fatalf("retry of the Python creation = %v", err)
	}
	var storedUnchanged bool
	if err := db.QueryRowContext(ctx, `SELECT creation = $2::jsonb FROM subscription WHERE id = $1`,
		subscriptionID, string(creationJSON)).Scan(&storedUnchanged); err != nil {
		t.Fatal(err)
	}
	if !storedUnchanged {
		t.Fatal("destination backfill changed the retained JSON creation")
	}
	var count int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM inbox WHERE owner_namespace = $1 AND owner_name = $2`,
		owner.Namespace, owner.Name).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("idempotent retry left %d inboxes, want 1", count)
	}
}

func assertLegacyInboxReadableAndIdempotent(
	t *testing.T, ctx context.Context, store *Store, owner ServiceAccount, ref DestinationRef, creation Subscribe, id uuid.UUID,
) {
	t.Helper()
	inboxes, err := store.ListInboxes(ctx, Owner{Account: owner, SandboxUID: ref.UID})
	if err != nil || len(inboxes) != 1 || inboxes[0].ID != id || inboxes[0].DestinationRef != ref {
		t.Fatalf("authorized legacy inbox list = %+v, %v", inboxes, err)
	}
	if _, err := store.Subscribe(ctx, owner, creation, nil); err != nil {
		t.Fatalf("retry of the retained creation = %v", err)
	}
}

func TestSubscribeDualWritesNormalizedDestination(t *testing.T) {
	db, ctx := newDestinationBackfillDatabase(t)
	owner := ServiceAccount{Namespace: "test-namespace", Name: "test-destination-dual-write"}
	ref := DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-uid"}
	creation := Subscribe{
		DestinationRef: ref, SessionID: "test-session", IdempotencyKey: "test-key", LifetimeDays: 7,
		Source: ActionsSource{RequestID: uuid.New(), AfterSequence: 3},
	}
	if _, err := New(db, defaultQuotas()).Subscribe(ctx, owner, creation, nil); err != nil {
		t.Fatal(err)
	}
	var got DestinationRef
	if err := db.QueryRowContext(ctx, `SELECT destination_namespace, destination_name, destination_uid
		FROM inbox WHERE owner_namespace = $1 AND owner_name = $2`, owner.Namespace, owner.Name).
		Scan(&got.Namespace, &got.Name, &got.UID); err != nil {
		t.Fatal(err)
	}
	if got != ref {
		t.Fatalf("new inbox destination = %+v, want %+v", got, ref)
	}
}

func TestBackfillInboxDestinationsResumesAfterInvalidRowAndUsesUUIDIndexes(t *testing.T) {
	db, ctx := newDestinationBackfillDatabase(t)
	const rowCount = destinationBackfillBatchSize + 1
	if _, err := db.ExecContext(ctx, `INSERT INTO inbox (
		id, owner_namespace, owner_name, destination_key, destination_ref, session_id, last_cursor, acknowledged,
		covered, expired_through, retired, updated_at, next_attempt, claim_until)
		SELECT ('00000000-0000-0000-0000-' || lpad(to_hex(row_number), 12, '0'))::uuid,
			'test-namespace', 'test-backfill-resume', 'key-' || row_number,
			CASE WHEN row_number = $1 THEN '{"namespace":"test-namespace","name":"test-sandbox"}'::jsonb
				ELSE '{"namespace":"test-namespace","name":"test-sandbox","uid":"test-uid"}'::jsonb END,
			row_number::text, 0, 0, 0, 0, false, now(), now(), now()
		FROM generate_series(1, $1) AS rows(row_number)`, rowCount); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `INSERT INTO inbox (
		id, owner_namespace, owner_name, destination_key, destination_ref, session_id, last_cursor, acknowledged,
		covered, expired_through, retired, updated_at, next_attempt, claim_until)
		SELECT ('10000000-0000-0000-0000-' || lpad(to_hex(row_number), 12, '0'))::uuid,
			'test-namespace', 'test-backfill-plan', 'plan-' || row_number,
			jsonb_build_object('namespace', repeat('é', 63), 'name', repeat('é', 63), 'uid', repeat('é', 128)),
			row_number::text, 0, 0, 0, 0, false, now(), now(), now()
		FROM generate_series(1, 10000) AS rows(row_number)`); err != nil {
		t.Fatal(err)
	}
	if err := BackfillInboxDestinations(ctx, db); err == nil || !strings.Contains(err.Error(), "destination_ref.uid") {
		t.Fatalf("backfill error = %v, want invalid destination_ref UID", err)
	}
	var lastID sql.NullString
	var complete bool
	if err := db.QueryRowContext(ctx, `SELECT inbox_destination_last_id::text, inbox_destination_complete
		FROM notification_storage_backfill_progress WHERE singleton`).Scan(&lastID, &complete); err != nil {
		t.Fatal(err)
	}
	if !lastID.Valid || lastID.String != "00000000-0000-0000-0000-000000000200" || complete {
		t.Fatalf("progress cursor = %v, complete = %t; want 512th UUID and incomplete", lastID, complete)
	}
	var rewritten int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM inbox WHERE owner_name = 'test-backfill-resume'
		AND destination_namespace IS NOT NULL AND destination_name IS NOT NULL AND destination_uid IS NOT NULL`).Scan(&rewritten); err != nil {
		t.Fatal(err)
	}
	if rewritten != destinationBackfillBatchSize {
		t.Fatalf("committed rows after interrupted batch = %d, want %d", rewritten, destinationBackfillBatchSize)
	}
	var retainedJSON string
	if err := db.QueryRowContext(ctx, `SELECT destination_ref::text FROM inbox
		WHERE owner_name = 'test-backfill-resume' AND id = $1::uuid`,
		fmt.Sprintf("00000000-0000-0000-0000-%012x", rowCount)).Scan(&retainedJSON); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(retainedJSON, "test-sandbox") {
		t.Fatalf("invalid source row was not retained: %s", retainedJSON)
	}

	planTx, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer planTx.Rollback()
	if _, err := planTx.ExecContext(ctx, `ANALYZE inbox`); err != nil {
		t.Fatal(err)
	}
	keysetPlan, err := explainLines(ctx, planTx, inboxDestinationCandidatesAfterCursor, lastID.String, destinationBackfillBatchSize)
	if err != nil {
		t.Fatal(err)
	}
	if !usesInboxPrimaryKeyWithoutFullScan(keysetPlan) {
		t.Fatalf("UUID keyset scan did not use the primary-key index:\n%s", keysetPlan)
	}
	updatePlan, err := explainLines(ctx, planTx, updateInboxDestinationsFromBatch,
		pq.Array([]string{"10000000-0000-0000-0000-000000000001"}),
		pq.Array([]string{"test-namespace"}), pq.Array([]string{"test-sandbox"}), pq.Array([]string{"test-uid"}))
	if err != nil {
		t.Fatal(err)
	}
	if !usesInboxPrimaryKeyWithoutFullScan(updatePlan) {
		t.Fatalf("bulk update did not use the primary-key index:\n%s", updatePlan)
	}
	if err := planTx.Rollback(); err != nil {
		t.Fatal(err)
	}

	if _, err := db.ExecContext(ctx, `UPDATE inbox SET destination_ref =
		'{"namespace":"test-namespace","name":"test-sandbox","uid":"test-uid"}'::jsonb
		WHERE owner_name = 'test-backfill-resume' AND id = $1::uuid`,
		fmt.Sprintf("00000000-0000-0000-0000-%012x", rowCount)); err != nil {
		t.Fatal(err)
	}
	if err := BackfillInboxDestinations(ctx, db); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, `SELECT inbox_destination_complete FROM notification_storage_backfill_progress
		WHERE singleton`).Scan(&complete); err != nil {
		t.Fatal(err)
	}
	if !complete {
		t.Fatal("resumed destination backfill did not finish")
	}
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM inbox WHERE owner_name = 'test-backfill-resume'
		AND destination_uid IS NULL`).Scan(&rewritten); err != nil {
		t.Fatal(err)
	}
	if rewritten != 0 {
		t.Fatalf("%d destination rows remain incomplete after resume", rewritten)
	}
}

func TestBoundedDestinationCandidatesLimitsSerializedBytes(t *testing.T) {
	candidates := []destinationBackfillCandidate{
		{id: uuid.New(), serialized: destinationBackfillBatchBytes / 4},
		{id: uuid.New(), serialized: destinationBackfillBatchBytes / 4},
		{id: uuid.New(), serialized: destinationBackfillBatchBytes / 4},
		{id: uuid.New(), serialized: destinationBackfillBatchBytes / 4},
		{id: uuid.New(), serialized: destinationBackfillBatchBytes / 4},
	}
	selected, total, err := boundedDestinationCandidates(candidates)
	if err != nil {
		t.Fatal(err)
	}
	if len(selected) != 4 || total != destinationBackfillBatchBytes {
		t.Fatalf("selected %d rows totaling %d bytes, want 4 rows and %d bytes",
			len(selected), total, destinationBackfillBatchBytes)
	}
	oversized := []destinationBackfillCandidate{{id: uuid.New(), serialized: destinationBackfillMaxRowBytes + 1}}
	if _, _, err := boundedDestinationCandidates(oversized); err == nil {
		t.Fatal("accepted a single destination_ref larger than the per-row bound")
	}
}

func explainLines(ctx context.Context, tx *sql.Tx, query string, args ...any) (string, error) {
	rows, err := tx.QueryContext(ctx, "EXPLAIN (COSTS OFF) "+query, args...)
	if err != nil {
		return "", err
	}
	var plan strings.Builder
	for rows.Next() {
		var line string
		if err := rows.Scan(&line); err != nil {
			_ = rows.Close()
			return "", err
		}
		plan.WriteString(line)
		plan.WriteByte('\n')
	}
	if err := errors.Join(rows.Err(), rows.Close()); err != nil {
		return "", err
	}
	return plan.String(), nil
}

func usesInboxPrimaryKeyWithoutFullScan(plan string) bool {
	return strings.Contains(plan, "inbox_pkey") && !strings.Contains(plan, "Seq Scan on inbox")
}
