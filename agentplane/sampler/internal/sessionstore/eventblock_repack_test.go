package sessionstore

import (
	"bytes"
	"compress/gzip"
	"context"
	"database/sql"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
)

func appendAPBTestJournalThrough(t *testing.T, store *Store, id string, through uint64) {
	t.Helper()
	shareable := []byte(strings.Repeat("shared event evidence ", 28))
	if err := store.transaction(t.Context(), "sampler.test_fixture", func(ctx context.Context, tx *sql.Tx) error {
		_, session, err := lockSession(ctx, tx, id)
		if err != nil {
			return err
		}
		for session.Cursor < through {
			payload := shareable
			if session.Cursor%2 == 0 {
				payload = shareable[:64]
			}
			if _, err := store.appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_BoundaryRecord{
				BoundaryRecord: &eventpb.BoundaryRecord{Payload: payload, MediaType: "text/plain", CorrelationId: "fixture"},
			}}); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

func eventFrames(t *testing.T, store *Store, sessionID string, first, last uint64) [][]byte {
	t.Helper()
	stored, err := journalFrames(t.Context(), store.DB(), sessionID, first, last)
	if err != nil {
		t.Fatal(err)
	}
	frames := make([][]byte, len(stored))
	for i, frame := range stored {
		frames[i], err = eventcodec.Uncompress(frame.wire)
		if err != nil {
			t.Fatal(err)
		}
	}
	return frames
}

func replaceRowsWithBlock(t *testing.T, store *Store, sessionID string, first, last uint64, legacy bool) {
	t.Helper()
	frames := eventFrames(t, store, sessionID, first, last)
	var payload []byte
	var err error
	if legacy {
		payload, err = eventcodec.PackBlock(frames)
	} else {
		packer, packerErr := eventblock.NewPacker(eventblock.Profile{TargetBytes: 64 << 10, TargetEntries: eventblock.MaxEntries, Level: 3})
		if packerErr != nil {
			t.Fatal(packerErr)
		}
		payload, err = packer.Encode(frames)
	}
	if err != nil {
		t.Fatal(err)
	}
	if err := store.transaction(t.Context(), "sampler.test_fixture", func(ctx context.Context, tx *sql.Tx) error {
		if _, err := tx.ExecContext(ctx, `DELETE FROM sampler_event WHERE session_id=$1 AND sequence BETWEEN $2 AND $3`, sessionID, first, last); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, insertJournalBlock, sessionID, first, last, payload)
		return err
	}); err != nil {
		t.Fatal(err)
	}
}

func assertFramesEqual(t *testing.T, got, want [][]byte) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("frame count=%d, want %d", len(got), len(want))
	}
	for i := range want {
		if !bytes.Equal(got[i], want[i]) {
			t.Fatalf("frame %d changed", i)
		}
	}
}

func TestLegacyJournalWorkerRejectsOverlapWithLongAPBBlock(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	const first = 2*journalWindowSize + 1
	const last = 3 * journalWindowSize
	appendAPBTestJournalThrough(t, store, session.ID, last)
	// Simulate retained storage with one cursor present in both tables.
	duplicate := eventFrames(t, store, session.ID, first, first)[0]
	replaceRowsWithBlock(t, store, session.ID, 1, first, false)
	migrationExec(t, store.DB(), `INSERT INTO sampler_event(session_id,sequence,entry) VALUES ($1,$2,$3)`,
		session.ID, first, duplicate)
	err := store.compactPendingJournal(t.Context(), pendingJournalCompaction{id: session.ID, cursor: last, after: first - 1})
	if err == nil || !strings.Contains(err.Error(), "existing journal block overlaps") {
		t.Fatalf("packing a window starting inside an APB block returned %v", err)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event WHERE session_id=$1`, session.ID); got != journalWindowSize {
		t.Fatalf("rejected window left %d source rows, want %d", got, journalWindowSize)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event_block WHERE session_id=$1`, session.ID); got != 1 {
		t.Fatalf("rejected window left %d blocks, want the original APB block", got)
	}
}

func TestCompactJournalTranscodesLegacyAndCurrentBlocks(t *testing.T) {
	store := newTestStore(t)
	store.eventBlockProfile = eventblock.Profile{TargetBytes: 64 << 10, TargetEntries: 64, Level: 3}
	session := openSessionForStore(t, store, "")
	appendAPBTestJournalThrough(t, store, session.ID, 300)
	// A one-event APB block ends exactly on the first cursor of the apply range.
	replaceRowsWithBlock(t, store, session.ID, 1, 1, false)
	replaceRowsWithBlock(t, store, session.ID, 2, 32, true)
	replaceRowsWithBlock(t, store, session.ID, 33, 64, false)
	migrationExec(t, store.DB(), `DELETE FROM sampler_event WHERE session_id=$1 AND sequence=150`, session.ID)
	want := eventFrames(t, store, session.ID, 1, 300)

	before, err := store.VerifyJournalInventory(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(before.Owners) != 1 || before.Owners[0].LegacyGzipBlocks != 1 || before.Owners[0].CurrentBlocks != 2 {
		t.Fatalf("pre-repack inventory=%+v", before.Owners)
	}
	if before.Owners[0].CoverageGaps != 1 || before.Owners[0].MissingSequences != 1 {
		t.Fatalf("pre-repack inventory did not report the skipped cursor: %+v", before.Owners[0])
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event_block WHERE session_id=$1`, session.ID); got != 3 {
		t.Fatalf("dry-run fixture started with %d blocks", got)
	}

	proposal, err := store.CompactJournal(t.Context(), session.ID, false)
	if err != nil || proposal.Events != int64(len(want)) || proposal.Blocks == 0 {
		t.Fatalf("dry-run proposal=%+v err=%v", proposal, err)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event_block WHERE session_id=$1`, session.ID); got != 3 {
		t.Fatalf("dry run changed the block count to %d", got)
	}
	result, err := store.CompactJournal(t.Context(), session.ID, true)
	if err != nil || !sameJournalCompactionWork(result, proposal) {
		t.Fatalf("apply result=%+v proposal=%+v err=%v", result, proposal, err)
	}

	after, err := store.VerifyJournalInventory(t.Context())
	if err != nil || len(after.Owners) != 1 {
		t.Fatalf("post-repack inventory=%+v err=%v", after, err)
	}
	owner := after.Owners[0]
	if owner.LegacyGzipBlocks != 0 || owner.CurrentBlocks == 0 || owner.UnsupportedBlocks != 0 || owner.InvalidBlocks != 0 {
		t.Fatalf("legacy or unclassified blocks remain: %+v", owner)
	}
	if err := JournalInventoryError(after); err != nil {
		t.Fatalf("verified repack did not prove zero legacy blocks: %v", err)
	}
	assertFramesEqual(t, eventFrames(t, store, session.ID, 1, 300), want)
	// A second pass scans every frame but must leave matching rows and blocks in place.
	migrationExec(t, store.DB(), `CREATE FUNCTION reject_journal_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN RAISE EXCEPTION 'second repack changed a row in %', TG_TABLE_NAME; END; $$`)
	migrationExec(t, store.DB(), `CREATE TRIGGER reject_event_rewrite BEFORE INSERT OR UPDATE OR DELETE ON sampler_event
		FOR EACH ROW EXECUTE FUNCTION reject_journal_rewrite()`)
	migrationExec(t, store.DB(), `CREATE TRIGGER reject_block_rewrite BEFORE INSERT OR UPDATE OR DELETE ON sampler_event_block
		FOR EACH ROW EXECUTE FUNCTION reject_journal_rewrite()`)
	if repeated, err := store.CompactJournal(t.Context(), session.ID, true); err != nil || !repeated.Completed || repeated.Events != int64(len(want)) {
		t.Fatalf("idempotent apply result=%+v err=%v", repeated, err)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM information_schema.tables
		WHERE table_schema='public' AND table_name='sampler_event_block_checkpoint'`); got != 0 {
		t.Fatalf("repack created %d persistent checkpoint tables", got)
	}
}

func sameJournalCompactionWork(left, right JournalCompactionResult) bool {
	return left.Events == right.Events && left.Blocks == right.Blocks &&
		left.StoredBytes == right.StoredBytes && left.PackedBytes == right.PackedBytes &&
		left.OutputBytes == right.OutputBytes && left.RetainedLegacyBlocks == right.RetainedLegacyBlocks &&
		left.OpeningCursor == right.OpeningCursor && left.Batches == right.Batches && left.Completed == right.Completed
}

func TestCompactJournalRestartRescansCommittedBatchesFromBeginning(t *testing.T) {
	store := newTestStore(t)
	store.eventBlockProfile = eventblock.Profile{TargetBytes: 64 << 10, TargetEntries: 64, Level: 3}
	session := openSessionForStore(t, store, "")
	const opening = 600
	appendAPBTestJournalThrough(t, store, session.ID, opening)
	want := eventFrames(t, store, session.ID, 1, opening)
	firstFrames := eventFrames(t, store, session.ID, 1, 256)

	migrationExec(t, store.DB(), `CREATE FUNCTION reject_later_event_blocks() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN
			IF NEW.first_sequence >= 257 THEN RAISE EXCEPTION 'fixture rejects later block'; END IF;
			RETURN NEW;
		END; $$`)
	migrationExec(t, store.DB(), `CREATE TRIGGER reject_later_event_blocks BEFORE INSERT ON sampler_event_block
		FOR EACH ROW EXECUTE FUNCTION reject_later_event_blocks()`)
	if result, err := store.CompactJournal(t.Context(), session.ID, true); err == nil || result.Events != 256 || result.Blocks == 0 {
		t.Fatalf("first partial apply result=%+v err=%v", result, err)
	}
	assertFramesEqual(t, eventFrames(t, store, session.ID, 1, 256), firstFrames)
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event_block WHERE session_id=$1 AND last_sequence<=256`, session.ID); got == 0 {
		t.Fatal("first batch did not commit before the later range was rejected")
	}

	migrationExec(t, store.DB(), `DROP TRIGGER reject_later_event_blocks ON sampler_event_block`)
	migrationExec(t, store.DB(), `DROP FUNCTION reject_later_event_blocks()`)
	appendAPBTestJournalThrough(t, store, session.ID, opening+1)
	appended := eventFrames(t, store, session.ID, opening+1, opening+1)
	result, err := store.CompactJournal(t.Context(), session.ID, true)
	if err != nil || !result.Completed {
		t.Fatalf("restart apply result=%+v err=%v", result, err)
	}
	if result.OpeningCursor != opening+1 || result.Events != int64(opening+1) {
		t.Fatalf("restart did not capture and rescan the full current history: %+v", result)
	}
	want = append(want, appended[0])
	assertFramesEqual(t, eventFrames(t, store, session.ID, 1, opening+1), want)
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM information_schema.tables
		WHERE table_schema='public' AND table_name='sampler_event_block_checkpoint'`); got != 0 {
		t.Fatalf("restart created %d persistent checkpoint tables", got)
	}
}

func appendOneJournalEvent(ctx context.Context, store *Store, sessionID string) error {
	return store.transaction(ctx, "sampler.test_fixture", func(ctx context.Context, tx *sql.Tx) error {
		_, session, err := lockSession(ctx, tx, sessionID)
		if err != nil {
			return err
		}
		_, err = store.appendEvent(ctx, tx, session, &eventpb.Event{Observation: &eventpb.Event_BoundaryRecord{
			BoundaryRecord: &eventpb.BoundaryRecord{Payload: []byte("appended while repacking"), MediaType: "text/plain", CorrelationId: "concurrent"},
		}})
		return err
	})
}

func TestRepackCompareAndSwapDetectsLiveWriterSealingPreparedRows(t *testing.T) {
	store, dsn := newTestStoreWithDSN(t)
	store.eventBlockProfile = eventblock.Profile{TargetBytes: 64 << 10, TargetEntries: 32, Level: 3}
	session := openSessionForStore(t, store, "")
	appendAPBTestJournalThrough(t, store, session.ID, 32)
	want := eventFrames(t, store, session.ID, 1, 32)
	opening, err := store.CurrentCursor(t.Context(), session.ID)
	state := journalRepackState{opening: opening}
	if err != nil || state.opening != 32 {
		t.Fatalf("open repack state=%+v err=%v", state, err)
	}
	plan, err := store.prepareJournalRepack(t.Context(), session.ID, state)
	if err != nil || len(plan.sources) == 0 {
		t.Fatalf("prepare source rows=%d err=%v", len(plan.sources), err)
	}

	liveDB := postgres.Open(t, dsn)
	live := New(liveDB, 30*time.Second, &settings.Settings{
		EventBlockV2Writes: true, EventBlockProfile: eventblock.Profile{TargetBytes: 64 << 10, TargetEntries: 32, Level: 3},
	})
	if err := appendOneJournalEvent(t.Context(), live, session.ID); err != nil {
		t.Fatal(err)
	}
	if got := postgres.Count(t, live.DB(), `SELECT count(*) FROM sampler_event_block WHERE session_id=$1 AND first_sequence=1`, session.ID); got != 1 {
		t.Fatalf("live writer did not seal the prepared rows: %d blocks", got)
	}
	if _, err := store.applyJournalRepack(t.Context(), session.ID, state, plan, JournalLockBudget{Wait: DefaultJournalLockWait, Hold: DefaultJournalLockHold}); !errors.Is(err, ErrJournalBatchChanged) {
		t.Fatalf("stale prepared batch error=%v, want ErrJournalBatchChanged", err)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event_block WHERE session_id=$1 AND first_sequence=1`, session.ID); got != 1 {
		t.Fatalf("stale batch changed the live writer's replacement block count to %d", got)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event WHERE session_id=$1 AND sequence=33`, session.ID); got != 1 {
		t.Fatalf("stale batch lost the live writer's appended row")
	}
	result, err := store.CompactJournalWithOptions(t.Context(), session.ID, true, JournalCompactionOptions{})
	if err != nil || !result.Completed {
		t.Fatalf("CAS retry result=%+v err=%v", result, err)
	}
	assertFramesEqual(t, eventFrames(t, store, session.ID, 1, 32), want)
	inventory, err := store.VerifyJournalInventory(t.Context())
	if err != nil || JournalInventoryError(inventory) != nil {
		t.Fatalf("post-CAS inventory=%+v err=%v verify=%v", inventory, err, JournalInventoryError(inventory))
	}
}

func TestRepackKeepsLiveReadsAndAppendsAvailableDuringBoundedLock(t *testing.T) {
	store, dsn := newTestStoreWithDSN(t)
	store.eventBlockProfile = eventblock.Profile{TargetBytes: 64 << 10, TargetEntries: 32, Level: 3}
	session := openSessionForStore(t, store, "")
	appendAPBTestJournalThrough(t, store, session.ID, 64)
	want := eventFrames(t, store, session.ID, 1, 64)

	readerDB := postgres.Open(t, dsn)
	reader := New(readerDB, 30*time.Second, nil)
	migrationExec(t, store.DB(), `CREATE FUNCTION delay_repack_delete() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN
			IF OLD.sequence=1 THEN PERFORM pg_sleep(0.10); END IF;
			RETURN OLD;
		END; $$`)
	migrationExec(t, store.DB(), `CREATE TRIGGER delay_repack_delete BEFORE DELETE ON sampler_event
		FOR EACH ROW EXECUTE FUNCTION delay_repack_delete()`)

	type repackOutcome struct {
		result JournalCompactionResult
		err    error
	}
	repackDone := make(chan repackOutcome, 1)
	go func() {
		result, err := store.CompactJournalWithOptions(t.Context(), session.ID, true, JournalCompactionOptions{
			ContentionRetries: 2, RetryDelay: 10 * time.Millisecond,
		})
		repackDone <- repackOutcome{result: result, err: err}
	}()

	deadline := time.Now().Add(5 * time.Second)
	for {
		var active bool
		err := reader.DB().QueryRowContext(t.Context(), `SELECT EXISTS(
			SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND state='active'
			AND query LIKE 'DELETE FROM sampler_event WHERE session_id%')`).Scan(&active)
		if err != nil {
			t.Fatal(err)
		}
		if active {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("repack batch transaction did not enter its bounded lock window")
		}
		time.Sleep(5 * time.Millisecond)
	}

	readStart := time.Now()
	assertFramesEqual(t, eventFrames(t, reader, session.ID, 1, 64), want)
	if time.Since(readStart) >= 100*time.Millisecond {
		t.Fatalf("reader was blocked by repack lock for %s", time.Since(readStart))
	}
	appendDone := make(chan error, 1)
	appendStart := time.Now()
	go func() { appendDone <- appendOneJournalEvent(t.Context(), reader, session.ID) }()

	var outcome repackOutcome
	select {
	case outcome = <-repackDone:
	case <-time.After(5 * time.Second):
		t.Fatal("repack did not finish its bounded lock transaction")
	}
	if outcome.err != nil || !outcome.result.Completed {
		t.Fatalf("concurrent repack result=%+v err=%v", outcome.result, outcome.err)
	}
	if outcome.result.LockHoldMax <= 0 || outcome.result.LockHoldMax > DefaultJournalLockHold {
		t.Fatalf("measured lock hold=%s, limit=%s", outcome.result.LockHoldMax, DefaultJournalLockHold)
	}
	select {
	case err := <-appendDone:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("live append did not complete after the short repack commit")
	}
	if time.Since(appendStart) > time.Second {
		t.Fatalf("live append waited %s for repack", time.Since(appendStart))
	}
	current, err := reader.CurrentCursor(t.Context(), session.ID)
	if err != nil || current != 65 {
		t.Fatalf("live cursor=%d err=%v", current, err)
	}
	got := eventFrames(t, reader, session.ID, 1, 65)
	if len(got) != 65 {
		t.Fatalf("concurrent append frame count=%d", len(got))
	}
	assertFramesEqual(t, got[:64], want)
	if got[64] == nil {
		t.Fatal("concurrent appender wrote an empty frame")
	}
	post, err := store.VerifyJournalInventory(t.Context())
	if err != nil || JournalInventoryError(post) != nil {
		t.Fatalf("post-concurrency inventory=%+v err=%v verify=%v", post, err, JournalInventoryError(post))
	}
}

func TestCompactJournalKeepsCurrentBlocksAtTheSameProfile(t *testing.T) {
	store, dsn := newTestStoreWithDSN(t)
	profile := eventblock.Profile{TargetBytes: 64 << 10, TargetEntries: 4, Level: 3}
	store.eventBlockProfile = profile
	writerDB := postgres.Open(t, dsn)
	writer := New(writerDB, 30*time.Second, &settings.Settings{
		EventBlockV2Writes: true, EventBlockProfile: profile,
	})
	session := openSessionForStore(t, store, "")
	for range profile.TargetEntries {
		if err := appendOneJournalEvent(t.Context(), writer, session.ID); err != nil {
			t.Fatal(err)
		}
	}
	var before []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT payload FROM sampler_event_block WHERE session_id=$1`, session.ID).Scan(&before); err != nil {
		t.Fatal(err)
	}
	if result, err := store.CompactJournal(t.Context(), session.ID, true); err != nil || !result.Completed {
		t.Fatalf("same-profile repack result=%+v err=%v", result, err)
	}
	var after []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT payload FROM sampler_event_block WHERE session_id=$1`, session.ID).Scan(&after); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(after, before) {
		t.Fatal("same-profile repack expanded or rewrote an already-current block")
	}
}

func TestInventoryValidatesOwnerCoverageAndEncodingClasses(t *testing.T) {
	store := newTestStore(t)
	legacy := openSessionForStore(t, store, "")
	appendAPBTestJournalThrough(t, store, legacy.ID, 3)
	replaceRowsWithBlock(t, store, legacy.ID, 1, 2, true)
	legacyWire := eventFrames(t, store, legacy.ID, 3, 3)
	var compressed bytes.Buffer
	gzipWriter := gzip.NewWriter(&compressed)
	if _, err := gzipWriter.Write(legacyWire[0]); err != nil {
		t.Fatal(err)
	}
	if err := gzipWriter.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_event SET entry=$3 WHERE session_id=$1 AND sequence=$2`,
		legacy.ID, 3, compressed.Bytes()); err != nil {
		t.Fatal(err)
	}

	current, err := openStoreSession(t, store, &sessionconfig.Configuration{
		SessionId: uuid.NewString(),
		Spec:      &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"},
	}, "another-owner", protocol.APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	appendAPBTestJournalThrough(t, store, current.ID, 1)
	replaceRowsWithBlock(t, store, current.ID, 1, 1, false)

	bad, err := openStoreSession(t, store, &sessionconfig.Configuration{
		SessionId: uuid.NewString(),
		Spec:      &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"},
	}, "another-owner", protocol.APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	appendAPBTestJournalThrough(t, store, bad.ID, 1)
	if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_event SET entry=$3 WHERE session_id=$1 AND sequence=$2`, bad.ID, 1, []byte{0xff}); err != nil {
		t.Fatal(err)
	}

	unsupported, err := openStoreSession(t, store, &sessionconfig.Configuration{
		SessionId: uuid.NewString(),
		Spec:      &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model"},
	}, "another-owner", protocol.APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	appendAPBTestJournalThrough(t, store, unsupported.ID, 2)
	migrationExec(t, store.DB(), `DELETE FROM sampler_event WHERE session_id=$1`, unsupported.ID)
	if _, err := store.DB().ExecContext(t.Context(), insertJournalBlock, unsupported.ID, 1, 2, []byte("future-block")); err != nil {
		t.Fatal(err)
	}

	result, err := store.VerifyJournalInventory(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Owners) != 2 || result.Owners[0].Owner != "another-owner" || result.Owners[1].Owner != "fixture-account" {
		t.Fatalf("owner inventory order/coverage=%+v", result.Owners)
	}
	if result.Owners[0].Sessions != 3 || result.Owners[0].CurrentBlocks != 1 ||
		result.Owners[0].UnsupportedBlocks != 1 || result.Owners[0].InvalidRows != 1 {
		t.Fatalf("other-owner inventory=%+v", result.Owners[0])
	}
	if result.Owners[1].Sessions != 1 || result.Owners[1].LegacyGzipBlocks != 1 || result.Owners[1].LegacyGzipRows != 1 {
		t.Fatalf("legacy-owner inventory=%+v", result.Owners[1])
	}
	if err := JournalInventoryError(result); err == nil || !strings.Contains(err.Error(), "legacy_gzip_blocks=1") ||
		!strings.Contains(err.Error(), "unsupported_records=1") || !strings.Contains(err.Error(), "invalid_records=1") {
		t.Fatalf("verification error=%v", err)
	}
}

func TestRepackPreservesMisplacedSingletonForRecovery(t *testing.T) {
	for _, misplaced := range []string{"APB in event row", "event in block row"} {
		t.Run(misplaced, func(t *testing.T) {
			store := newTestStore(t)
			session := openSessionForStore(t, store, "")
			appendAPBTestJournalThrough(t, store, session.ID, 3)
			want := eventFrames(t, store, session.ID, 1, 3)
			if misplaced == "APB in event row" {
				replaceRowsWithBlock(t, store, session.ID, 2, 2, false)
				migrationExec(t, store.DB(), `WITH moved AS (
					DELETE FROM sampler_event_block WHERE session_id=$1 AND first_sequence=2 RETURNING payload)
					INSERT INTO sampler_event(session_id,sequence,entry) SELECT $1,2,payload FROM moved`, session.ID)
			} else {
				migrationExec(t, store.DB(), `WITH moved AS (
					DELETE FROM sampler_event WHERE session_id=$1 AND sequence=2 RETURNING entry)
					INSERT INTO sampler_event_block(session_id,first_sequence,last_sequence,payload) SELECT $1,2,2,entry FROM moved`, session.ID)
			}
			for _, apply := range []bool{false, true} {
				if result, err := store.CompactJournal(t.Context(), session.ID, apply); err == nil || result.Batches != 0 {
					t.Errorf("apply=%v repacked misplaced record: batches=%d err=%v", apply, result.Batches, err)
				}
			}
			inventory, err := store.VerifyJournalInventory(t.Context())
			if err != nil || len(inventory.Issues) != 1 || inventory.Issues[0].Classification != journalUnsupported || inventory.Issues[0].First != 2 {
				t.Fatalf("inventory after repack attempts: %+v, %v", inventory.Issues, err)
			}
			assertFramesEqual(t, eventFrames(t, store, session.ID, 1, 3), want)
		})
	}
}

func TestInventoryDistinguishesInvalidBlockFromUnsupportedBlock(t *testing.T) {
	if class, _ := classifyJournalBlock([]byte("APB\x01bad"), 1, 1); class != "invalid" {
		t.Fatalf("malformed supported block classified as %q", class)
	}
	if class, _ := classifyJournalBlock([]byte("APB\x02future"), 1, 1); class != "unsupported" {
		t.Fatalf("future APB block classified as %q", class)
	}
	if class, _ := classifyJournalBlock([]byte("not-a-block"), 1, 1); class != "unsupported" {
		t.Fatalf("unknown block classified as %q", class)
	}
}

func TestInventoryReportsOverlappingJournalStorage(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	appendAPBTestJournalThrough(t, store, session.ID, 2)
	frames := eventFrames(t, store, session.ID, 1, 1)
	packer, err := eventblock.NewPacker(eventblock.Profile{TargetBytes: 64 << 10, TargetEntries: 1, Level: 3})
	if err != nil {
		t.Fatal(err)
	}
	payload, err := packer.Encode(frames)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.DB().ExecContext(t.Context(), insertJournalBlock, session.ID, 1, 1, payload); err != nil {
		t.Fatal(err)
	}
	inventory, err := store.VerifyJournalInventory(t.Context())
	if err != nil || len(inventory.Owners) != 1 || inventory.Owners[0].InvalidRanges != 1 {
		t.Fatalf("overlap inventory=%+v err=%v", inventory, err)
	}
	if err := JournalInventoryError(inventory); err == nil || !strings.Contains(err.Error(), "invalid_ranges=1") {
		t.Fatalf("overlap verification error=%v", err)
	}
}

func TestRepackBlockLimitPreservesCrossingSourceBetweenCommits(t *testing.T) {
	store := newTestStore(t)
	store.eventBlockProfile = eventblock.Profile{TargetBytes: 64 << 10, TargetEntries: 16, Level: 3}
	session := openSessionForStore(t, store, "")
	const opening = 96
	appendAPBTestJournalThrough(t, store, session.ID, opening)
	want := eventFrames(t, store, session.ID, 1, opening)
	// The first 16-entry output ends inside a 32-entry gzip source.
	replaceRowsWithBlock(t, store, session.ID, 9, 40, true)
	options := JournalCompactionOptions{MaxBlocksPerTxn: 1}
	preview, err := store.CompactJournalWithOptions(t.Context(), session.ID, false, options)
	if err != nil || preview.Blocks < 2 || preview.Batches != preview.Blocks {
		t.Fatalf("preview: %+v, %v", preview, err)
	}
	migrationExec(t, store.DB(), `CREATE FUNCTION reject_next_block() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF NEW.first_sequence > 16 THEN RAISE EXCEPTION 'second transaction'; END IF; RETURN NEW; END; $$`)
	migrationExec(t, store.DB(), `CREATE TRIGGER reject_next_block BEFORE INSERT ON sampler_event_block FOR EACH ROW EXECUTE FUNCTION reject_next_block()`)
	partial, err := store.CompactJournalWithOptions(t.Context(), session.ID, true, options)
	if err == nil || partial.Batches != 1 || partial.Blocks != 1 {
		t.Fatalf("first transaction: %+v, %v", partial, err)
	}
	assertFramesEqual(t, eventFrames(t, store, session.ID, 1, opening), want)
	migrationExec(t, store.DB(), `DROP TRIGGER reject_next_block ON sampler_event_block`)
	result, err := store.CompactJournalWithOptions(t.Context(), session.ID, true, JournalCompactionOptions{})
	if err != nil || !result.Completed || result.Batches >= preview.Batches {
		t.Fatalf("multi-block restart: %+v, %v", result, err)
	}
	assertFramesEqual(t, eventFrames(t, store, session.ID, 1, opening), want)
	maxInstalled := postgres.Count(t, store.DB(), `SELECT max(n) FROM (SELECT count(*) AS n FROM sampler_event_block WHERE session_id=$1 GROUP BY xmin) AS batches`, session.ID)
	if maxInstalled != DefaultJournalBlocksPerTxn {
		t.Fatalf("largest committed group = %d, want %d", maxInstalled, DefaultJournalBlocksPerTxn)
	}
}

func TestRepackSplitBlockPreservesPendingSamplingProjection(t *testing.T) {
	store, session, owner := claimedSession(t)
	const commandID = "split-block-test"
	admitInput(t, store, session.ID, commandID, "Start")
	if _, err := store.NextCommand(t.Context(), owner, session.ID); err != nil {
		t.Fatal(err)
	}
	request, prepare := providerAttemptTestRequest(PendingIntentSampling)
	intentID, _, err := store.begin(t.Context(), owner, request, prepare, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	dispatchSequence := func() int {
		t.Helper()
		var seq int
		if err := store.DB().QueryRowContext(t.Context(), `SELECT dispatch_sequence FROM sampler_intent WHERE id=$1 AND dispatch_turn_id=$2`, intentID, commandID).Scan(&seq); err != nil {
			t.Fatal(err)
		}
		return seq
	}
	want := dispatchSequence()
	cursor, err := store.CurrentCursor(t.Context(), session.ID)
	if err != nil {
		t.Fatal(err)
	}
	store.eventBlockProfile = eventblock.Profile{TargetBytes: 64 << 10, TargetEntries: 16, Level: 3}
	opening := cursor + 96
	appendAPBTestJournalThrough(t, store, session.ID, opening)
	replaceRowsWithBlock(t, store, session.ID, cursor+9, cursor+40, true)
	state := journalRepackState{through: cursor, opening: opening}
	page, err := store.prepareJournalRepack(t.Context(), session.ID, state)
	if err != nil {
		t.Fatal(err)
	}
	batches, err := splitJournalPage(page, 1)
	if err != nil || len(batches) < 2 {
		t.Fatalf("split into %d batches: %v", len(batches), err)
	}
	if _, err := store.applyJournalRepack(t.Context(), session.ID, state, batches[0], JournalLockBudget{Wait: DefaultJournalLockWait, Hold: DefaultJournalLockHold}); err != nil {
		t.Fatal(err)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event WHERE session_id=$1 AND sequence BETWEEN $2 AND $3`, session.ID, cursor+17, cursor+40); got != 24 {
		t.Fatalf("reinserted suffix rows=%d, want 24", got)
	}
	if got := dispatchSequence(); got != want {
		t.Fatalf("dispatch sequence after physical rewrite=%d, want %d", got, want)
	}
}

func TestRepackMigratesSmallerLegacySource(t *testing.T) {
	store := newTestStore(t)
	store.eventBlockProfile = eventblock.DefaultProfile(1)
	session := openSessionForStore(t, store, "")
	const through = 32
	appendAPBTestJournalThrough(t, store, session.ID, through)
	replaceRowsWithBlock(t, store, session.ID, 1, through, true)
	want := eventFrames(t, store, session.ID, 1, through)
	for _, apply := range []bool{false, true} {
		result, err := store.CompactJournal(t.Context(), session.ID, apply)
		if err != nil || !result.Completed || result.OutputBytes <= result.StoredBytes || result.RetainedLegacyBlocks != 0 {
			t.Fatalf("apply=%v result=%+v err=%v", apply, result, err)
		}
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event_block
		WHERE session_id=$1 AND substring(payload FROM 1 FOR 2)=decode('1f8b','hex')`, session.ID); got != 0 {
		t.Fatalf("legacy blocks after apply=%d", got)
	}
	assertFramesEqual(t, eventFrames(t, store, session.ID, 1, through), want)
}

func TestRepackUsesConfiguredTransactionLocalLockWait(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	_, err := store.withJournalSessionLock(t.Context(), session.ID, JournalLockBudget{Wait: 310 * time.Millisecond, Hold: time.Second}, func(ctx context.Context, tx *sql.Tx, _ uint64) error {
		var timeout string
		if err := tx.QueryRowContext(ctx, "SHOW lock_timeout").Scan(&timeout); err != nil {
			return err
		}
		if timeout != "310ms" {
			t.Errorf("lock_timeout = %q, want 310ms", timeout)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestRepackRetriesOnlyItsOwnDeadline(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	run := func(ctx context.Context, query string) error {
		_, err := store.withJournalSessionLock(ctx, session.ID, JournalLockBudget{Wait: 50 * time.Millisecond, Hold: 75 * time.Millisecond}, func(txCtx context.Context, tx *sql.Tx, _ uint64) error {
			_, err := tx.ExecContext(txCtx, query)
			return err
		})
		return err
	}
	if err := run(t.Context(), "SELECT pg_sleep(5)"); !errors.Is(err, ErrJournalBusy) {
		t.Fatalf("local hold deadline: %v", err)
	}
	if err := run(t.Context(), `DO $$ BEGIN RAISE EXCEPTION USING ERRCODE='57014', MESSAGE='external cancellation'; END $$`); err == nil || errors.Is(err, ErrJournalBusy) {
		t.Fatalf("external cancellation was lost or marked retryable: %v", err)
	}
	store.DB().SetMaxOpenConns(1)
	conn, err := store.DB().Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := run(t.Context(), "SELECT 1"); !errors.Is(err, ErrJournalBusy) {
		t.Fatalf("local deadline waiting for connection: %v", err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Millisecond)
	defer cancel()
	if err := run(ctx, "SELECT 1"); !errors.Is(err, context.DeadlineExceeded) || errors.Is(err, ErrJournalBusy) {
		t.Fatalf("caller's deadline: %v", err)
	}
}

func TestRepackedBlockInventoryColumns(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	const through = 32
	appendAPBTestJournalThrough(t, store, session.ID, through)
	frames := eventFrames(t, store, session.ID, 1, through)
	size := 4 * len(frames)
	for _, frame := range frames {
		size += len(frame)
	}
	replaceRowsWithBlock(t, store, session.ID, 1, through, false)
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event_block WHERE session_id=$1 AND block_format='apb_v1' AND uncompressed_bytes=$2`, session.ID, size); got != 1 {
		t.Fatal("APB block missing from format and size inventory")
	}
}

func TestRepackResumesAtFirstLegacyBlock(t *testing.T) {
	store := newTestStore(t)
	store.eventBlockProfile = eventblock.DefaultProfile(64)
	session := openSessionForStore(t, store, "")
	appendAPBTestJournalThrough(t, store, session.ID, 96)
	replaceRowsWithBlock(t, store, session.ID, 1, 32, false)
	replaceRowsWithBlock(t, store, session.ID, 33, 64, true)
	want := eventFrames(t, store, session.ID, 1, 96)
	options := JournalCompactionOptions{FromFirstLegacyBlock: true}
	for _, apply := range []bool{false, true} {
		result, err := store.CompactJournalWithOptions(t.Context(), session.ID, apply, options)
		if err != nil || !result.Completed || result.SkippedThrough != 32 || result.Events != 64 || result.RetainedLegacyBlocks != 0 {
			t.Fatalf("apply=%v result=%+v error=%v", apply, result, err)
		}
	}
	assertFramesEqual(t, eventFrames(t, store, session.ID, 1, 96), want)
	result, err := store.CompactJournalWithOptions(t.Context(), session.ID, true, options)
	if err != nil || !result.Completed || result.SkippedThrough != 96 || result.Batches != 0 {
		t.Fatalf("repeat touched an already migrated journal: %+v %v", result, err)
	}
}

func TestSingleSessionRepackBacksOffAndResumes(t *testing.T) {
	store, _ := newTestStoreWithDSN(t)
	store.eventBlockProfile = eventblock.Profile{TargetBytes: 64 << 10, TargetEntries: 32, Level: 3}
	session := openSessionForStore(t, store, "")
	appendAPBTestJournalThrough(t, store, session.ID, 32)
	want := eventFrames(t, store, session.ID, 1, 32)
	// Sequence increments survive rollback. Force exactly one busy transaction.
	migrationExec(t, store.DB(), `CREATE SEQUENCE busy_attempt`)
	migrationExec(t, store.DB(), `CREATE FUNCTION busy_once() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN IF nextval('busy_attempt')=1 THEN RAISE EXCEPTION USING ERRCODE='55P03', MESSAGE='fixture lock timeout'; END IF; RETURN OLD; END $$`)
	migrationExec(t, store.DB(), `CREATE TRIGGER busy_once BEFORE DELETE ON sampler_event FOR EACH ROW EXECUTE FUNCTION busy_once()`)
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	defer cancel()
	result, err := store.CompactJournalWithOptions(ctx, session.ID, true, JournalCompactionOptions{WaitForIdle: true})
	if err != nil || !result.Completed || result.OpeningCursor != 32 {
		t.Fatalf("retry result=%+v error=%v", result, err)
	}
	if calls := postgres.Count(t, store.DB(), `SELECT last_value FROM busy_attempt`); calls <= 1 {
		t.Fatalf("expected a retry, got %d trigger calls", calls)
	}
	assertFramesEqual(t, eventFrames(t, store, session.ID, 1, 32), want)
}

func TestMixedJournalBatchKeepsMatchingBlock(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	store.eventBlockProfile = eventblock.Profile{TargetBytes: eventblock.MaxBytes, TargetEntries: 32, Level: 3}
	appendAPBTestJournalThrough(t, store, session.ID, 97)
	replaceRowsWithBlock(t, store, session.ID, 1, 32, true)
	replaceRowsWithBlock(t, store, session.ID, 33, 64, false)
	replaceRowsWithBlock(t, store, session.ID, 65, 96, true)
	want := eventFrames(t, store, session.ID, 1, 97)
	var before, after string
	const query = `SELECT xmin::text FROM sampler_event_block WHERE session_id=$1 AND first_sequence=33`
	if err := store.DB().QueryRowContext(t.Context(), query, session.ID).Scan(&before); err != nil {
		t.Fatal(err)
	}
	result, err := store.CompactJournal(t.Context(), session.ID, true)
	if err != nil || !result.Completed || result.RetainedLegacyBlocks != 0 {
		t.Fatalf("mixed repack: %+v, %v", result, err)
	}
	if err := store.DB().QueryRowContext(t.Context(), query, session.ID).Scan(&after); err != nil {
		t.Fatal(err)
	}
	if before != after {
		t.Fatal("rewrote unchanged block between two legacy blocks")
	}
	assertFramesEqual(t, eventFrames(t, store, session.ID, 1, 97), want)
}
