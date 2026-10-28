package store

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

func TestFinalSchemaStoresNativeContentWithoutLegacyColumns(t *testing.T) {
	f := newIsolatedFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("native-content-final-schema"))
	event := model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))
	for _, column := range []struct{ table, name string }{
		{"inbox", "destination_ref"}, {"subscription", "creation"}, {"entry", "event"},
	} {
		assertColumnExists(t, f.db, column.table, column.name, false)
	}
	for _, column := range []struct{ table, name string }{
		{"inbox", "destination_namespace"}, {"subscription", "creation_proto"}, {"entry", "identity_provider"}, {"entry", "action_at"},
	} {
		assertColumnExists(t, f.db, column.table, column.name, true)
	}
	var provider string
	var payloadIsNull, actionStatePresent, actionAtPresent bool
	var actorKind string
	if err := f.db.QueryRowContext(t.Context(), `SELECT identity_provider, payload IS NULL, action_state IS NOT NULL,
		action_at IS NOT NULL, action_actor_kind FROM entry WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID).
		Scan(&provider, &payloadIsNull, &actionStatePresent, &actionAtPresent, &actorKind); err != nil {
		t.Fatal(err)
	}
	if provider != "actions" || !payloadIsNull || !actionStatePresent || !actionAtPresent || actorKind == "" {
		t.Fatalf("native Action row = provider %q, payload NULL %t, state present %t, time present %t, actor %q",
			provider, payloadIsNull, actionStatePresent, actionAtPresent, actorKind)
	}
	page := f.read(t, subscription.InboxID, 0)
	if len(page.Entries) != 1 || page.Entries[0].Event != event {
		t.Fatalf("read final-schema event = %+v, want %+v", page.Entries, event)
	}
}
