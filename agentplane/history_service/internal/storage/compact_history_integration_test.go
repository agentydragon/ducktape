package storage

import (
	"bytes"
	"compress/gzip"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	dbpostgres "github.com/agentydragon/ducktape/agentplane/postgres"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	testpostgres "github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func repackTestPackers(t *testing.T) map[historypb.EventKind]*eventblock.Packer {
	t.Helper()
	packer, err := eventblock.NewPacker(eventblock.Profile{TargetBytes: 4 << 10, TargetEntries: 4, Level: 1})
	if err != nil {
		t.Fatal(err)
	}
	packers := make(map[historypb.EventKind]*eventblock.Packer, len(historypb.EventKind_name)-1)
	for number := range historypb.EventKind_name {
		if number != 0 {
			packers[historypb.EventKind(number)] = packer
		}
	}
	return packers
}

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

func repackTestEntries(count int) []*eventlogpb.EventEntry {
	entries := make([]*eventlogpb.EventEntry, 0, count)
	for index := range count {
		cursor := uint64(index + 1)
		if index%2 == 0 {
			entries = append(entries, nativeEntry(cursor, strings.Repeat("stable-payload-", 80)))
		} else {
			entries = append(entries, executionEvidenceEntry(cursor, index%4 == 1))
		}
	}
	if len(entries) > 5 {
		entries[5].ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01})
	}
	return entries
}

func logicalFrames(t *testing.T, db *sql.DB, sessionID uuid.UUID) map[uint64][]byte {
	t.Helper()
	frames := map[uint64][]byte{}
	rows, err := db.QueryContext(t.Context(), `SELECT cursor,payload FROM session_event WHERE session_id=$1 ORDER BY cursor`, sessionID)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var cursor uint64
		var payload []byte
		if err := rows.Scan(&cursor, &payload); err != nil {
			rows.Close()
			t.Fatal(err)
		}
		if _, exists := frames[cursor]; exists {
			rows.Close()
			t.Fatalf("duplicate logical frame at cursor %d", cursor)
		}
		frames[cursor] = bytes.Clone(payload)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		t.Fatal(err)
	}
	if err := rows.Close(); err != nil {
		t.Fatal(err)
	}
	rows, err = db.QueryContext(t.Context(), `SELECT first_cursor,last_cursor,kind,cursors,sizes,payload,pg_column_size(payload)
		FROM session_event_block WHERE session_id=$1 ORDER BY first_cursor`, sessionID)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		block, err := scanStoredBlock(rows)
		if err != nil {
			rows.Close()
			t.Fatal(err)
		}
		for _, frame := range block.frames {
			if _, exists := frames[frame.cursor]; exists {
				rows.Close()
				t.Fatalf("duplicate logical frame at cursor %d", frame.cursor)
			}
			frames[frame.cursor] = bytes.Clone(frame.payload)
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		t.Fatal(err)
	}
	if err := rows.Close(); err != nil {
		t.Fatal(err)
	}
	return frames
}

func assertLogicalFrames(t *testing.T, got, want map[uint64][]byte) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("got %d stored frames, want %d", len(got), len(want))
	}
	for cursor, wantFrame := range want {
		if gotFrame, ok := got[cursor]; !ok || !bytes.Equal(gotFrame, wantFrame) {
			t.Fatalf("stored frame %d changed or disappeared", cursor)
		}
	}
}

func nonCanonicalCursorWire(entry *eventlogpb.EventEntry) ([]byte, error) {
	wire, err := proto.Marshal(entry)
	if err != nil {
		return nil, err
	}
	number, typ, tagSize := protowire.ConsumeTag(wire)
	if tagSize < 0 {
		return nil, protowire.ParseError(tagSize)
	}
	value, valueSize := protowire.ConsumeVarint(wire[tagSize:])
	if valueSize < 0 {
		return nil, protowire.ParseError(valueSize)
	}
	if number != 1 || typ != protowire.VarintType || value != entry.GetCursor() || value >= 128 {
		return nil, errors.New("fixture cursor does not have the expected wire representation")
	}
	prefix := protowire.AppendTag(nil, 1, protowire.VarintType)
	prefix = append(prefix, byte(value)|0x80, 0)
	return append(prefix, wire[tagSize+valueSize:]...), nil
}

func legacyRawBlock(t *testing.T, frames []rawFrame) []byte {
	t.Helper()
	var wire []byte
	for _, frame := range frames {
		wire = protowire.AppendTag(wire, 1, protowire.BytesType)
		wire = protowire.AppendBytes(wire, frame.payload)
	}
	var compressed bytes.Buffer
	writer := gzip.NewWriter(&compressed)
	if _, err := writer.Write(wire); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return compressed.Bytes()
}

func seedBlockFromRows(t *testing.T, db *sql.DB, sessionID uuid.UUID, cursors []int64, versioned bool) {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), `SELECT cursor,kind,payload FROM session_event
		WHERE session_id=$1 AND cursor=ANY($2::bigint[]) ORDER BY cursor`, sessionID, pq.Array(cursors))
	if err != nil {
		t.Fatal(err)
	}
	var frames []rawFrame
	for rows.Next() {
		var cursor uint64
		var kind historypb.EventKind
		var payload []byte
		if err := rows.Scan(&cursor, &kind, &payload); err != nil {
			rows.Close()
			t.Fatal(err)
		}
		entry, err := unmarshalRawEntry(payload)
		if err != nil || entry.GetCursor() != cursor || historypb.EventKind(observationNumber(entry)) != kind {
			rows.Close()
			t.Fatalf("invalid fixture frame at %d: %v", cursor, err)
		}
		frames = append(frames, rawFrame{cursor: cursor, kind: kind, size: proto.Size(entry), payload: bytes.Clone(payload)})
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		t.Fatal(err)
	}
	if err := rows.Close(); err != nil {
		t.Fatal(err)
	}
	if len(frames) != len(cursors) || len(frames) == 0 {
		t.Fatalf("loaded %d fixture frames for %d cursors", len(frames), len(cursors))
	}
	kind := frames[0].kind
	wireFrames := make([][]byte, len(frames))
	values, sizes := make([]int64, len(frames)), make([]int64, len(frames))
	for index, frame := range frames {
		if frame.kind != kind {
			t.Fatalf("fixture mixes kinds %s and %s", kind, frame.kind)
		}
		wireFrames[index] = frame.payload
		values[index], sizes[index] = int64(frame.cursor), int64(frame.size)
	}
	var payload []byte
	if versioned {
		packer, err := eventblock.NewPacker(eventblock.Profile{TargetBytes: eventblock.MaxBytes, TargetEntries: eventblock.MaxEntries, Level: 1})
		if err != nil {
			t.Fatal(err)
		}
		payload, err = packer.Encode(wireFrames)
		if err != nil {
			t.Fatal(err)
		}
	} else {
		payload = legacyRawBlock(t, frames)
	}
	if err := dbpostgres.Transaction(t.Context(), db, sql.TxOptions{}, func(tx *sql.Tx) error {
		if _, err := tx.ExecContext(t.Context(), `DELETE FROM session_event WHERE session_id=$1 AND cursor=ANY($2::bigint[])`, sessionID, pq.Array(values)); err != nil {
			return err
		}
		_, err := tx.ExecContext(t.Context(), `INSERT INTO session_event_block
			(session_id,first_cursor,last_cursor,kind,cursors,sizes,payload) VALUES($1,$2,$3,$4,$5,$6,$7)`,
			sessionID, values[0], values[len(values)-1], kind, pq.Array(values), pq.Array(sizes), payload)
		return err
	}); err != nil {
		t.Fatal(err)
	}
}

func TestOnlineRepackDryRunMixedFormatsAndRerunPreserveWireFrames(t *testing.T) {
	writer := testStore(t)
	id := opened(t, writer)
	original := repackTestEntries(180)
	appendEntries(t, writer, id, original...)
	for _, index := range []int{4, 6} {
		nonCanonical, err := nonCanonicalCursorWire(original[index])
		if err != nil {
			t.Fatal(err)
		}
		if _, err := writer.db.ExecContext(t.Context(), `UPDATE session_event SET payload=$3 WHERE session_id=$1 AND cursor=$2`, id, original[index].GetCursor(), nonCanonical); err != nil {
			t.Fatal(err)
		}
	}
	seedBlockFromRows(t, writer.db, id, []int64{1, 5, 9, 13, 17}, false)
	seedBlockFromRows(t, writer.db, id, []int64{81, 85, 89, 93, 97}, true)
	before := logicalFrames(t, writer.db, id)
	if len(before) != len(original) {
		t.Fatalf("fixture contains %d frames, want %d", len(before), len(original))
	}
	compactor := NewStore(writer.db, Options{EventPackers: repackTestPackers(t)})
	if tables := testpostgres.Count(t, writer.db, `SELECT count(*) FROM information_schema.tables
		WHERE table_schema=current_schema() AND table_name IN ('history_event_repack_run','history_event_repack_checkpoint')`); tables != 0 {
		t.Fatalf("repacker added database progress tables: %d", tables)
	}
	inventoryBefore, err := compactor.InventoryEventBlocks(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	ordinaryBefore, blocksBefore := packedRows(t, compactor, id)
	dryRun, err := compactor.RepackSession(t.Context(), id, false, nil)
	if err != nil || dryRun.Entries != int64(len(original)) || dryRun.SourceBlocks != 2 ||
		dryRun.LegacyBlocks != 1 || dryRun.VersionedSourceBlocks != 1 || dryRun.Blocks == 0 || dryRun.PackedBytes >= dryRun.UncompressedBytes {
		t.Fatalf("mixed-format dry run: %+v, %v", dryRun, err)
	}
	ordinaryAfterDryRun, blocksAfterDryRun := packedRows(t, compactor, id)
	if ordinaryAfterDryRun != ordinaryBefore || blocksAfterDryRun != blocksBefore {
		t.Fatalf("dry run wrote journal rows: ordinary %d->%d blocks %d->%d",
			ordinaryBefore, ordinaryAfterDryRun, blocksBefore, blocksAfterDryRun)
	}
	apply, err := compactor.RepackSession(t.Context(), id, true, nil)
	if err != nil || apply != dryRun {
		t.Fatalf("apply %+v differs from dry run %+v: %v", apply, dryRun, err)
	}
	assertLogicalFrames(t, logicalFrames(t, writer.db, id), before)
	assertBlocksDoNotOverlap(t, compactor, id)
	page := readPage(t, compactor, id, 0, len(original)+1)
	assertEntries(t, page.Entries, original...)
	inventoryAfter, err := compactor.InventoryEventBlocks(t.Context())
	if err != nil || inventoryAfter.LegacyBlocks != inventoryBefore.LegacyBlocks-1 || inventoryAfter.InvalidBlocks != inventoryBefore.InvalidBlocks {
		t.Fatalf("inventory after repack: before %+v, after %+v, err %v", inventoryBefore, inventoryAfter, err)
	}
	again, err := compactor.RepackSession(t.Context(), id, true, nil)
	if err != nil || again.Entries != int64(len(original)) {
		t.Fatalf("safe rescan did not revisit the committed prefix: %+v, %v", again, err)
	}
	assertLogicalFrames(t, logicalFrames(t, writer.db, id), before)

	suffix := repackTestEntries(4)
	for index, entry := range suffix {
		entry.Cursor = uint64(len(original) + index + 1)
		entry.Origin.Sequence = entry.Cursor
	}
	appendEntries(t, writer, id, suffix...)
	resumed, err := compactor.RepackSession(t.Context(), id, true, nil)
	if err != nil || resumed.Entries != int64(len(original)+len(suffix)) {
		t.Fatalf("fresh rescan did not process the retained prefix and appended suffix: %+v, %v", resumed, err)
	}
	wantAfterSuffix := make(map[uint64][]byte, len(before)+len(suffix))
	for cursor, wire := range before {
		wantAfterSuffix[cursor] = bytes.Clone(wire)
	}
	for _, entry := range suffix {
		wire, err := proto.Marshal(entry)
		if err != nil {
			t.Fatal(err)
		}
		wantAfterSuffix[entry.Cursor] = wire
	}
	assertLogicalFrames(t, logicalFrames(t, writer.db, id), wantAfterSuffix)
}

func TestOnlineRepackRevalidatesDeletionAndSettlementBeforeCommit(t *testing.T) {
	writer := testStore(t)
	id := opened(t, writer)
	original := make([]*eventlogpb.EventEntry, 64)
	for index := range original {
		original[index] = nativeEntry(uint64(index+1), strings.Repeat("settled-frame-", 60))
	}
	appendEntries(t, writer, id, original...)
	seedBlockFromRows(t, writer.db, id, int64s(1, 40), false)
	before := logicalFrames(t, writer.db, id)
	compactor := NewStore(writer.db, Options{EventPackers: repackTestPackers(t)})
	mutated := false
	result, err := compactor.RepackSession(t.Context(), id, true, func(ctx context.Context, _ int64) error {
		if mutated {
			return nil
		}
		mutated = true
		return dbpostgres.Transaction(ctx, writer.db, sql.TxOptions{}, func(tx *sql.Tx) error {
			if _, err := lockRow(ctx, tx, id); err != nil {
				return err
			}
			return RecordSettlement(ctx, tx, id, &historypb.SettledDeltas{
				ItemId: "fixture", CompletionCursor: 64, Ranges: []*historypb.CursorRange{{First: 5, Last: 10}}, ChunkCount: 6,
			})
		})
	})
	if err != nil || !mutated || result.LegacyBlocks != 1 {
		t.Fatalf("settlement during repack: %+v, mutated=%t, err=%v", result, mutated, err)
	}
	want := make(map[uint64][]byte, len(before)-6)
	for cursor, frame := range before {
		if cursor < 5 || cursor > 10 {
			want[cursor] = frame
		}
	}
	assertLogicalFrames(t, logicalFrames(t, writer.db, id), want)
	page := readPage(t, compactor, id, 0, 100)
	if len(page.Removed) != 0 || len(page.Settlements) != 1 || len(page.Settlements[0].GetRanges()) != 1 {
		t.Fatalf("settled range missing from retained read: %+v", page)
	}
	wantEntries := make([]*eventlogpb.EventEntry, 0, len(original)-6)
	for _, entry := range original {
		if entry.Cursor < 5 || entry.Cursor > 10 {
			wantEntries = append(wantEntries, entry)
		}
	}
	assertEntries(t, page.Entries, wantEntries...)
}

func TestOnlineRepackResumesAfterCancellationAndAllowsConcurrentAppendAndReaders(t *testing.T) {
	writer := testStore(t)
	id := opened(t, writer)
	original := repackTestEntries(100)
	appendEntries(t, writer, id, original...)
	before := logicalFrames(t, writer.db, id)
	compactor := NewStore(writer.db, Options{EventPackers: repackTestPackers(t)})
	ctx, cancel := context.WithCancel(t.Context())
	calls := 0
	partial, err := compactor.RepackSession(ctx, id, true, func(context.Context, int64) error {
		calls++
		if calls == 2 {
			cancel()
			return context.Canceled
		}
		return nil
	})
	if !errors.Is(err, context.Canceled) || calls != 2 {
		t.Fatalf("interrupted after a committed batch: calls=%d err=%v", calls, err)
	}
	cancel()
	if partial.Blocks == 0 {
		t.Fatalf("interruption did not leave an atomically committed prefix: %+v", partial)
	}
	restarted := NewStore(writer.db, Options{EventPackers: repackTestPackers(t)})
	resumed, err := restarted.RepackSession(t.Context(), id, true, nil)
	if err != nil || resumed.Entries != int64(len(original)) {
		t.Fatalf("safe rescan after cancellation: %+v %v", resumed, err)
	}
	assertLogicalFrames(t, logicalFrames(t, writer.db, id), before)

	liveID := opened(t, writer)
	liveOriginal := repackTestEntries(100)
	appendEntries(t, writer, liveID, liveOriginal...)
	liveBefore := logicalFrames(t, writer.db, liveID)
	if _, err := writer.db.ExecContext(t.Context(), `UPDATE session_event SET compactable=true WHERE session_id=$1`, liveID); err != nil {
		t.Fatal(err)
	}
	liveCompactor := NewStore(writer.db, Options{EventPackers: repackTestPackers(t)})
	liveWriter := NewStore(writer.db, Options{EventPackers: repackTestPackers(t)})
	var mu sync.Mutex
	wantEntries := append([]*eventlogpb.EventEntry(nil), liveOriginal...)
	readerStop, readerDone := make(chan struct{}), make(chan struct{})
	readerErrors := make(chan error, 1)
	go func() {
		defer close(readerDone)
		ticker := time.NewTicker(time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-readerStop:
				return
			case <-ticker.C:
				page, err := liveCompactor.ReadPage(t.Context(), liveID, 0, 1000)
				if err != nil {
					readerErrors <- err
					return
				}
				mu.Lock()
				want := append([]*eventlogpb.EventEntry(nil), wantEntries...)
				mu.Unlock()
				if len(page.Entries) > len(want) {
					continue // the append committed just before its expected rows were published here.
				}
				if uint64(len(page.Entries)) != page.LastCursor {
					readerErrors <- errors.New("reader observed a gap during repack")
					return
				}
				for index, entry := range page.Entries {
					if !proto.Equal(entry, want[index]) {
						readerErrors <- errors.New("reader observed a gap or changed entry during repack")
						return
					}
				}
			}
		}
	}()
	added := false
	_, err = liveCompactor.RepackSession(t.Context(), liveID, true, func(ctx context.Context, _ int64) error {
		if added {
			return nil
		}
		added = true
		suffix := make([]*eventlogpb.EventEntry, 20)
		for index := range suffix {
			suffix[index] = nativeEntry(uint64(len(liveOriginal)+index+1), strings.Repeat("concurrent-tail-", 50))
		}
		if _, err := liveWriter.Append(ctx, liveID, suffix); err != nil {
			return err
		}
		mu.Lock()
		wantEntries = append(wantEntries, suffix...)
		mu.Unlock()
		return nil
	})
	close(readerStop)
	<-readerDone
	if err != nil || !added {
		t.Fatalf("concurrent append repack: added=%t err=%v", added, err)
	}
	select {
	case err := <-readerErrors:
		t.Fatal(err)
	default:
	}
	wantFrames := make(map[uint64][]byte, len(liveBefore)+20)
	for cursor, frame := range liveBefore {
		wantFrames[cursor] = frame
	}
	for _, entry := range wantEntries[len(liveBefore):] {
		wire, err := proto.Marshal(entry)
		if err != nil {
			t.Fatal(err)
		}
		wantFrames[entry.Cursor] = wire
	}
	assertLogicalFrames(t, logicalFrames(t, writer.db, liveID), wantFrames)
	assertEntries(t, readPage(t, liveCompactor, liveID, 0, 1000).Entries, wantEntries...)
}

func TestOnlineRepackUnpackBatchBudgetRollsBackAndResumes(t *testing.T) {
	writer := testStore(t)
	id := opened(t, writer)
	original := make([]*eventlogpb.EventEntry, 4)
	for index := range original {
		original[index] = nativeEntry(uint64(index+1), strings.Repeat("budget-rollback-frame-", 80))
	}
	appendEntries(t, writer, id, original...)
	seedBlockFromRows(t, writer.db, id, int64s(1, 4), false)
	before := logicalFrames(t, writer.db, id)
	if _, err := writer.db.ExecContext(t.Context(), `CREATE FUNCTION history_test_repack_budget() RETURNS trigger
		LANGUAGE plpgsql AS $$ BEGIN IF NEW.cursor=1 THEN PERFORM pg_sleep(1.0); END IF; RETURN NEW; END $$`); err != nil {
		t.Fatal(err)
	}
	if _, err := writer.db.ExecContext(t.Context(), `CREATE TRIGGER history_test_repack_budget BEFORE INSERT ON session_event
		FOR EACH ROW EXECUTE FUNCTION history_test_repack_budget()`); err != nil {
		t.Fatal(err)
	}
	budget := 350 * time.Millisecond
	compactor := NewStore(writer.db, Options{EventPackers: repackTestPackers(t), RepackBatchBudget: budget})
	if _, err := compactor.RepackSession(t.Context(), id, true, nil); !errors.Is(err, ErrCompactionBatchBudget) {
		t.Fatalf("stalled unpack should exhaust its bounded retries: %v", err)
	}
	if blocks := testpostgres.Count(t, writer.db, "SELECT count(*) FROM session_event_block WHERE session_id=$1", id); blocks != 1 {
		t.Fatalf("timed-out unpack removed its source block: %d blocks remain", blocks)
	}
	if rows := testpostgres.Count(t, writer.db, "SELECT count(*) FROM session_event WHERE session_id=$1", id); rows != 0 {
		t.Fatalf("timed-out unpack left partial ordinary rows: %d remain", rows)
	}
	assertLogicalFrames(t, logicalFrames(t, writer.db, id), before)
	if _, err := writer.db.ExecContext(t.Context(), `DROP TRIGGER history_test_repack_budget ON session_event`); err != nil {
		t.Fatal(err)
	}
	if _, err := writer.db.ExecContext(t.Context(), `DROP FUNCTION history_test_repack_budget()`); err != nil {
		t.Fatal(err)
	}
	restarted := NewStore(writer.db, Options{EventPackers: repackTestPackers(t), RepackBatchBudget: budget})
	resumed, err := restarted.RepackSession(t.Context(), id, true, nil)
	if err != nil || resumed.SourceBlocks != 1 || resumed.Entries != int64(len(original)) {
		t.Fatalf("resume after rolled-back unpack: %+v, %v", resumed, err)
	}
	assertLogicalFrames(t, logicalFrames(t, writer.db, id), before)
}

func TestOnlineRepackAppendWaitIsMeasuredDuringStalledUnpack(t *testing.T) {
	writer := testStore(t)
	id := opened(t, writer)
	original := make([]*eventlogpb.EventEntry, 4)
	for index := range original {
		original[index] = nativeEntry(uint64(index+1), strings.Repeat("stalled-repack-frame-", 80))
	}
	appendEntries(t, writer, id, original...)
	seedBlockFromRows(t, writer.db, id, int64s(1, 4), false)
	before := logicalFrames(t, writer.db, id)
	if _, err := writer.db.ExecContext(t.Context(), `CREATE FUNCTION history_test_repack_stall() RETURNS trigger
		LANGUAGE plpgsql AS $$ BEGIN IF NEW.cursor=1 THEN
		PERFORM set_config('application_name','history-repack-stall-test',true); PERFORM pg_sleep(0.75);
		END IF; RETURN NEW; END $$`); err != nil {
		t.Fatal(err)
	}
	if _, err := writer.db.ExecContext(t.Context(), `CREATE TRIGGER history_test_repack_stall BEFORE INSERT ON session_event
		FOR EACH ROW EXECUTE FUNCTION history_test_repack_stall()`); err != nil {
		t.Fatal(err)
	}
	defer func() {
		_, _ = writer.db.ExecContext(context.Background(), `DROP TRIGGER IF EXISTS history_test_repack_stall ON session_event`)
		_, _ = writer.db.ExecContext(context.Background(), `DROP FUNCTION IF EXISTS history_test_repack_stall()`)
	}()

	budget, stalledFor := 2*time.Second, 750*time.Millisecond
	compactor := NewStore(writer.db, Options{EventPackers: repackTestPackers(t), RepackBatchBudget: budget})
	var logOutput bytes.Buffer
	previousLogger := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&logOutput, nil)))
	defer slog.SetDefault(previousLogger)
	type repackOutcome struct {
		result CompactionResult
		err    error
	}
	repacked := make(chan repackOutcome, 1)
	go func() {
		result, err := compactor.RepackSession(t.Context(), id, true, nil)
		repacked <- repackOutcome{result: result, err: err}
	}()

	waitCtx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	lockerPID := waitForBackendPID(t, waitCtx, writer.db, `SELECT pid FROM pg_stat_activity
		WHERE application_name='history-repack-stall-test' AND wait_event='PgSleep' ORDER BY pid DESC LIMIT 1`)
	appendStarted := time.Now()
	appended := make(chan error, 1)
	tail := nativeEntry(5, "append waiting behind the stalled repack")
	go func() {
		_, err := writer.Append(waitCtx, id, []*eventlogpb.EventEntry{tail})
		appended <- err
	}()
	blockedPID := waitForBackendPID(t, waitCtx, writer.db, `SELECT pid FROM pg_stat_activity
		WHERE wait_event_type='Lock' AND $1=ANY(pg_blocking_pids(pid)) ORDER BY pid DESC LIMIT 1`, lockerPID)
	testpostgres.WaitForBlocker(t, waitCtx, writer.db, blockedPID, lockerPID)

	var outcome repackOutcome
	select {
	case outcome = <-repacked:
	case <-waitCtx.Done():
		t.Fatal("stalled History repack did not finish within the test deadline")
	}
	if outcome.err != nil || outcome.result.SourceBlocks != 1 {
		t.Fatalf("stalled repack: %+v, %v", outcome.result, outcome.err)
	}
	select {
	case err := <-appended:
		if err != nil {
			t.Fatal(err)
		}
	case <-waitCtx.Done():
		t.Fatal("append remained blocked after the repack transaction committed")
	}
	appendWait := time.Since(appendStarted)
	if appendWait < stalledFor/2 || appendWait > budget+time.Second {
		t.Fatalf("append wait %s does not reflect the %s repack stall under %s budget", appendWait, stalledFor, budget)
	}

	type batchLog struct {
		Message   string `json:"msg"`
		Operation string `json:"operation"`
		LockWait  int64  `json:"lock_wait"`
		LockHold  int64  `json:"lock_hold"`
		Budget    int64  `json:"budget"`
		Outcome   string `json:"outcome"`
	}
	var measured *batchLog
	for _, line := range strings.Split(logOutput.String(), "\n") {
		var record batchLog
		if err := json.Unmarshal([]byte(line), &record); err != nil || record.Message != "History repack batch transaction" ||
			record.Operation != "unpack" || record.Outcome != "committed" {
			continue
		}
		measured = &record
		break
	}
	if measured == nil || measured.Budget != int64(budget) || measured.LockHold < int64(stalledFor/2) ||
		measured.LockHold >= int64(budget) || measured.LockWait < 0 {
		t.Fatalf("missing or invalid separate lock wait/hold measurement (budget %s): %s", budget, logOutput.String())
	}
	t.Logf("synthetic canary batch budget=%s repack_lock_wait=%s repack_lock_hold=%s blocked_append_wait=%s",
		budget, time.Duration(measured.LockWait), time.Duration(measured.LockHold), appendWait)

	want := make(map[uint64][]byte, len(before)+1)
	for cursor, frame := range before {
		want[cursor] = bytes.Clone(frame)
	}
	tailWire, err := proto.Marshal(tail)
	if err != nil {
		t.Fatal(err)
	}
	want[tail.Cursor] = tailWire
	assertLogicalFrames(t, logicalFrames(t, writer.db, id), want)
}

func waitForBackendPID(t *testing.T, ctx context.Context, db *sql.DB, query string, args ...any) int {
	t.Helper()
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		var pid int
		err := db.QueryRowContext(ctx, query, args...).Scan(&pid)
		if err == nil {
			return pid
		}
		if !errors.Is(err, sql.ErrNoRows) {
			t.Fatalf("query for PostgreSQL backend PID: %v", err)
		}
		select {
		case <-ctx.Done():
			t.Fatalf("waiting for PostgreSQL backend PID: %v", ctx.Err())
		case <-ticker.C:
		}
	}
}

func TestEventBlockInventoryCountsInvalidSeparately(t *testing.T) {
	store := testStore(t)
	id := opened(t, store)
	appendEntries(t, store, id, nativeEntry(1, "inventory"))
	before, err := store.InventoryEventBlocks(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.ExecContext(t.Context(), `INSERT INTO session_event_block
		(session_id,first_cursor,last_cursor,kind,cursors,sizes,payload) VALUES($1,1,1,$2,'{1}','{9}','\x41')`,
		id, historypb.EventKind_EVENT_KIND_NATIVE); err != nil {
		t.Fatal(err)
	}
	after, err := store.InventoryEventBlocks(t.Context())
	if err != nil || after.InvalidBlocks != before.InvalidBlocks+1 || after.Blocks != before.Blocks+1 ||
		after.LegacyBlocks != before.LegacyBlocks || after.VersionedBlocks != before.VersionedBlocks {
		t.Fatalf("invalid block inventory: before %+v, after %+v, err %v", before, after, err)
	}
}

func int64s(first, last int64) []int64 {
	values := make([]int64, last-first+1)
	for index := range values {
		values[index] = first + int64(index)
	}
	return values
}

func TestRepackGroupsOutputBlocksInBoundedTransactions(t *testing.T) {
	writer := testStore(t)
	for _, limit := range []int{1, 4} {
		id := opened(t, writer)
		original := make([]*eventlogpb.EventEntry, 48)
		for i := range original {
			original[i] = nativeEntry(uint64(i+1), strings.Repeat("repeated fixture input ", 32))
		}
		appendEntries(t, writer, id, original...)
		before := logicalFrames(t, writer.db, id)
		store := NewStore(writer.db, Options{EventPackers: repackTestPackers(t), RepackBlocksPerTxn: limit})
		if _, err := store.RepackSession(t.Context(), id, true, nil); err != nil {
			t.Fatal(err)
		}
		assertLogicalFrames(t, logicalFrames(t, writer.db, id), before)
		got := testpostgres.Count(t, writer.db, `SELECT max(n) FROM (SELECT count(*) AS n FROM session_event_block WHERE session_id=$1 GROUP BY xmin) AS batches`, id)
		if got != limit {
			t.Fatalf("largest transaction installed %d blocks, want %d", got, limit)
		}
	}
}
