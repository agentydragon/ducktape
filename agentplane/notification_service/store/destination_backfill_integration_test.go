package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	model "github.com/agentydragon/ducktape/agentplane/notification_service/model"

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

// allowLegacyInboxDestinationTestRows creates a disposable legacy-schema fixture for backfill tests.
func allowLegacyInboxDestinationTestRows(t *testing.T, ctx context.Context, db *sql.DB) {
	t.Helper()
	if _, err := db.ExecContext(ctx, `ALTER TABLE inbox DROP CONSTRAINT inbox_destination_fields_valid`); err != nil {
		t.Fatal(err)
	}
}

func TestBackfillInboxDestinationsPreservesPythonRowsAndSubscriptionIdempotency(t *testing.T) {
	db, ctx := newDestinationBackfillDatabase(t)
	allowLegacyInboxDestinationTestRows(t, ctx, db)
	owner := model.ServiceAccount{Namespace: "test-namespace", Name: "test-destination-backfill"}
	ref := model.DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-uid"}
	creation := model.Subscribe{
		DestinationRef: ref, SessionID: "test-session", IdempotencyKey: "test-key", LifetimeDays: 7,
		Source: model.ActionsSource{RequestID: uuid.New(), AfterSequence: 3},
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
		inboxID, owner.Namespace, owner.Name, model.DestinationKey(ref, creation.SessionID),
		`{"namespace":"test-namespace","name":"test-sandbox","uid":"test-uid"}`, creation.SessionID); err != nil {
		t.Fatal(err)
	}
	subscriptionID := uuid.New()
	allowLegacySubscriptionCreationTestRows(t, ctx, db)
	if _, err := db.ExecContext(ctx, `INSERT INTO subscription (
		id, inbox_id, idempotency_key, creation, version, actions_after_sequence, generation, cancelled, expires_at)
		VALUES ($1, $2, $3, $4::jsonb, 1, $5, 0, false, $6)`,
		subscriptionID, inboxID, creation.IdempotencyKey, string(creationJSON), creation.Source.(model.ActionsSource).AfterSequence,
		time.Now().Add(24*time.Hour)); err != nil {
		t.Fatal(err)
	}
	store := New(db, defaultQuotas())
	if err := BackfillSubscriptionCreations(ctx, db); err != nil {
		t.Fatal(err)
	}
	if err := BackfillInboxDestinations(ctx, db); err != nil {
		t.Fatal(err)
	}
	assertInboxReadableAndIdempotent(t, ctx, store, owner, ref, creation, inboxID)
	var namespace, name, uid string
	if err := db.QueryRowContext(ctx, `SELECT destination_namespace, destination_name, destination_uid
		FROM inbox WHERE id = $1`, inboxID).Scan(&namespace, &name, &uid); err != nil {
		t.Fatal(err)
	}
	if got := (model.DestinationRef{Namespace: namespace, Name: name, UID: uid}); got != ref {
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

func assertInboxReadableAndIdempotent(
	t *testing.T, ctx context.Context, store *Store, owner model.ServiceAccount, ref model.DestinationRef, creation model.Subscribe, id uuid.UUID,
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

func TestSubscribeWritesNormalizedDestination(t *testing.T) {
	db, ctx := newDestinationBackfillDatabase(t)
	owner := model.ServiceAccount{Namespace: "test-namespace", Name: "test-destination-typed-only"}
	ref := model.DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-uid"}
	creation := model.Subscribe{
		DestinationRef: ref, SessionID: "test-session", IdempotencyKey: "test-key", LifetimeDays: 7,
		Source: model.ActionsSource{RequestID: uuid.New(), AfterSequence: 3},
	}
	if _, err := New(db, defaultQuotas()).Subscribe(ctx, owner, creation, nil); err != nil {
		t.Fatal(err)
	}
	var got model.DestinationRef
	if err := db.QueryRowContext(ctx, `SELECT destination_namespace, destination_name, destination_uid
		FROM inbox WHERE owner_namespace = $1 AND owner_name = $2`, owner.Namespace, owner.Name).
		Scan(&got.Namespace, &got.Name, &got.UID); err != nil {
		t.Fatal(err)
	}
	if got != ref {
		t.Fatalf("new inbox destination = %+v, want %+v", got, ref)
	}
	var legacyJSONIsNull bool
	if err := db.QueryRowContext(ctx, `SELECT destination_ref IS NULL FROM inbox
		WHERE owner_namespace = $1 AND owner_name = $2`, owner.Namespace, owner.Name).Scan(&legacyJSONIsNull); err != nil {
		t.Fatal(err)
	}
	if !legacyJSONIsNull {
		t.Fatal("typed-only inbox writer populated the legacy destination_ref JSON column")
	}
}

func TestBackfillInboxDestinationsResumesAfterInvalidRowAndUsesUUIDIndexes(t *testing.T) {
	db, ctx := newDestinationBackfillDatabase(t)
	allowLegacyInboxDestinationTestRows(t, ctx, db)
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

	// Simulate an old writer rollback inserting a legacy row below the completed UUID cursor. The final
	// validation must notice it, reset progress once and migrate it instead of trusting the completion bit.
	if _, err := db.ExecContext(ctx, `INSERT INTO inbox (
		id, owner_namespace, owner_name, destination_key, destination_ref, session_id, last_cursor, acknowledged,
		covered, expired_through, retired, updated_at, next_attempt, claim_until)
		VALUES ('00000000-0000-0000-0000-000000000000', 'test-namespace', 'test-old-writer', 'old-writer-key',
			'{"namespace":"test-namespace","name":"test-sandbox","uid":"test-uid"}'::jsonb,
			'old-writer-session', 0, 0, 0, 0, false, now(), now(), now())`); err != nil {
		t.Fatal(err)
	}
	if err := BackfillInboxDestinations(ctx, db); err != nil {
		t.Fatalf("backfill after old-writer insertion behind completed cursor: %v", err)
	}
	var repaired model.DestinationRef
	if err := db.QueryRowContext(ctx, `SELECT destination_namespace, destination_name, destination_uid
		FROM inbox WHERE owner_name = 'test-old-writer'`).Scan(&repaired.Namespace, &repaired.Name, &repaired.UID); err != nil {
		t.Fatal(err)
	}
	if repaired != (model.DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: "test-uid"}) {
		t.Fatalf("old-writer row after completed-cursor recovery = %+v", repaired)
	}
}

func TestValidateInboxDestinationConstraints(t *testing.T) {
	for _, test := range []struct {
		name   string
		insert func(context.Context, *sql.DB) error
		wantOK bool
	}{
		{
			name: "valid rows",
			insert: func(ctx context.Context, db *sql.DB) error {
				_, err := db.ExecContext(ctx, `INSERT INTO inbox (
					id, owner_namespace, owner_name, destination_key, destination_namespace, destination_name, destination_uid,
					session_id, last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until)
					VALUES ($1, 'test-namespace', 'valid-destination-proof', 'key', 'test-namespace', 'sandbox', 'uid',
					'session', 0, 0, 0, 0, false, now(), now(), now())`, uuid.New())
				return err
			},
			wantOK: true,
		},
		{
			name:   "empty database",
			insert: func(context.Context, *sql.DB) error { return nil },
			wantOK: true,
		},
		{
			name: "null and empty keys",
			insert: func(ctx context.Context, db *sql.DB) error {
				if _, err := db.ExecContext(ctx, `ALTER TABLE inbox DROP CONSTRAINT inbox_destination_fields_valid`); err != nil {
					return err
				}
				if _, err := db.ExecContext(ctx, `INSERT INTO inbox (
					id, owner_namespace, owner_name, destination_key, destination_ref, destination_namespace, destination_name,
					destination_uid, session_id, last_cursor, acknowledged, covered, expired_through, retired, updated_at,
					next_attempt, claim_until)
					VALUES ($1, 'test-namespace', 'invalid-destination-proof', 'null', '{}', NULL, 'sandbox', 'uid',
					'session', 0, 0, 0, 0, false, now(), now(), now()),
					($2, 'test-namespace', 'invalid-destination-proof', 'empty', '{}', 'test-namespace', '', 'uid',
					'session-2', 0, 0, 0, 0, false, now(), now(), now())`, uuid.New(), uuid.New()); err != nil {
					return err
				}
				_, err := db.ExecContext(ctx, `ALTER TABLE inbox ADD CONSTRAINT inbox_destination_fields_valid CHECK (
					destination_namespace IS NOT NULL AND char_length(destination_namespace) BETWEEN 1 AND 63 AND
					destination_name IS NOT NULL AND char_length(destination_name) BETWEEN 1 AND 63 AND
					destination_uid IS NOT NULL AND char_length(destination_uid) BETWEEN 1 AND 128
				) NOT VALID`)
				return err
			},
			wantOK: false,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			db, ctx := newDestinationBackfillDatabase(t)
			if err := test.insert(ctx, db); err != nil {
				t.Fatal(err)
			}
			err := ValidateInboxDestinationConstraints(ctx, db)
			if test.wantOK && err != nil {
				t.Fatalf("validate normalized destination proof: %v", err)
			}
			if !test.wantOK && err == nil {
				t.Fatal("validated constraints with NULL and empty destination keys")
			}
			var validated bool
			if err := db.QueryRowContext(ctx, `SELECT convalidated FROM pg_constraint
				WHERE conrelid = 'inbox'::regclass AND conname = 'inbox_destination_fields_valid'`).Scan(&validated); err != nil {
				t.Fatal(err)
			}
			if validated != test.wantOK {
				t.Fatalf("destination shape proof validated = %t, want %t", validated, test.wantOK)
			}
		})
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

func usesInboxPrimaryKeyWithoutFullScan(plan string) bool {
	return strings.Contains(plan, "inbox_pkey") && !strings.Contains(plan, "Seq Scan on inbox")
}
