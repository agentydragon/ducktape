package inbox

import (
	"errors"
	"testing"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

func TestSessionKeyContractRetiredTheSandboxColumns(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)

	for _, column := range []string{
		"owner_namespace", "owner_name", "destination_key",
		"destination_namespace", "destination_name", "destination_uid",
	} {
		var present bool
		if err := db.QueryRowContext(ctx, `SELECT EXISTS (
			SELECT 1 FROM pg_attribute
			WHERE attrelid = 'inbox'::regclass AND attname = $1 AND NOT attisdropped)`, column).Scan(&present); err != nil {
			t.Fatalf("inspect inbox.%s: %v", column, err)
		}
		if present {
			t.Errorf("inbox.%s survived the Session key contract", column)
		}
	}
	for _, constraint := range []string{"inbox_destination_fields_valid", "inbox_owner_namespace_owner_name_destination_key_key"} {
		var present bool
		if err := db.QueryRowContext(ctx, `SELECT EXISTS (
			SELECT 1 FROM pg_constraint WHERE conrelid = 'inbox'::regclass AND conname = $1)`, constraint).Scan(&present); err != nil {
			t.Fatalf("inspect constraint %s: %v", constraint, err)
		}
		if present {
			t.Errorf("constraint %s survived the Session key contract", constraint)
		}
	}

	// The Session key alone refuses a second inbox, which is what the retired composite UNIQUE could not do
	// once a replacement ServiceAccount left its columns NULL.
	_, session := uuid.New(), uuid.NewString()
	insert := func() error {
		_, err := db.ExecContext(ctx, `INSERT INTO inbox (
			id, session_id, last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until,
			owner_identity_name, owner_identity_uid)
			VALUES ($1, $2, 0, 0, 0, 0, false, now(), now(), now(), $3, $4)`,
			uuid.New(), session, "contract-identity", "contract-identity-uid")
		return err
	}
	if err := insert(); err != nil {
		t.Fatalf("insert Session-keyed inbox: %v", err)
	}
	var postgresError *pq.Error
	if err := insert(); !errors.As(err, &postgresError) || postgresError.Code != "23505" || postgresError.Constraint != "inbox_session_id_unique" {
		t.Fatalf("second inbox error = %v, want inbox_session_id_unique violation 23505", err)
	}
}
