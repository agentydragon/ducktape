package reads

import (
	"encoding/json"
	"errors"
	"math"
	"slices"
	"strconv"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
)

// More deltas than a fold page, so the folded Thread comes from several batches.
const deltas = testPageSize + 2

// foldedReply is a Session whose reply streamed as deltas, folded in full.
func foldedReply(t *testing.T, f fixture) uuid.UUID {
	t.Helper()
	entries := []*eventlogpb.EventEntry{turnStarted(1), replyStarted(2)}
	for index := range deltas {
		entries = append(entries, replyDelta(uint64(3+index), strconv.Itoa(index%10)))
	}
	sessionID := f.session(t, f.history, sandboxName("thread-"), entries...)
	f.fold(t, sessionID)
	return sessionID
}

func entityRange(start uint64, limit uint32) *historypb.ReadThreadEntitiesRequest_Range {
	return &historypb.ReadThreadEntitiesRequest_Range{Range: &historypb.EntityRange{StartEntityIndex: start, Limit: limit}}
}

func entityKeys(keys ...*historypb.EntityKey) *historypb.ReadThreadEntitiesRequest_Keys {
	return &historypb.ReadThreadEntitiesRequest_Keys{Keys: &historypb.EntityKeys{Keys: keys}}
}

func readEntities(t *testing.T, f fixture, request *historypb.ReadThreadEntitiesRequest) *historypb.ReadThreadEntitiesResponse {
	t.Helper()
	response, err := f.reader.ReadThreadEntities(t.Context(), request)
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func views(t *testing.T, response *historypb.ReadThreadEntitiesResponse) []map[string]any {
	t.Helper()
	var decoded []map[string]any
	for _, entity := range response.Entities {
		var view map[string]any
		if err := json.Unmarshal([]byte(entity.ViewJson), &view); err != nil {
			t.Fatal(err)
		}
		decoded = append(decoded, view)
	}
	return decoded
}

func TestARangeWindowReadsBackTheFoldedStream(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedReply(t, f)

	window := readEntities(t, f, &historypb.ReadThreadEntitiesRequest{
		SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Window: entityRange(0, 200),
	})

	if window.ProjectionEpoch != testEpoch || window.ThroughCursor != 2+deltas {
		t.Fatalf("epoch %q through %d", window.ProjectionEpoch, window.ThroughCursor)
	}
	for position, entity := range window.Entities {
		if entity.EntityIndex != uint64(position) {
			t.Fatalf("entity %d has index %d, want a dense numbering from 0", position, entity.EntityIndex)
		}
	}
	// The row browsers sync: every field of the view, present even when null.
	contract := []string{
		"arguments_ref", "cursor", "entity_id", "entity_kind", "input_ref", "output_ref", "pending",
		"projection_epoch", "revision_cursor", "state", "text_ref", "thread_id", "turn_id",
	}
	var reply map[string]any
	for _, view := range views(t, window) {
		keys := make([]string, 0, len(view))
		for key := range view {
			keys = append(keys, key)
		}
		slices.Sort(keys)
		if !slices.Equal(keys, contract) {
			t.Fatalf("view fields = %v, want %v", keys, contract)
		}
		if view["entity_id"] == "test-reply" {
			reply = view
		}
	}
	if reply == nil || reply["entity_kind"] != "item" || reply["thread_id"] != sessionID.String() || reply["projection_epoch"] != testEpoch {
		t.Fatalf("reply view = %v", reply)
	}
	// The reply started at cursor 2 and has been revised since.
	text, _ := reply["text_ref"].(map[string]any)
	revisionCursor, ok := reply["revision_cursor"].(string)
	if !ok {
		t.Fatalf("revision cursor has type %T, want string", reply["revision_cursor"])
	}
	revision, err := strconv.ParseUint(revisionCursor, 10, 64)
	if err != nil {
		t.Fatalf("parse revision cursor %q: %v", revisionCursor, err)
	}
	if reply["cursor"] != "2" || revision <= 2 || text["owner_id"] != "test-reply" {
		t.Fatalf("reply cursor %v revision %v text %v", reply["cursor"], reply["revision_cursor"], reply["text_ref"])
	}
}

func TestAWindowHoldsExactlyTheRequestedIndexes(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedReply(t, f)
	all := readEntities(t, f, &historypb.ReadThreadEntitiesRequest{SessionId: sessionID.String(), Window: entityRange(0, 200)})
	if len(all.Entities) < 3 {
		t.Fatalf("the folded Thread has %d entities, the test needs 3", len(all.Entities))
	}

	middle := readEntities(t, f, &historypb.ReadThreadEntitiesRequest{SessionId: sessionID.String(), Window: entityRange(1, 1)})
	past := readEntities(t, f, &historypb.ReadThreadEntitiesRequest{SessionId: sessionID.String(), Window: entityRange(uint64(len(all.Entities)), 5)})

	if len(middle.Entities) != 1 || middle.Entities[0].EntityIndex != 1 {
		t.Fatalf("window of index 1 = %v", middle.Entities)
	}
	if past.ProjectionEpoch != testEpoch || len(past.Entities) != 0 {
		t.Fatalf("window past the end = %v", past)
	}
}

func TestKeysReturnTheRowsThatExist(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedReply(t, f)

	window := readEntities(t, f, &historypb.ReadThreadEntitiesRequest{
		SessionId: sessionID.String(),
		Window: entityKeys(
			&historypb.EntityKey{EntityKind: "item", EntityId: "test-reply"},
			&historypb.EntityKey{EntityKind: "command", EntityId: "test-never-sent"},
		),
	})

	if found := views(t, window); len(found) != 1 || found[0]["entity_id"] != "test-reply" {
		t.Fatalf("views = %v, want only the reply", found)
	}
}

func TestAKeyMatchesAnEntityByItsKindAndItsIdTogether(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedReply(t, f)
	all := views(t, readEntities(t, f, &historypb.ReadThreadEntitiesRequest{SessionId: sessionID.String(), Window: entityRange(0, 200)}))
	first := all[0]
	second := all[slices.IndexFunc(all, func(view map[string]any) bool {
		return view["entity_kind"] != first["entity_kind"] && view["entity_id"] != first["entity_id"]
	})]

	// Each key pairs one entity's kind with the other's id, so no entity has either.
	window := readEntities(t, f, &historypb.ReadThreadEntitiesRequest{
		SessionId: sessionID.String(),
		Window: entityKeys(
			&historypb.EntityKey{EntityKind: first["entity_kind"].(string), EntityId: second["entity_id"].(string)},
			&historypb.EntityKey{EntityKind: second["entity_kind"].(string), EntityId: first["entity_id"].(string)},
		),
	})

	if len(window.Entities) != 0 {
		t.Fatalf("keys of no entity read %v", window.Entities)
	}
}

func TestReadsRefuseAStaleEpochAndAnUnknownSession(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedReply(t, f)
	for _, test := range []struct {
		name    string
		request *historypb.ReadThreadEntitiesRequest
		want    error
	}{
		{"stale epoch", &historypb.ReadThreadEntitiesRequest{SessionId: sessionID.String(), ProjectionEpoch: "v1", Window: entityRange(0, 10)}, ErrScopeReset},
		{"unknown Session", &historypb.ReadThreadEntitiesRequest{SessionId: uuid.NewString(), Window: entityRange(0, 10)}, storage.ErrNotFound},
		{"malformed Session", &historypb.ReadThreadEntitiesRequest{SessionId: "not-a-uuid", Window: entityRange(0, 10)}, storage.ErrInvalidArgument},
	} {
		if _, err := f.reader.ReadThreadEntities(t.Context(), test.request); !errors.Is(err, test.want) {
			t.Errorf("%s: %v, want %v", test.name, err, test.want)
		}
	}
}

func TestASessionTheFoldHasNotReachedReadsAsEmpty(t *testing.T) {
	f := newFixture(t)
	sessionID := f.session(t, f.history, sandboxName("unfolded-"), turnStarted(1))

	empty := readEntities(t, f, &historypb.ReadThreadEntitiesRequest{SessionId: sessionID.String(), Window: entityRange(0, 10)})
	_, held := f.reader.ReadThreadEntities(t.Context(), &historypb.ReadThreadEntitiesRequest{
		SessionId: sessionID.String(), ProjectionEpoch: testEpoch, Window: entityRange(0, 10),
	})

	if empty.ProjectionEpoch != "" || empty.ThroughCursor != 0 || len(empty.Entities) != 0 {
		t.Fatalf("unfolded Session read %v", empty)
	}
	if !errors.Is(held, ErrScopeReset) {
		t.Fatalf("a caller holding an epoch of an unfolded Session: %v, want ErrScopeReset", held)
	}
}

func TestOnlyAWellFormedWindowIsRead(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedReply(t, f)
	tooMany := make([]*historypb.EntityKey, 201)
	for index := range tooMany {
		tooMany[index] = &historypb.EntityKey{EntityKind: "item", EntityId: strconv.Itoa(index)}
	}
	for name, window := range map[string]func(*historypb.ReadThreadEntitiesRequest){
		"no window":         func(*historypb.ReadThreadEntitiesRequest) {},
		"empty range":       func(r *historypb.ReadThreadEntitiesRequest) { r.Window = entityRange(0, 0) },
		"oversized range":   func(r *historypb.ReadThreadEntitiesRequest) { r.Window = entityRange(0, 201) },
		"range off the end": func(r *historypb.ReadThreadEntitiesRequest) { r.Window = entityRange(math.MaxInt64-5, 10) },
		"no keys":           func(r *historypb.ReadThreadEntitiesRequest) { r.Window = entityKeys() },
		"too many keys":     func(r *historypb.ReadThreadEntitiesRequest) { r.Window = entityKeys(tooMany...) },
	} {
		request := &historypb.ReadThreadEntitiesRequest{SessionId: sessionID.String()}
		window(request)
		if _, err := f.reader.ReadThreadEntities(t.Context(), request); !errors.Is(err, storage.ErrInvalidArgument) {
			t.Errorf("%s: %v, want ErrInvalidArgument", name, err)
		}
	}
}

func TestAStoredEntityOutsideTheViewContractIsRefused(t *testing.T) {
	f := newFixture(t)
	sessionID := foldedReply(t, f)
	if _, err := f.db.Exec(`UPDATE thread_entity SET state = state || '{"unexpected": true}'
		WHERE thread_id = $1 AND entity_kind = 'item' AND entity_id = 'test-reply'`, sessionID); err != nil {
		t.Fatal(err)
	}

	_, err := f.reader.ReadThreadEntities(t.Context(), &historypb.ReadThreadEntitiesRequest{
		SessionId: sessionID.String(), Window: entityKeys(&historypb.EntityKey{EntityKind: "item", EntityId: "test-reply"}),
	})

	if !errors.Is(err, ErrInvalidStoredEntity) {
		t.Fatalf("read of a malformed row: %v, want ErrInvalidStoredEntity", err)
	}
}
