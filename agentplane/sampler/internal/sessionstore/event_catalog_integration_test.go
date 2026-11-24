package sessionstore

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"google.golang.org/protobuf/proto"
)

func TestJavaScriptCatalogReferencesSurviveSamplerReads(t *testing.T) {
	writer, session, owner := claimedSession(t)
	writer.eventBlockWrites = true
	reader := New(writer.DB(), writer.operationTimeout, nil)
	before, err := reader.GetSession(t.Context(), session.ID, "")
	if err != nil {
		t.Fatal(err)
	}
	definitions := `[{"name":"test","description":"` + strings.Repeat("an exact catalog ", 100) + `"}]`
	frame := func(catalog, source string) map[string]any {
		return map[string]any{"type": "operation/request", "request": map[string]any{
			"method": "session/execute", "request": map[string]any{"enabled_tools": json.RawMessage(catalog), "source": source},
		}}
	}
	legacy := frame(definitions, "text(0)")
	first := frame(definitions, "text(1)")
	second := frame(definitions, "text(2)")
	changed := frame(strings.Replace(definitions, "test", "other", 1), "text(3)")
	fragmented := frame(definitions, strings.Repeat("x", BoundaryBytes))
	want := []map[string]any{legacy, first, second, changed, fragmented}
	for range eventcodec.MaxBlockEntries / 2 {
		want = append(want, second)
	}
	for index, object := range want {
		writer.deduplicateToolCatalogs = index > 0
		if err := writer.ProtocolEvent(t.Context(), owner, "exec-test", eventpb.ExecutionProgress_KIND_JAVASCRIPT,
			eventpb.ExecutionProgress_PHASE_SEND, object); err != nil {
			t.Fatal(err)
		}
	}
	other := openSessionForStore(t, writer, "")
	if err := writer.ProtocolEvent(t.Context(), claim(t, writer, other), "exec-other", eventpb.ExecutionProgress_KIND_JAVASCRIPT,
		eventpb.ExecutionProgress_PHASE_SEND, first); err != nil {
		t.Fatal(err)
	}
	if got := postgres.Count(t, writer.DB(), "SELECT count(*) FROM sampler_content WHERE kind=$1", content.ContentJavaScriptToolCatalog); got != 2 {
		t.Fatalf("stored %d catalogs for one repeated across Sessions and one changed value", got)
	}
	// The first reference follows the legacy boundary and its progress entry.
	packedCursor := before.Cursor + 3
	if count := postgres.Count(t, writer.DB(), `SELECT count(*) FROM sampler_event_block
		WHERE session_id=$1 AND $2 BETWEEN first_sequence AND last_sequence`, session.ID, packedCursor); count != 1 {
		t.Fatal("catalog-backed event was not sealed into a block")
	}
	frames, err := journalFrames(t.Context(), writer.DB(), session.ID, packedCursor, packedCursor)
	if err != nil || len(frames) != 1 || !bytes.HasPrefix(frames[0].wire, []byte(catalogMarker)) {
		t.Fatalf("packed cursor %d lost its catalog reference: %v", packedCursor, err)
	}
	// Recovery reads in reverse and may encounter a catalog reference between sampling events.
	tx, err := writer.DB().BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	entries, err := reader.Events(t.Context(), session.ID, before.Cursor, 2*eventcodec.MaxBlockEntries)
	if err != nil {
		t.Fatal(err)
	}
	reverse, err := recoveryEventsBefore(t.Context(), tx, session.ID, entries[len(entries)-1].Cursor+1)
	if err != nil {
		t.Fatal(err)
	}
	for index, entry := range entries {
		if !proto.Equal(entry, reverse[len(entries)-1-index]) {
			t.Fatalf("recovery read changed event %d", entry.Cursor)
		}
	}
	var actual [][]byte
	for _, entry := range entries {
		if boundary := entry.GetEvent().GetBoundaryRecord(); boundary != nil {
			if boundary.GetFragmentIndex() == 0 {
				actual = append(actual, nil)
			}
			actual[len(actual)-1] = append(actual[len(actual)-1], boundary.GetPayload()...)
		}
	}
	if len(actual) != len(want) {
		t.Fatalf("read %d execute records, want %d", len(actual), len(want))
	}
	for index, object := range want {
		encoded, err := json.Marshal(object)
		if err != nil || !bytes.Equal(encoded, actual[index]) {
			t.Fatalf("read changed execute request %d: %v", index, err)
		}
	}
}
