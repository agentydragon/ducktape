package storage

import (
	"errors"
	"strings"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/testing/spans"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func packedRows(t *testing.T, store *Store, sessionID uuid.UUID) (int, int) {
	t.Helper()
	var raw, blocks int
	if err := store.db.QueryRowContext(t.Context(), `SELECT
		(SELECT count(*) FROM session_event WHERE session_id=$1),
		(SELECT count(*) FROM session_event_block WHERE session_id=$1)`, sessionID).Scan(&raw, &blocks); err != nil {
		t.Fatal(err)
	}
	return raw, blocks
}

func TestCompactionBeforeCompletionPreservesAllReadContracts(t *testing.T) {
	store := NewStore(testDB(t), Options{CompactDeltas: true})
	sessionID := opened(t, store)
	chunks := make([]string, 320)
	for index := range chunks {
		chunks[index] = "text\x00🙂"
	}
	span := spans.CodexReasoning([][]string{chunks})
	prefix := span.Entries[:len(span.Entries)-2]
	prefix[4].ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01})
	// Small ingress batches cross multiple block boundaries before any completion exists.
	for start := 0; start < len(prefix); start += 31 {
		appendEntries(t, store, sessionID, prefix[start:min(start+31, len(prefix))]...)
	}
	raw, blocks := packedRows(t, store, sessionID)
	if blocks == 0 || raw >= len(prefix)/2 {
		t.Fatalf("compaction left %d singleton rows and %d blocks for %d events", raw, blocks, len(prefix))
	}
	// A newly created reader has compaction disabled, as all API and projector readers do.
	reader := NewStore(store.db, Options{})
	assertEntries(t, readPage(t, reader, sessionID, 0, 1000).Entries, prefix...)
	assertEntries(t, readPage(t, reader, sessionID, 37, 17).Entries, prefix[37:54]...)
	for after := uint64(0); after < uint64(len(prefix)); {
		page := followPage(t, reader, sessionID, after)
		assertEntries(t, page.Entries, prefix[after:after+uint64(len(page.Entries))]...)
		if page.ThroughCursor <= after {
			t.Fatal("follow did not advance")
		}
		after = page.ThroughCursor
	}
	filtered := followPage(t, reader, sessionID, 37, historypb.EventKind_EVENT_KIND_NATIVE, historypb.EventKind_EVENT_KIND_NATIVE)
	var native []*eventlogpb.EventEntry
	for _, entry := range prefix[37:] {
		if entry.Event.GetNative() != nil {
			native = append(native, entry)
		}
	}
	assertEntries(t, filtered.Entries, native[:FollowEntriesLimit]...)
	start, err := reader.ResolveFollowStart(t.Context(), sessionID, &historypb.FollowSessionRequest{
		Start: &historypb.FollowSessionRequest_Back{Back: &historypb.StartBack{Kind: historypb.EventKind_EVENT_KIND_NATIVE, Count: 3}},
	})
	if err != nil || start != native[len(native)-3].Cursor-1 {
		t.Fatalf("back start: %d, %v", start, err)
	}
	for _, reverse := range []bool{false, true} {
		var before, after *uint64
		want := prefix[37:54]
		if reverse {
			before = pointer(55)
		} else {
			after = pointer(37)
		}
		page := observationPage(t, reader, sessionID, before, after, 17)
		for index, observation := range page.Observations {
			if observation.Cursor != want[index].Cursor || int32(observation.Kind) != observationNumber(want[index]) {
				t.Fatalf("observation %d: %+v", index, observation)
			}
		}
	}
	sequences := []uint64{32, 1, 128, 32, uint64(len(prefix)), uint64(len(prefix) + 9)}
	page, err := reader.ReadSequences(t.Context(), sessionID, sequences, 2)
	if err != nil {
		t.Fatal(err)
	}
	for index, sequence := range sequences {
		if sequence > uint64(len(prefix)) {
			if page.Reads[index].NextEntry != nil {
				t.Fatal("read past tail returned an entry")
			}
			continue
		}
		if page.Reads[index].Unstored || !proto.Equal(page.Reads[index].NextEntry, prefix[sequence-1]) {
			t.Fatalf("sequence %d differs", sequence)
		}
	}
	appendEntries(t, reader, sessionID, prefix[31:39]...)
	changed := proto.Clone(prefix[32]).(*eventlogpb.EventEntry)
	changed.Event.Observation = &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{ItemId: "changed", Text: "conflict"}}
	if _, err := reader.Append(t.Context(), sessionID, []*eventlogpb.EventEntry{changed}); !errors.Is(err, ErrConflict) {
		t.Fatalf("conflicting packed replay: %v", err)
	}
	// Disabling writes and appending a completion does not invalidate previously packed history.
	appendEntries(t, reader, sessionID, span.Entries[len(prefix):]...)
	assertEntries(t, readPage(t, reader, sessionID, 0, 1000).Entries, span.Entries...)
}

func TestSettlementPrunesPackedPairsWithoutLosingInterleavedFrames(t *testing.T) {
	store := NewStore(testDB(t), Options{CompactDeltas: true, SettleDeltas: true})
	sessionID := opened(t, store)
	chunks := make([]string, 80)
	for index := range chunks {
		chunks[index] = "hello"
	}
	span := spans.ClaudeText(chunks, spans.TextOptions{Interpose: func(s *spans.Span) {
		s.Native(map[string]any{"type": "test-extra-frame"})
	}})
	prefix := span.Entries[:len(span.Entries)-2]
	appendEntries(t, store, sessionID, prefix...)
	if _, blocks := packedRows(t, store, sessionID); blocks == 0 {
		t.Fatal("prefix was not packed")
	}
	appendEntries(t, store, sessionID, span.Entries[len(prefix):]...)
	page := readPage(t, store, sessionID, 0, 1000)
	want := append([]*eventlogpb.EventEntry{}, span.Entries[:2]...)
	want = append(want, span.Entries[len(span.Entries)-3:]...)
	assertEntries(t, page.Entries, want...)
	if len(page.Settlements) != 1 || page.Settlements[0].ChunkCount != 80 {
		t.Fatalf("settlements: %v", page.Settlements)
	}
	reads, err := store.ReadSequences(t.Context(), sessionID, []uint64{3, prefix[len(prefix)-1].Cursor}, 10)
	if err != nil || !reads.Reads[0].Unstored || !proto.Equal(reads.Reads[1].NextEntry, prefix[len(prefix)-1]) {
		t.Fatalf("settled and retained packed sequences: %v, %v", reads, err)
	}
}

func TestLongUnfinishedItemIsCompactedBeyondSettlementScanLimit(t *testing.T) {
	store := NewStore(testDB(t), Options{CompactDeltas: true})
	sessionID := opened(t, store)
	chunks := make([]string, 10_050)
	for index := range chunks {
		chunks[index] = "token "
	}
	span := spans.CodexReasoning([][]string{chunks})
	prefix := span.Entries[:len(span.Entries)-2]
	appendEntries(t, store, sessionID, prefix...)
	raw, blocks := packedRows(t, store, sessionID)
	if blocks == 0 || raw+blocks > len(prefix)/20 {
		t.Fatalf("%d events occupy %d rows and %d blocks", len(prefix), raw, blocks)
	}
	for _, after := range []uint64{0, 5001, uint64(len(prefix) - 20)} {
		page := readPage(t, store, sessionID, after, 17)
		assertEntries(t, page.Entries, prefix[after:after+uint64(len(page.Entries))]...)
	}
	var stored int64
	if err := store.db.QueryRowContext(t.Context(), `SELECT COALESCE(sum(size),0) FROM (
		SELECT pg_column_size(payload) AS size FROM session_event WHERE session_id=$1
		UNION ALL SELECT pg_column_size(payload) FROM session_event_block WHERE session_id=$1) storage`, sessionID).Scan(&stored); err != nil {
		t.Fatal(err)
	}
	var original int64
	for _, entry := range prefix {
		original += int64(proto.Size(entry))
	}
	if stored >= original/2 {
		t.Fatalf("packed payloads %d bytes, original %d", stored, original)
	}
}

func TestCompactionLeavesOversizedFramesReadable(t *testing.T) {
	store := NewStore(testDB(t), Options{CompactDeltas: true})
	sessionID := opened(t, store)
	batch := []*eventlogpb.EventEntry{nativeEntry(1, strings.Repeat("large", FollowBytesLimit))}
	batch = append(batch, nativeEntry(2, strings.Repeat("a", blockBytes/2+1)), nativeEntry(3, strings.Repeat("b", blockBytes/2+1)))
	for cursor := uint64(4); cursor < 104; cursor++ {
		batch = append(batch, nativeEntry(cursor, "small"))
	}
	appendEntries(t, store, sessionID, batch...)
	if _, blocks := packedRows(t, store, sessionID); blocks == 0 {
		t.Fatal("oversized frames blocked packing of later small frames")
	}
	first := followPage(t, store, sessionID, 0)
	assertEntries(t, first.Entries, batch[0])
	if first.ThroughCursor != 1 {
		t.Fatalf("oversized frame advanced to %d", first.ThroughCursor)
	}
	assertEntries(t, followPage(t, store, sessionID, 1).Entries, batch[1:]...)
}

func TestCompactionOnlyPacksEnabledAppendIntervals(t *testing.T) {
	store := NewStore(testDB(t), Options{})
	id := opened(t, store)
	var all []*eventlogpb.EventEntry
	for _, enabled := range []bool{false, true, false, true} {
		store.options.CompactDeltas = enabled
		var batch []*eventlogpb.EventEntry
		for range 80 {
			entry := nativeEntry(uint64(len(all)+len(batch)+1), "frame")
			batch = append(batch, entry)
		}
		appendEntries(t, store, id, batch...)
		all = append(all, batch...)
	}
	var ordinary int
	if err := store.db.QueryRowContext(t.Context(), "SELECT count(*) FROM session_event WHERE session_id=$1", id).Scan(&ordinary); err != nil {
		t.Fatal(err)
	}
	if ordinary != 160 {
		t.Fatalf("expected the two disabled intervals to retain 160 ordinary entries, got %d", ordinary)
	}
	assertEntries(t, readPage(t, store, id, 0, 1000).Entries, all...)
}

func TestCorruptPackedContentOrIndexFailsBodyRead(t *testing.T) {
	for _, corrupt := range []string{
		"UPDATE session_event_block SET payload=$2 WHERE session_id=$1",
		"UPDATE session_event_block SET sizes[1]=sizes[1]+1 WHERE session_id=$1 AND $2::bytea IS NOT NULL",
	} {
		store := NewStore(testDB(t), Options{CompactDeltas: true})
		id := opened(t, store)
		var batch []*eventlogpb.EventEntry
		for cursor := uint64(1); cursor <= 80; cursor++ {
			batch = append(batch, nativeEntry(cursor, "frame"))
		}
		appendEntries(t, store, id, batch...)
		if _, err := store.db.ExecContext(t.Context(), corrupt, id, []byte("invalid gzip")); err != nil {
			t.Fatal(err)
		}
		if _, err := store.ReadPage(t.Context(), id, 0, 128); err == nil {
			t.Fatal("corrupt block was served as valid history")
		}
	}
}
