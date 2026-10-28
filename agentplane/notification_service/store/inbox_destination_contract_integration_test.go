package store

import (
	"errors"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

func TestInboxDestinationContractPromotesRelationalFieldsAndRetiresJSON(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)

	var legacyColumnExists bool
	if err := db.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM information_schema.columns
		WHERE table_schema = current_schema() AND table_name = 'inbox' AND column_name = 'destination_ref'
	)`).Scan(&legacyColumnExists); err != nil {
		t.Fatal(err)
	}
	if legacyColumnExists {
		t.Fatal("contract migration retained inbox.destination_ref")
	}
	for _, column := range []string{"destination_namespace", "destination_name", "destination_uid"} {
		var required bool
		if err := db.QueryRowContext(ctx, `SELECT attnotnull FROM pg_attribute
			WHERE attrelid = 'inbox'::regclass AND attname = $1 AND NOT attisdropped`, column).Scan(&required); err != nil {
			t.Fatalf("inspect inbox.%s: %v", column, err)
		}
		if !required {
			t.Errorf("inbox.%s is nullable after the contract migration", column)
		}
	}
	var shapeProofValidated bool
	if err := db.QueryRowContext(ctx, `SELECT convalidated FROM pg_constraint
		WHERE conrelid = 'inbox'::regclass AND conname = 'inbox_destination_fields_valid'`).Scan(&shapeProofValidated); err != nil {
		t.Fatal(err)
	}
	if !shapeProofValidated {
		t.Fatal("contract migration did not preserve a validated destination shape constraint")
	}
	var destinationProgressColumns int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM information_schema.columns
		WHERE table_schema = current_schema() AND table_name = 'notification_storage_backfill_progress'
		AND column_name IN ('inbox_destination_last_id', 'inbox_destination_complete')`).Scan(&destinationProgressColumns); err != nil {
		t.Fatal(err)
	}
	if destinationProgressColumns != 0 {
		t.Fatalf("destination backfill checkpoint columns remain: %d", destinationProgressColumns)
	}
}

func TestInboxDestinationContractRejectsNullEmptyAndOutOfRangeKeys(t *testing.T) {
	tests := []struct {
		caseName        string
		namespace       any
		destinationName any
		uid             any
		wantCode        string
		wantConstraint  string
	}{
		{
			caseName: "null namespace", namespace: nil, destinationName: "sandbox", uid: "uid",
			wantCode: "23502",
		},
		{
			caseName: "empty name", namespace: "namespace", destinationName: "", uid: "uid",
			wantCode: "23514", wantConstraint: "inbox_destination_fields_valid",
		},
		{
			caseName: "oversized UID", namespace: "namespace", destinationName: "sandbox", uid: strings.Repeat("u", 129),
			wantCode: "23514", wantConstraint: "inbox_destination_fields_valid",
		},
	}
	db, ctx := newStorageIntegrationDatabase(t)
	for _, test := range tests {
		t.Run(test.caseName, func(t *testing.T) {
			_, err := db.ExecContext(ctx, `INSERT INTO inbox (
				id, owner_namespace, owner_name, destination_key, destination_namespace, destination_name, destination_uid,
				session_id, last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until)
				VALUES ($1, 'destination-contract-test', 'owner', $2, $3, $4, $5, 'session', 0, 0, 0, 0, false, now(), now(), now())`,
				uuid.New(), test.caseName, test.namespace, test.destinationName, test.uid)
			var postgresError *pq.Error
			if !errors.As(err, &postgresError) {
				t.Fatalf("insert error = %v, want a PostgreSQL constraint violation", err)
			}
			if string(postgresError.Code) != test.wantCode || postgresError.Constraint != test.wantConstraint {
				t.Fatalf("insert error = (%s, %s), want (%s, %s)", postgresError.Code, postgresError.Constraint,
					test.wantCode, test.wantConstraint)
			}
		})
	}
}

func TestInboxDestinationContractGatesPopulatedRowsAndPreservesIdentity(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	if _, err := db.ExecContext(ctx, `DROP SCHEMA public CASCADE; CREATE SCHEMA public`); err != nil {
		t.Fatalf("reset cloned test database schema: %v", err)
	}
	if err := migrations.UpTo(ctx, db, 19); err != nil {
		t.Fatalf("build schema through migration 00019: %v", err)
	}
	const ownerNamespace = "destination-contract-gate"
	const ownerName = "owner"
	const destinationKey = "python-compatible-key"
	const sessionID = "session-before-contract"
	inboxID := uuid.New()

	// The predecessor schema has a NOT VALID proof. Drop it for the missing-proof case, then recreate it without
	// validation to prove both populated cutover gates against the production migration provider.
	if _, err := db.ExecContext(ctx, `ALTER TABLE inbox DROP CONSTRAINT inbox_destination_fields_valid`); err != nil {
		t.Fatalf("remove predecessor proof for missing-proof case: %v", err)
	}
	if _, err := db.ExecContext(ctx, `INSERT INTO inbox (
		id, owner_namespace, owner_name, destination_key, destination_ref, destination_namespace, destination_name,
		destination_uid, session_id, last_cursor, acknowledged, covered, expired_through, retired,
		updated_at, next_attempt, claim_until)
		VALUES ($1, $2, $3, $4, '{"namespace":"typed-ns","name":"typed-name","uid":"typed-uid"}'::jsonb,
			'typed-ns', 'typed-name', 'typed-uid', $5, 0, 0, 0, 0, false, now(), now(), now())`,
		inboxID, ownerNamespace, ownerName, destinationKey, sessionID); err != nil {
		t.Fatalf("insert pre-contract inbox row: %v", err)
	}

	if err := migrations.UpTo(ctx, db, 20); err == nil || !strings.Contains(err.Error(), "shape constraint is missing") {
		t.Fatalf("contract migration error = %v, want missing-proof gate", err)
	}
	var migrationApplied bool
	if err := db.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM notification_service_goose_version WHERE version_id = 20 AND is_applied
	)`).Scan(&migrationApplied); err != nil {
		t.Fatal(err)
	}
	if migrationApplied {
		t.Fatal("failed contract gate marked migration 00020 applied")
	}
	var retainedJSON string
	var retainedKey, retainedSession string
	if err := db.QueryRowContext(ctx, `SELECT destination_ref::text, destination_key, session_id
		FROM inbox WHERE id = $1`, inboxID).Scan(&retainedJSON, &retainedKey, &retainedSession); err != nil {
		t.Fatalf("read retained pre-contract row after missing-proof rejection: %v", err)
	}
	if !strings.Contains(retainedJSON, "typed-uid") || retainedKey != destinationKey || retainedSession != sessionID {
		t.Fatalf("missing-proof rejection changed retained row: JSON=%q key=%q session=%q", retainedJSON, retainedKey, retainedSession)
	}
	if _, err := db.ExecContext(ctx, `ALTER TABLE inbox ADD CONSTRAINT inbox_destination_fields_valid CHECK (
		destination_namespace IS NOT NULL AND char_length(destination_namespace) BETWEEN 1 AND 63 AND
		destination_name IS NOT NULL AND char_length(destination_name) BETWEEN 1 AND 63 AND
		destination_uid IS NOT NULL AND char_length(destination_uid) BETWEEN 1 AND 128
	) NOT VALID`); err != nil {
		t.Fatalf("restore the expanded migration's NOT VALID proof: %v", err)
	}
	if err := migrations.UpTo(ctx, db, 20); err == nil || !strings.Contains(err.Error(), "unvalidated on a populated database") {
		t.Fatalf("contract migration error = %v, want populated unvalidated-proof gate", err)
	}
	if err := db.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM notification_service_goose_version WHERE version_id = 20 AND is_applied
	)`).Scan(&migrationApplied); err != nil {
		t.Fatal(err)
	}
	if migrationApplied {
		t.Fatal("failed populated-row gate marked migration 00020 applied")
	}
	if err := db.QueryRowContext(ctx, `SELECT destination_ref::text, destination_key, session_id
		FROM inbox WHERE id = $1`, inboxID).Scan(&retainedJSON, &retainedKey, &retainedSession); err != nil {
		t.Fatalf("read retained pre-contract row after rejected migration: %v", err)
	}
	if !strings.Contains(retainedJSON, "typed-uid") || retainedKey != destinationKey || retainedSession != sessionID {
		t.Fatalf("rejected migration changed retained row: JSON=%q key=%q session=%q", retainedJSON, retainedKey, retainedSession)
	}
	var proofValidated bool
	if err := db.QueryRowContext(ctx, `SELECT convalidated FROM pg_constraint
		WHERE conrelid = 'inbox'::regclass AND conname = 'inbox_destination_fields_valid'`).Scan(&proofValidated); err != nil {
		t.Fatal(err)
	}
	if proofValidated {
		t.Fatal("rejected migration unexpectedly validated the populated destination constraint")
	}

	if _, err := db.ExecContext(ctx, `ALTER TABLE inbox VALIDATE CONSTRAINT inbox_destination_fields_valid`); err != nil {
		t.Fatalf("validate populated destination proof after offline gate: %v", err)
	}
	if err := migrations.Up(ctx, db); err != nil {
		t.Fatalf("apply contract after catalog proof is validated: %v", err)
	}
	var gotKey, gotNamespace, gotName, gotUID, gotSession string
	if err := db.QueryRowContext(ctx, `SELECT destination_key, destination_namespace, destination_name, destination_uid, session_id
		FROM inbox WHERE id = $1`, inboxID).Scan(&gotKey, &gotNamespace, &gotName, &gotUID, &gotSession); err != nil {
		t.Fatalf("read retained typed inbox row after contract: %v", err)
	}
	if gotKey != destinationKey || gotNamespace != "typed-ns" || gotName != "typed-name" || gotUID != "typed-uid" || gotSession != sessionID {
		t.Fatalf("contract changed inbox identity/session: key=%q destination=(%q,%q,%q) session=%q",
			gotKey, gotNamespace, gotName, gotUID, gotSession)
	}
	var legacyColumnExists bool
	if err := db.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM information_schema.columns
		WHERE table_schema = current_schema() AND table_name = 'inbox' AND column_name = 'destination_ref'
	)`).Scan(&legacyColumnExists); err != nil {
		t.Fatal(err)
	}
	if legacyColumnExists {
		t.Fatal("successful contract migration retained inbox.destination_ref")
	}
}
