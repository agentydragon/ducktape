package storage

import (
	"errors"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/testing/spans"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
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

func executionEvidenceEntry(cursor uint64, boundary bool) *eventlogpb.EventEntry {
	entry := entry(cursor)
	if boundary {
		entry.Event.Observation = &eventpb.Event_BoundaryRecord{BoundaryRecord: &eventpb.BoundaryRecord{
			Boundary: "provider", Direction: "response", Payload: []byte{0, 0xff, byte(cursor)},
			MediaType: "application/octet-stream", CorrelationId: "intent", RecordId: "record",
			FragmentIndex: uint32(cursor % 3), FragmentCount: 3,
		}}
		return entry
	}
	entry.Event.Observation = &eventpb.Event_ExecutionProgress{ExecutionProgress: &eventpb.ExecutionProgress{
		OperationId: "operation", Kind: eventpb.ExecutionProgress_KIND_PROVIDER,
		Phase: eventpb.ExecutionProgress_PHASE_RECEIVED,
	}}
	return entry
}

func TestExecutionEvidenceBlocksAreGatedForMixedVersionRollout(t *testing.T) {
	store := NewStore(testDB(t), Options{CompactDeltas: true, CompactExecutionEvidence: true})
	sessionID := opened(t, store)
	var original []*eventlogpb.EventEntry
	appendEvidence := func(count int) {
		var batch []*eventlogpb.EventEntry
		for index := range count {
			batch = append(batch, executionEvidenceEntry(uint64(len(original)+len(batch)+1), index%2 == 0))
			batch = append(batch, executionEvidenceEntry(uint64(len(original)+len(batch)+1), index%2 != 0))
		}
		if len(original) == 0 {
			batch[5].ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01})
		}
		original = append(original, batch...)
		appendEntries(t, store, sessionID, batch...)
	}
	appendEvidence(20)
	if raw, blocks := packedRows(t, store, sessionID); raw != 40 || blocks != 0 {
		t.Fatalf("small evidence tails should remain ordinary: %d raw, %d blocks", raw, blocks)
	}
	store.options.CompactExecutionEvidence = false
	appendEvidence(20)
	if raw, blocks := packedRows(t, store, sessionID); raw != 80 || blocks != 0 {
		t.Fatalf("disabled evidence gate packed a mixed-version tail: %d raw, %d blocks", raw, blocks)
	}
	store.options.CompactExecutionEvidence = true
	appendEvidence(50)
	if raw, blocks := packedRows(t, store, sessionID); raw != 40 || blocks < 2 {
		t.Fatalf("enabled evidence gate did not pack both kinds: %d raw, %d blocks", raw, blocks)
	}
	reader := NewStore(store.db, Options{})
	page := readPage(t, reader, sessionID, 0, len(original))
	assertEntries(t, page.Entries, original...)
	filtered := followPage(t, reader, sessionID, 0, historypb.EventKind_EVENT_KIND_BOUNDARY_RECORD)
	if len(filtered.Entries) != 90 {
		t.Fatalf("boundary follow returned %d entries, want 90", len(filtered.Entries))
	}
	sequences, err := reader.ReadSequences(t.Context(), sessionID, []uint64{1, 2, 38, 39, 100, 120, 180}, 2)
	if err != nil {
		t.Fatal(err)
	}
	for index, sequence := range []uint64{1, 2, 38, 39, 100, 120, 180} {
		read := sequences.Reads[index]
		if read.Unstored || read.NextEntry == nil || !proto.Equal(read.NextEntry, original[sequence-1]) {
			t.Fatalf("sequence %d read: %+v", sequence, read)
		}
	}
	appendEntries(t, reader, sessionID, page.Entries[50:55]...)
	changed := proto.Clone(page.Entries[51]).(*eventlogpb.EventEntry)
	changed.Event.Observation = &eventpb.Event_BoundaryRecord{BoundaryRecord: &eventpb.BoundaryRecord{Boundary: "changed"}}
	if _, err := reader.Append(t.Context(), sessionID, []*eventlogpb.EventEntry{changed}); !errors.Is(err, ErrConflict) {
		t.Fatalf("changed packed replay: %v", err)
	}
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
	ordinary := postgres.Count(t, store.db, "SELECT count(*) FROM session_event WHERE session_id=$1", id)
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

func TestReadVersionedSparseBlockOfPreviouslyUnpackedKind(t *testing.T) {
	store := testStore(t)
	id := opened(t, store)
	originals := []*eventlogpb.EventEntry{entry(1), executionEvidenceEntry(2, true), entry(3)}
	appendEntries(t, store, id, originals...)
	p, err := eventblock.NewPacker(eventblock.Profile{TargetBytes: 65536, TargetEntries: 32, Level: 3})
	if err != nil {
		t.Fatal(err)
	}
	a, err := marshal(originals[0])
	if err != nil {
		t.Fatal(err)
	}
	b, err := marshal(originals[2])
	if err != nil {
		t.Fatal(err)
	}
	wire, err := p.Encode([][]byte{a, b})
	if err != nil {
		t.Fatal(err)
	}
	kind := observationNumber(originals[0])
	if _, err := store.db.ExecContext(t.Context(), `INSERT INTO session_event_block(session_id,first_cursor,last_cursor,kind,cursors,sizes,payload)
    VALUES($1,1,3,$2,'{1,3}',ARRAY[$3::bigint,$4::bigint],$5)`, id, kind, len(a), len(b), wire); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.ExecContext(t.Context(), `DELETE FROM session_event WHERE session_id=$1 AND cursor IN (1,3)`, id); err != nil {
		t.Fatal(err)
	}
	assertEntries(t, readPage(t, store, id, 0, 3).Entries, originals...)
	assertEntries(t, followPage(t, store, id, 1, historypb.EventKind(kind)).Entries, originals[2])
	reads, err := store.ReadSequences(t.Context(), id, []uint64{1, 2, 3}, 3)
	if err != nil {
		t.Fatal(err)
	}
	for i, r := range reads.Reads {
		if !proto.Equal(r.NextEntry, originals[i]) {
			t.Fatalf("sequence %d lost", i+1)
		}
	}
	tx, err := store.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	refs, err := eventReferences(t.Context(), tx, id, 0, 2, nil, 2, true)
	if err != nil || len(refs) != 2 || refs[0].cursor != 2 || refs[1].cursor != 1 {
		t.Fatalf("reverse: %+v, %v", refs, err)
	}
	if err := deleteEventRanges(t.Context(), tx, id, []*historypb.CursorRange{{First: 1, Last: 1}}); err != nil {
		t.Fatal(err)
	}
	surviving, err := ReadEntry(t.Context(), tx, id, 3)
	if err != nil || !proto.Equal(surviving, originals[2]) {
		t.Fatalf("after removal: %v, %v", surviving, err)
	}
}

func TestVersionedWriterPacksLargeBoundaryOnCrossing(t *testing.T) {
	p, err := eventblock.NewPacker(eventblock.Profile{TargetBytes: 2 << 20, TargetEntries: 32, Level: 3})
	if err != nil {
		t.Fatal(err)
	}
	store := NewStore(testDB(t), Options{EventPackers: map[historypb.EventKind]*eventblock.Packer{historypb.EventKind_EVENT_KIND_BOUNDARY_RECORD: p}})
	id := opened(t, store)
	var original []*eventlogpb.EventEntry
	for i := uint64(1); i <= 4; i++ {
		e := executionEvidenceEntry(i, true)
		e.Event.GetBoundaryRecord().Payload = []byte(strings.Repeat("journal", 100000))
		appendEntries(t, store, id, e)
		original = append(original, e)
		if i == 2 {
			if raw, blocks := packedRows(t, store, id); raw != 2 || blocks != 0 {
				t.Fatalf("early seal: %d %d", raw, blocks)
			}
		}
	}
	if raw, blocks := packedRows(t, store, id); raw != 1 || blocks != 1 {
		t.Fatalf("after crossing: %d rows, %d blocks", raw, blocks)
	}
	got, err := store.ReadSequences(t.Context(), id, []uint64{1, 2, 3, 4}, 4)
	if err != nil {
		t.Fatal(err)
	}
	for i, r := range got.Reads {
		if !proto.Equal(r.NextEntry, original[i]) {
			t.Fatalf("read differs at %d", i+1)
		}
	}
}
