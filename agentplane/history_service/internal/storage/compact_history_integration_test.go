package storage

import (
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/history_service/testing/spans"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
)

func assertBlocksDoNotOverlap(t *testing.T, store *Store, id uuid.UUID) {
	t.Helper()
	var overlaps int
	if err := store.db.QueryRowContext(t.Context(), `SELECT count(*) FROM session_event_block a JOIN session_event_block b
		ON a.session_id=b.session_id AND a.kind=b.kind AND a.first_cursor<b.first_cursor AND a.last_cursor>=b.first_cursor
		WHERE a.session_id=$1`, id).Scan(&overlaps); err != nil {
		t.Fatal(err)
	}
	if overlaps != 0 {
		t.Fatalf("%d overlapping block spans", overlaps)
	}
}

func TestHistoricalPackingDryRunApplyAndRerunPreserveEvents(t *testing.T) {
	store := testStore(t)
	id := opened(t, store)
	var original []*eventlogpb.EventEntry
	original = append(original, nativeEntry(1, strings.Repeat("large", 20_000)))
	for cursor := uint64(2); cursor <= 701; cursor++ {
		original = append(original, nativeEntry(cursor, "small\x00🙂"))
	}
	original[5].ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01})
	appendEntries(t, store, id, original...)
	dry, err := store.CompactHistory(t.Context(), id, false)
	if err != nil || dry.Entries == 0 || dry.PackedBytes >= dry.UncompressedBytes {
		t.Fatalf("dry run: %+v, %v", dry, err)
	}
	if raw, blocks := packedRows(t, store, id); raw != len(original) || blocks != 0 {
		t.Fatalf("dry run wrote: %d ordinary, %d blocks", raw, blocks)
	}
	apply, err := store.CompactHistory(t.Context(), id, true)
	if err != nil || apply != dry {
		t.Fatalf("apply %+v differs from preview %+v: %v", apply, dry, err)
	}
	assertEntries(t, readPage(t, store, id, 0, 1000).Entries, original...)
	assertBlocksDoNotOverlap(t, store, id)
	again, err := store.CompactHistory(t.Context(), id, true)
	if err != nil || again.Entries != 0 || again.Blocks != 0 {
		t.Fatalf("rerun changed packed history: %+v, %v", again, err)
	}
	appendEntries(t, store, id, original[250:260]...)
	if last, err := store.Append(t.Context(), id, nil); err != nil || last != uint64(len(original)) {
		t.Fatalf("watermark changed: %d, %v", last, err)
	}
}

func TestHistoricalPackingRespectsLiveBlockBoundaries(t *testing.T) {
	store := testStore(t)
	id := opened(t, store)
	chunks := make([]string, 600)
	for index := range chunks {
		chunks[index] = "hello "
	}
	span := spans.CodexReasoning([][]string{chunks})
	original := span.Entries[:len(span.Entries)-2]
	appendEntries(t, store, id, original[:400]...)
	store.options.CompactDeltas = true
	appendEntries(t, store, id, original[400:]...)
	if _, blocks := packedRows(t, store, id); blocks == 0 {
		t.Fatal("no live blocks")
	}
	result, err := store.CompactHistory(t.Context(), id, true)
	if err != nil || result.Entries == 0 || result.Skipped != 0 {
		t.Fatalf("backfill: %+v, %v", result, err)
	}
	assertBlocksDoNotOverlap(t, store, id)
	for after := uint64(0); after < uint64(len(original)); {
		page := readPage(t, store, id, after, 1000)
		assertEntries(t, page.Entries, original[after:after+uint64(len(page.Entries))]...)
		after += uint64(len(page.Entries))
	}
	appendEntries(t, store, id, span.Entries[len(original):]...)
	assertBlocksDoNotOverlap(t, store, id)
}

func TestHistoricalPackingLeavesOrdinaryEntriesInsideExistingBlockSpans(t *testing.T) {
	store := NewStore(testDB(t), Options{CompactDeltas: true})
	id := opened(t, store)
	var original []*eventlogpb.EventEntry
	for _, interval := range []struct {
		enabled bool
		count   int
	}{{true, 10}, {false, 100}, {true, 80}} {
		store.options.CompactDeltas = interval.enabled
		var batch []*eventlogpb.EventEntry
		for range interval.count {
			batch = append(batch, nativeEntry(uint64(len(original)+len(batch)+1), "frame"))
		}
		appendEntries(t, store, id, batch...)
		original = append(original, batch...)
	}
	result, err := store.CompactHistory(t.Context(), id, true)
	if err != nil || result.Skipped != 100 || result.Entries != 0 {
		t.Fatalf("covered ordinary rows: %+v, %v", result, err)
	}
	assertBlocksDoNotOverlap(t, store, id)
	assertEntries(t, readPage(t, store, id, 0, 1000).Entries, original...)
}

func TestHistoricalPackingExecutionEvidenceRequiresExplicitScope(t *testing.T) {
	writer := NewStore(testDB(t), Options{})
	id := opened(t, writer)
	var original []*eventlogpb.EventEntry
	for index := range 160 {
		original = append(original, executionEvidenceEntry(uint64(index+1), index%2 == 0))
	}
	appendEntries(t, writer, id, original...)
	defaultDryRun, err := writer.CompactHistory(t.Context(), id, false)
	if err != nil || defaultDryRun.Entries != 0 || defaultDryRun.Blocks != 0 {
		t.Fatalf("default historical scope included execution evidence: %+v, %v", defaultDryRun, err)
	}

	compactor := NewStore(writer.db, Options{CompactExecutionEvidence: true})
	dryRun, err := compactor.CompactHistory(t.Context(), id, false)
	if err != nil || dryRun.Entries != int64(len(original)) || dryRun.Blocks != 2 || dryRun.PackedBytes >= dryRun.UncompressedBytes {
		t.Fatalf("evidence dry run: %+v, %v", dryRun, err)
	}
	if raw, blocks := packedRows(t, compactor, id); raw != len(original) || blocks != 0 {
		t.Fatalf("dry run wrote %d ordinary rows and %d blocks", raw, blocks)
	}
	result, err := compactor.CompactHistory(t.Context(), id, true)
	if err != nil || result != dryRun {
		t.Fatalf("apply %+v differs from dry run %+v: %v", result, dryRun, err)
	}
	assertBlocksDoNotOverlap(t, compactor, id)
	reader := NewStore(writer.db, Options{})
	assertEntries(t, readPage(t, reader, id, 0, len(original)).Entries, original...)
	appendEntries(t, reader, id, original[31:38]...)
}
