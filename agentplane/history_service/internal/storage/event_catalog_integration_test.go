package storage

import (
	"errors"
	"strings"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/proto"
)

func TestJavaScriptCatalogReferencesSurviveHistoryReadsAndReplay(t *testing.T) {
	writer := NewStore(testDB(t), Options{DeduplicateToolCatalogs: true, CompactExecutionEvidence: true})
	reader := NewStore(writer.db, Options{})
	sessionID := opened(t, writer)
	catalog := `[ { "description": "` + strings.Repeat("example ", FollowBytesLimit/16) + `" } ]`
	execute := func(cursor uint64, source, catalog string) *eventlogpb.EventEntry {
		event := entry(cursor)
		event.Event.Observation = &eventpb.Event_BoundaryRecord{BoundaryRecord: &eventpb.BoundaryRecord{
			Boundary: "javascript", Direction: "send", FragmentCount: 1, MediaType: "application/json",
			Payload: []byte(`{"type":"operation/request","request":{"method":"session/execute","request":{"source":"` + source + `","enabled_tools":` + catalog + `}}}`),
		}}
		event.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01})
		return event
	}
	legacy := execute(1, "text(0)", catalog)
	first := execute(2, "text(1)", catalog)
	second := execute(3, "text(2)", catalog)
	appendEntries(t, reader, sessionID, legacy)
	appendEntries(t, writer, sessionID, first, second)
	appendEntries(t, reader, sessionID, legacy, first, second)
	replay := execute(3, "text(2)", strings.Replace(catalog, "example", "changed", 1))
	if _, err := reader.Append(t.Context(), sessionID, []*eventlogpb.EventEntry{replay}); !errors.Is(err, ErrConflict) {
		t.Fatalf("conflicting catalog replay: %v", err)
	}
	var catalogs, refs int
	var stored int64
	err := writer.db.QueryRowContext(t.Context(), `SELECT
		(SELECT count(*) FROM session_event_catalog WHERE session_id=$1),
		count(catalog_id),sum(octet_length(payload))
		FROM session_event WHERE session_id=$1`, sessionID).Scan(&catalogs, &refs, &stored)
	if err != nil || catalogs != 1 || refs != 2 || stored >= int64(proto.Size(first)+proto.Size(second)) {
		t.Fatalf("stored catalogs=%d refs=%d event bytes=%d: %v", catalogs, refs, stored, err)
	}
	assertEntries(t, readPage(t, reader, sessionID, 1, 2).Entries, first, second)
	read, err := reader.ReadSequences(t.Context(), sessionID, []uint64{2, 3}, 2)
	if err != nil || len(read.Reads) != 2 {
		t.Fatalf("read catalog sequences: %v %v", read, err)
	}
	assertEntries(t, []*eventlogpb.EventEntry{read.Reads[0].NextEntry, read.Reads[1].NextEntry}, first, second)
	// The full catalog counts against the follow response size even though it appears once on disk.
	page := followPage(t, reader, sessionID, 1, historypb.EventKind_EVENT_KIND_BOUNDARY_RECORD)
	assertEntries(t, page.Entries, first)

	// Pack ordinary boundary records on both sides of a catalog-backed cursor.
	var batch []*eventlogpb.EventEntry
	for i := range blockMinimum + 1 {
		cursor := uint64(4 + i)
		if i == blockMinimum/2 {
			batch = append(batch, execute(cursor, "text(3)", `["`+strings.Repeat("x", 2048)+`"]`))
		} else {
			batch = append(batch, executionEvidenceEntry(cursor, true))
		}
	}
	appendEntries(t, writer, sessionID, batch...)
	if raw, blocks := packedRows(t, writer, sessionID); raw != 4 || blocks != 1 {
		t.Fatalf("mixed boundary storage: %d rows, %d blocks", raw, blocks)
	}
	assertEntries(t, readPage(t, reader, sessionID, 3, len(batch)).Entries, batch...)
	appendEntries(t, reader, sessionID, batch...)
}
