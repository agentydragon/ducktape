package centralsampler

import (
	"encoding/json"
	"strings"
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func TestAuditMigrationAtomicallyRewritesIntentAndJournal(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	intentID := uuid.NewString()
	event := map[string]any{"type": "fixture.delta", "delta": strings.Repeat("fixture ", 2000), "opaque_sequence": json.Number("9007199254740993")}
	provider, err := json.Marshal(event)
	if err != nil {
		t.Fatal(err)
	}
	audit, err := json.Marshal(map[string]any{"events": []any{event}, "response": map[string]string{"id": "fixture-response"}})
	if err != nil {
		t.Fatal(err)
	}
	base := session.Cursor
	entry := func(cursor uint64, kind string, payload []byte) *eventlogpb.EventEntry {
		return &eventlogpb.EventEntry{Cursor: cursor, Origin: &eventlogpb.EventOrigin{SourceId: session.SourceID, Sequence: cursor}, Event: &eventpb.Event{Observation: &eventpb.Event_BoundaryRecord{BoundaryRecord: &eventpb.BoundaryRecord{Boundary: kind, Direction: map[string]string{"provider": "received", "sampling": "result"}[kind], MediaType: "application/json", CorrelationId: intentID, RecordId: kind, FragmentCount: 1, Payload: payload}}}}
	}
	before := []*eventlogpb.EventEntry{entry(base+1, "provider", provider), entry(base+2, "sampling", audit)}
	migrationExec(t, store.DB(), `INSERT INTO sampler_intent(id,conversation_id,generation,request,result) VALUES($1,$2,0,'{"kind":"sampling"}','{"fixture_mismatch":true}')`, intentID, session.ConversationID)
	for _, e := range before {
		raw, err := proto.Marshal(e)
		if err != nil {
			t.Fatal(err)
		}
		wire, err := encodeStoredEvent(raw)
		if err != nil {
			t.Fatal(err)
		}
		migrationExec(t, store.DB(), `INSERT INTO sampler_event(session_id,sequence,entry) VALUES($1,$2,$3)`, session.ID, e.Cursor, wire)
	}
	migrationExec(t, store.DB(), `UPDATE sampler_session SET cursor=$2 WHERE id=$1`, session.ID, base+2)
	migrationExec(t, store.DB(), `INSERT INTO sampler_export_checkpoint(session_id,destination,cursor) VALUES($1,'fixture-history',$2)`, session.ID, base+2)
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id=14`)
	if err := Migrate(t.Context(), store.DB()); err == nil {
		t.Fatal("accepted intent/journal disagreement")
	}
	got, err := store.Events(t.Context(), session.ID, base, 10)
	if err != nil {
		t.Fatal(err)
	}
	for i, want := range before {
		if !proto.Equal(got[i], want) {
			t.Fatal("failed record transaction changed its journal")
		}
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version WHERE version_id=14 AND is_applied`); count != 0 {
		t.Fatal("failed migration recorded completion")
	}
	migrationExec(t, store.DB(), `UPDATE sampler_intent SET result=$2 WHERE id=$1`, intentID, string(audit))
	for range 2 {
		if err := Migrate(t.Context(), store.DB()); err != nil {
			t.Fatal(err)
		}
	}
	got, err = store.Events(t.Context(), session.ID, base, 10)
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(got[0], before[0]) {
		t.Fatal("provider evidence changed")
	}
	want := proto.Clone(before[1]).(*eventlogpb.EventEntry)
	want.Event.GetBoundaryRecord().Payload = []byte(`{"response":{"id":"fixture-response"}}`)
	if !proto.Equal(got[1], want) {
		t.Fatalf("rewritten journal %v", got[1])
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_intent WHERE id=$1 AND result='{"response":{"id":"fixture-response"}}'::jsonb`, intentID); count != 1 {
		t.Fatal("intent result differs from journal")
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_export_checkpoint WHERE session_id=$1 AND cursor=$2`, session.ID, base+2); count != 1 {
		t.Fatal("export checkpoint changed")
	}
}
