package store

import (
	"errors"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

func TestFinalSchemaUsesRelationalDestinationFields(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)

	for _, column := range []string{"destination_namespace", "destination_name", "destination_uid"} {
		var required bool
		if err := db.QueryRowContext(ctx, `SELECT attnotnull FROM pg_attribute
			WHERE attrelid = 'inbox'::regclass AND attname = $1 AND NOT attisdropped`, column).Scan(&required); err != nil {
			t.Fatalf("inspect inbox.%s: %v", column, err)
		}
		if !required {
			t.Errorf("inbox.%s is nullable", column)
		}
	}
	var shapeProofValidated bool
	if err := db.QueryRowContext(ctx, `SELECT convalidated FROM pg_constraint
		WHERE conrelid = 'inbox'::regclass AND conname = 'inbox_destination_fields_valid'`).Scan(&shapeProofValidated); err != nil {
		t.Fatal(err)
	}
	if !shapeProofValidated {
		t.Fatal("destination shape constraint is not validated")
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
