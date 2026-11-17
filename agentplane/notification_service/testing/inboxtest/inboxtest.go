// Package inboxtest writes inbox rows directly, for integration tests of the inbox store and its consumers.
package inboxtest

import (
	"database/sql"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// Insert writes an inbox row for the destination owned by identity, and returns its ID.
func Insert(t testing.TB, db *sql.DB, key string, ref model.DestinationRef, sessionID string, identity model.Identity) uuid.UUID {
	t.Helper()
	id := uuid.New()
	if _, err := db.ExecContext(t.Context(), `
		INSERT INTO inbox (id, owner_namespace, owner_name, destination_key,
			destination_namespace, destination_name, destination_uid, session_id,
			last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until,
			owner_identity_name, owner_identity_uid)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0, 0, 0, 0, false, now(), now(), now(), $9, $10)`,
		id, ref.Namespace, ref.Name, key, ref.Namespace, ref.Name, ref.UID, sessionID, identity.Name, identity.UID); err != nil {
		t.Fatal(err)
	}
	return id
}
