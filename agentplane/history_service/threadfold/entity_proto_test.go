package threadfold

import (
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

func TestStoredEntitiesRoundTrip(t *testing.T) {
	const cursor uint64 = 1<<53 + 7
	ref := &historypb.ThreadPayloadReference{ProjectionEpoch: "epoch", OwnerCursor: cursor, OwnerId: "id", Field: historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_TEXT, RevisionCursor: cursor + 1, Generation: cursor, ChunkCount: 2}
	examples := []struct {
		kind    EntityKind
		entity  *historypb.ThreadEntity
		pending bool
	}{
		{KindViewState, &historypb.ThreadEntity{Entity: &historypb.ThreadEntity_ViewState{ViewState: &historypb.ThreadViewState{Controls: &historypb.ThreadControls{}, Operational: &historypb.ThreadOperational{Status: historypb.ThreadFeedStatus_THREAD_FEED_STATUS_ACTIVE}}}}, false},
		{KindItem, &historypb.ThreadEntity{Entity: &historypb.ThreadEntity_Item{Item: &historypb.ThreadItem{Kind: eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT, TextRef: ref, Completion: &historypb.ThreadItem_Tool{Tool: &historypb.ThreadItemToolCompletion{Succeeded: false}}}}}, false},
		{KindConfirmedInput, &historypb.ThreadEntity{Entity: &historypb.ThreadEntity_ConfirmedInput{ConfirmedInput: &historypb.ThreadConfirmedInput{HarnessMessageId: "message", InputRef: &historypb.ThreadPayloadReference{ProjectionEpoch: "epoch", OwnerCursor: cursor, OwnerId: "message", Field: historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_CONFIRMED_INPUT, RevisionCursor: cursor, Generation: cursor}}}}, false},
		{KindLifecycle, &historypb.ThreadEntity{Entity: &historypb.ThreadEntity_Lifecycle{Lifecycle: &historypb.ThreadLifecycle{Event: &eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{}}}}}}, false},
		{KindCommand, &historypb.ThreadEntity{Entity: &historypb.ThreadEntity_Command{Command: &historypb.ThreadCommand{Outcome: historypb.ThreadCommandOutcome_THREAD_COMMAND_OUTCOME_PENDING, OutcomeCursor: proto.Uint64(cursor)}}}, true},
	}
	for _, example := range examples {
		t.Run(string(example.kind), func(t *testing.T) {
			original := example.entity
			original.SessionId = "session"
			original.ProjectionEpoch = "epoch"
			original.EntityId = "id"
			original.Cursor = cursor
			original.RevisionCursor = cursor + 1
			original.EntityIndex = cursor
			state, err := EntityState(original)
			if err != nil {
				t.Fatal(err)
			}
			encoded, err := protojson.Marshal(state)
			if err != nil {
				t.Fatal(err)
			}
			decoded := &historypb.ThreadEntity{SessionId: original.SessionId, ProjectionEpoch: original.ProjectionEpoch, EntityId: original.EntityId, Cursor: original.Cursor, RevisionCursor: original.RevisionCursor, EntityIndex: original.EntityIndex}
			if err := ParseEntity(decoded, example.kind, example.pending, encoded); err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(original, decoded) {
				t.Fatalf("round trip: got %v, want %v", decoded, original)
			}
		})
	}
}

func TestStoredEntityRejectsInvalidState(t *testing.T) {
	entity := &historypb.ThreadEntity{EntityId: "id"}
	if err := ParseEntity(entity, KindCommand, true, []byte(`{"outcome":"THREAD_COMMAND_OUTCOME_FAILED"}`)); err == nil {
		t.Fatal("pending index disagrees with outcome")
	}
	if err := ParseEntity(entity, KindItem, false, []byte(`{"unknown":1}`)); err == nil {
		t.Fatal("unknown state field accepted")
	}
	if err := ParseEntity(entity, EntityKind("bad"), false, []byte(`{}`)); err == nil {
		t.Fatal("unknown kind accepted")
	}
}

func TestOrderedEntityRowsRejectsUnknownPayloadFields(t *testing.T) {
	for _, field := range []historypb.ThreadPayloadField{historypb.ThreadPayloadField_THREAD_PAYLOAD_FIELD_UNSPECIFIED, 99} {
		ref := PayloadRef{Field: field}
		batch := ProjectionBatch{ItemUpserts: []Item{{Text: &ref}}}
		if _, err := OrderedEntityRows(uuid.Nil, batch, map[PayloadRef]uint64{ref: 1}, nil); err == nil {
			t.Errorf("accepted payload field %d", field)
		}
	}
}
