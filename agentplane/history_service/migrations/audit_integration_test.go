package migrations

import (
	"encoding/json"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func TestAuditMigrationRetainsReadAndReplayIdentities(t *testing.T) {
	db := baselineDB(t)
	provider, err := schema.Provider(db)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 16); err != nil {
		t.Fatal(err)
	}
	id := uuid.New()
	if _, err := db.ExecContext(t.Context(), `INSERT INTO session_history(id,last_cursor,source_id) VALUES($1,4,'fixture-source')`, id); err != nil {
		t.Fatal(err)
	}
	entry := func(cursor uint64, kind, direction string, payload []byte) *eventlogpb.EventEntry {
		return &eventlogpb.EventEntry{Cursor: cursor, Origin: &eventlogpb.EventOrigin{SourceId: "fixture-source", Sequence: cursor}, Event: &eventpb.Event{Observation: &eventpb.Event_BoundaryRecord{BoundaryRecord: &eventpb.BoundaryRecord{Boundary: kind, Direction: direction, MediaType: "application/json", CorrelationId: "fixture-intent", RecordId: kind, FragmentCount: 1, Payload: payload}}}}
	}
	event := json.RawMessage(`{"type":"message_stop","opaque_sequence":9007199254740993}`)
	wire, err := json.Marshal(map[string]string{"wire_event": string(event)})
	if err != nil {
		t.Fatal(err)
	}
	audit, err := json.Marshal(map[string]any{"events": []json.RawMessage{event}, "response": map[string]string{"id": "fixture-message"}, "http_status": 200})
	if err != nil {
		t.Fatal(err)
	}
	first := entry(2, "compaction", "result", audit[:len(audit)/2])
	first.Event.GetBoundaryRecord().FragmentCount = 2
	last := entry(3, "compaction", "result", audit[len(audit)/2:])
	last.Event.GetBoundaryRecord().FragmentCount = 2
	last.Event.GetBoundaryRecord().FragmentIndex = 1
	progress := &eventlogpb.EventEntry{Cursor: 4, Origin: &eventlogpb.EventOrigin{SourceId: "fixture-source", Sequence: 4}, Event: &eventpb.Event{SourceSequences: []uint64{2, 3}, Observation: &eventpb.Event_ExecutionProgress{ExecutionProgress: &eventpb.ExecutionProgress{OperationId: "fixture-intent"}}}}
	before := []*eventlogpb.EventEntry{entry(1, "provider_anthropic", "received", wire), first, last, progress}
	for _, e := range before {
		raw, err := proto.Marshal(e)
		if err != nil {
			t.Fatal(err)
		}
		kind := e.Event.ProtoReflect().WhichOneof(e.Event.ProtoReflect().Descriptor().Oneofs().ByName("observation")).Number()
		if _, err := db.ExecContext(t.Context(), `INSERT INTO session_event(session_id,cursor,payload,kind) VALUES($1,$2,$3,$4)`, id, e.Cursor, raw, int32(kind)); err != nil {
			t.Fatal(err)
		}
	}
	for range 2 {
		if err := Up(t.Context(), db); err != nil {
			t.Fatal(err)
		}
	}
	history := storage.NewStore(db, storage.Options{}).WithReadScope(storage.AllSessions())
	page, err := history.ReadPage(t.Context(), id, 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if page.LastCursor != 4 || len(page.Entries) != 4 {
		t.Fatalf("cursor prefix changed: %v", page)
	}
	if !proto.Equal(before[0], page.Entries[0]) || !proto.Equal(progress, page.Entries[3]) {
		t.Fatal("provider event or source links changed")
	}
	got := append(append([]byte(nil), page.Entries[1].Event.GetBoundaryRecord().Payload...), page.Entries[2].Event.GetBoundaryRecord().Payload...)
	if string(got) != `{"http_status":200,"response":{"id":"fixture-message"}}` {
		t.Fatalf("compacted audit: %s", got)
	}
	for i := 1; i <= 2; i++ {
		metadata := proto.Clone(page.Entries[i]).(*eventlogpb.EventEntry)
		metadata.Event.GetBoundaryRecord().Payload = before[i].Event.GetBoundaryRecord().Payload
		if !proto.Equal(before[i], metadata) {
			t.Fatal("fragment identity changed")
		}
	}
	if _, err := history.Append(t.Context(), id, page.Entries); err != nil {
		t.Fatalf("rewritten source replay conflicts: %v", err)
	}
}
