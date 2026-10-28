package store

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

func TestFinalEventIdentityHasValidatedRelationalKeys(t *testing.T) {
	f := newIsolatedFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("event-identity-final-schema"))
	event := model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))

	var indexCount int
	if err := f.db.QueryRowContext(t.Context(), `SELECT count(*) FROM pg_index i
		JOIN pg_class c ON c.oid = i.indexrelid
		WHERE i.indrelid = 'entry'::regclass AND c.relname LIKE 'entry_identity_%_key'
		AND i.indisvalid AND i.indisready AND i.indisunique`).Scan(&indexCount); err != nil {
		t.Fatal(err)
	}
	if indexCount != 6 {
		t.Fatalf("valid relational identity indexes = %d, want 6", indexCount)
	}
	var identityShapeValidated, messageBodyLimitValidated bool
	if err := f.db.QueryRowContext(t.Context(), `SELECT
		(SELECT convalidated FROM pg_constraint WHERE conrelid = 'entry'::regclass AND conname = 'entry_identity_shape'),
		(SELECT convalidated FROM pg_constraint WHERE conrelid = 'entry'::regclass AND conname = 'entry_message_body_bytes')`).
		Scan(&identityShapeValidated, &messageBodyLimitValidated); err != nil {
		t.Fatal(err)
	}
	if !identityShapeValidated || !messageBodyLimitValidated {
		t.Fatalf("identity shape and Message body constraints validated = %t/%t", identityShapeValidated, messageBodyLimitValidated)
	}

	_, err := f.db.ExecContext(t.Context(), `UPDATE entry SET actions_sequence = NULL WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID)
	requireConstraint(t, err, "23514", "entry_identity_shape")
	_, err = f.db.ExecContext(t.Context(), `INSERT INTO entry
		(inbox_id, cursor, identity_provider, actions_request_id, actions_sequence, created_at)
		VALUES ($1, 2, 'actions', $2, 1, now())`, subscription.InboxID, event.RequestID)
	requireConstraint(t, err, "23505", "entry_identity_actions_key")
}
