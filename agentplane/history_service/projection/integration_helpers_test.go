package projection

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
)

const (
	testSource = "test-runner"
	testEpoch  = "test-epoch"
)

func entry(cursor uint64, observation *eventpb.Event, sourceSequences ...uint64) *eventlogpb.EventEntry {
	observation.SourceSequences = sourceSequences
	return &eventlogpb.EventEntry{
		Cursor: cursor,
		Origin: &eventlogpb.EventOrigin{SourceId: testSource, Sequence: cursor},
		Event:  observation,
	}
}

func native(cursor uint64) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_Native{Native: &eventpb.Native{Line: "{}"}}})
}

func pageOf(entries ...*eventlogpb.EventEntry) *historypb.ReadSessionEventsResponse {
	return &historypb.ReadSessionEventsResponse{Entries: entries, LastCursor: entries[len(entries)-1].Cursor}
}

func withTx(t *testing.T, db *sql.DB, f func(tx *sql.Tx) error) error {
	t.Helper()
	tx, err := db.BeginTx(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := f(tx); err != nil {
		tx.Rollback()
		return err
	}
	return tx.Commit()
}

func record(t *testing.T, db *sql.DB, threadID uuid.UUID, page *historypb.ReadSessionEventsResponse) error {
	t.Helper()
	return withTx(t, db, func(tx *sql.Tx) error {
		return RecordThreadFold(context.Background(), tx, threadID, testSource, page, testEpoch)
	})
}

func mustRecord(t *testing.T, db *sql.DB, threadID uuid.UUID, page *historypb.ReadSessionEventsResponse) {
	t.Helper()
	if err := record(t, db, threadID, page); err != nil {
		t.Fatal(err)
	}
}

type storedEntity struct {
	kind, id         string
	cursor, revision uint64
	index            uint64
	textRef          *historypb.ThreadPayloadReference
	state            json.RawMessage
}

func entities(t *testing.T, db *sql.DB, threadID uuid.UUID) map[string]storedEntity {
	t.Helper()
	rows, err := db.Query(`SELECT entity_kind, entity_id, cursor, revision_cursor, entity_index, state
		FROM thread_entity WHERE thread_id = $1`, threadID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	found := map[string]storedEntity{}
	for rows.Next() {
		var entity storedEntity
		var state []byte
		if err := rows.Scan(&entity.kind, &entity.id, &entity.cursor, &entity.revision, &entity.index, &state); err != nil {
			t.Fatal(err)
		}
		entity.state = state
		if entity.kind == "item" {
			item := &historypb.ThreadItem{}
			if err := protojson.Unmarshal(state, item); err != nil {
				t.Fatal(err)
			}
			entity.textRef = item.TextRef
		}
		found[entity.kind+"/"+entity.id] = entity
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return found
}

// bodyOf reads the text a reference spans, straight from the chunk table.
func bodyOf(t *testing.T, db *sql.DB, threadID uuid.UUID, reference historypb.ThreadPayloadReference) string {
	t.Helper()
	field, err := payloadFieldSQLName(reference.Field)
	if err != nil {
		t.Fatal(err)
	}
	rows, err := db.Query(`SELECT text FROM thread_payload_chunk WHERE thread_id = $1 AND projection_epoch = $2
		AND owner_cursor = $3 AND owner_id = $4 AND field = $5 AND generation = $6 AND chunk_index < $7
		ORDER BY chunk_index`, threadID, reference.ProjectionEpoch, reference.OwnerCursor, reference.OwnerId,
		field, reference.Generation, reference.ChunkCount)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var body strings.Builder
	for rows.Next() {
		var encoded []byte
		var chunk string
		if err := rows.Scan(&encoded); err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(encoded, &chunk); err != nil {
			t.Fatal(err)
		}
		body.WriteString(chunk)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return body.String()
}

func count(t *testing.T, db *sql.DB, query string, args ...any) int {
	t.Helper()
	var n int
	if err := db.QueryRow(query, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}
