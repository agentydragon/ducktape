package sessionstore

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"

	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/eventcodec"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"google.golang.org/protobuf/proto"
)

func appendTestJournalThrough(t *testing.T, store *Store, id string, through, oversized uint64) {
	t.Helper()
	shareable := []byte(strings.Repeat("shared event evidence ", 256))
	if err := store.transaction(t.Context(), func(ctx context.Context, tx *sql.Tx) error {
		_, session, err := lockSession(ctx, tx, id)
		if err != nil {
			return err
		}
		for session.Cursor < through {
			sequence := session.Cursor + 1
			payload := shareable
			if sequence == oversized {
				payload = bytes.Repeat([]byte("a"), eventcodec.MaxBlockBytes)
			} else if sequence%2 == 0 {
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

func TestCompactJournalDryRunApplyAndReplay(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	const window = eventcodec.MaxBlockEntries
	const oversized, gap, tail = 13, 20, 3*window + 3
	command := &commandpb.Command{CommandId: "historical-admission", Operation: &commandpb.Command_SubmitInput{
		SubmitInput: &commandpb.SubmitInput{Text: "pack the historical journal"},
	}}
	admission, err := store.Admit(t.Context(), session.ID, command, session.OwnerKey, nil)
	if err != nil {
		t.Fatal(err)
	}
	appendTestJournalThrough(t, store, session.ID, window, oversized)
	// An imported journal with a missing cursor can pack each side independently.
	migrationExec(t, store.DB(), `DELETE FROM sampler_event WHERE session_id=$1 AND sequence=$2`, session.ID, gap)
	appendTestJournalThrough(t, store, session.ID, 2*window, 0)
	if result, err := store.compactJournalWindow(t.Context(), session.ID, window+1, 2*window, true); err != nil || result.Blocks != 1 {
		t.Fatalf("pack existing block: result=%+v err=%v", result, err)
	}
	appendTestJournalThrough(t, store, session.ID, tail, 0)
	var previousBlock []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT payload FROM sampler_event_block
		WHERE session_id=$1 AND first_sequence=$2 AND last_sequence=$3`, session.ID, window+1, 2*window).Scan(&previousBlock); err != nil {
		t.Fatalf("read existing live block: %v", err)
	}
	original, err := journalFrames(t.Context(), store.DB(), session.ID, 1, tail)
	if err != nil {
		t.Fatal(err)
	}
	proposal, err := store.CompactJournal(t.Context(), session.ID, false)
	if err != nil || proposal.Events == 0 || proposal.Blocks == 0 || proposal.StoredBytes <= proposal.PackedBytes {
		t.Fatalf("dry run result=%+v err=%v", proposal, err)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event_block WHERE session_id=$1`, session.ID); got != 1 {
		t.Fatalf("dry run changed the block count to %d", got)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event WHERE session_id=$1`, session.ID); got != len(original)-window {
		t.Fatalf("dry run changed the standalone row count to %d", got)
	}
	result, err := store.CompactJournal(t.Context(), session.ID, true)
	if err != nil || result != proposal {
		t.Fatalf("apply result=%+v proposal=%+v err=%v", result, proposal, err)
	}
	var existing []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT payload FROM sampler_event_block
		WHERE session_id=$1 AND first_sequence=$2 AND last_sequence=$3`, session.ID, window+1, 2*window).Scan(&existing); err != nil || !bytes.Equal(existing, previousBlock) {
		t.Fatalf("apply changed existing block: %v", err)
	}
	rows, err := store.DB().QueryContext(t.Context(), `SELECT sequence FROM sampler_event WHERE session_id=$1
		AND (sequence=$2 OR sequence>$3) ORDER BY sequence`, session.ID, oversized, 3*window)
	if err != nil {
		t.Fatal(err)
	}
	var standalone []uint64
	for rows.Next() {
		var cursor uint64
		if err := rows.Scan(&cursor); err != nil {
			rows.Close()
			t.Fatal(err)
		}
		standalone = append(standalone, cursor)
	}
	rows.Close()
	if err := rows.Err(); err != nil || !slices.Equal(standalone, []uint64{oversized, tail - 2, tail - 1, tail}) {
		t.Fatalf("standalone cursor preservation=%v err=%v", standalone, err)
	}
	restored, err := journalFrames(t.Context(), store.DB(), session.ID, 1, tail)
	if err != nil || len(restored) != len(original) {
		t.Fatalf("read %d restored frames, wanted %d: %v", len(restored), len(original), err)
	}
	for index, frame := range restored {
		before, err := eventcodec.Uncompress(original[index].wire)
		if err != nil {
			t.Fatal(err)
		}
		after, err := eventcodec.Uncompress(frame.wire)
		if err != nil || frame.sequence != original[index].sequence || !bytes.Equal(before, after) {
			t.Fatalf("history changed at cursor %d: %v", original[index].sequence, err)
		}
	}
	replayed, err := store.Admit(t.Context(), session.ID, command, session.OwnerKey, nil)
	if err != nil || !proto.Equal(replayed, admission) {
		t.Fatalf("command admission replay changed after packing: %v, %v", replayed, err)
	}
	if current, err := store.CurrentCursor(t.Context(), session.ID); err != nil || current != tail {
		t.Fatalf("apply changed the session cursor to %d: %v", current, err)
	}
	if repeated, err := store.CompactJournal(t.Context(), session.ID, true); err != nil || repeated != (JournalCompactionResult{}) {
		t.Fatalf("repeat apply result=%+v err=%v", repeated, err)
	}
}

func TestCompactJournalResumesAfterRejectedWindow(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	const window = eventcodec.MaxBlockEntries
	appendTestJournalThrough(t, store, session.ID, 2*window, 0)
	original, err := journalFrames(t.Context(), store.DB(), session.ID, window+1, 2*window)
	if err != nil || len(original) != window {
		t.Fatalf("read %d original frames: %v", len(original), err)
	}
	var frames [][]byte
	for _, frame := range original {
		wire, err := eventcodec.Uncompress(frame.wire)
		if err != nil {
			t.Fatal(err)
		}
		frames = append(frames, bytes.Clone(wire))
	}
	frames[0][len(frames[0])-1] ^= 1
	tampered, err := eventcodec.PackBlock(frames)
	if err != nil {
		t.Fatal(err)
	}
	migrationExec(t, store.DB(), fmt.Sprintf(`CREATE FUNCTION replace_block_payload() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN
			IF NEW.first_sequence=%d THEN NEW.payload := decode('%x', 'hex'); END IF;
			RETURN NEW;
		END; $$`, window+1, tampered))
	migrationExec(t, store.DB(), `CREATE TRIGGER replace_block_payload BEFORE INSERT ON sampler_event_block
		FOR EACH ROW EXECUTE FUNCTION replace_block_payload()`)
	if result, err := store.CompactJournal(t.Context(), session.ID, true); err == nil || result.Events != window || result.Blocks != 1 {
		t.Fatalf("changed packed frame: result=%+v err=%v", result, err)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event_block WHERE session_id=$1`, session.ID); got != 1 {
		t.Fatalf("failed verification committed %d blocks", got)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event WHERE session_id=$1 AND sequence>$2`,
		session.ID, window); got != window {
		t.Fatalf("failed verification left %d original rows in the rejected window, wanted %d", got, window)
	}
	migrationExec(t, store.DB(), `DROP TRIGGER replace_block_payload ON sampler_event_block`)
	if result, err := store.CompactJournal(t.Context(), session.ID, true); err != nil || result.Events != window || result.Blocks != 1 {
		t.Fatalf("resume result=%+v err=%v", result, err)
	}
	if got := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event WHERE session_id=$1`, session.ID); got != 0 {
		t.Fatalf("resumed packing left %d standalone rows", got)
	}
}

func TestListJournalSessionsUsesExclusiveKeyset(t *testing.T) {
	store := newTestStore(t)
	want := []string{openSessionForStore(t, store, "").ID, openSessionForStore(t, store, "").ID}
	slices.Sort(want)
	var actual []string
	for after := ""; ; {
		page, err := store.ListJournalSessions(t.Context(), after, 1)
		if err != nil {
			t.Fatal(err)
		}
		if len(page) == 0 {
			break
		}
		if len(actual) == len(want) || len(page) != 1 {
			t.Fatalf("unexpected page %v after %q", page, after)
		}
		actual = append(actual, page[0])
		after = page[0]
	}
	if !slices.Equal(actual, want) {
		t.Fatalf("listed %v, want %v", actual, want)
	}
}

func TestJournalCompactionRetriesChangedStandaloneRows(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	const window = eventcodec.MaxBlockEntries
	appendTestJournalThrough(t, store, session.ID, window, 0)
	blocks, err := prepareJournalBlocks(t.Context(), store.DB(), session.ID, 1, window)
	if err != nil || len(blocks) != 1 {
		t.Fatalf("prepare blocks=%d err=%v", len(blocks), err)
	}
	// Replace one row without moving its cursor. Even a partial delete must
	// roll back when the prepared block no longer matches its sources.
	migrationExec(t, store.DB(), `UPDATE sampler_event SET entry=entry WHERE session_id=$1 AND sequence=$2`, session.ID, window)
	if err := store.commitJournalBlocks(t.Context(), session.ID, blocks); !errors.Is(err, errJournalWindowChanged) {
		t.Fatalf("apply prepared block after row replacement: %v", err)
	}
	if count := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event WHERE session_id=$1`, session.ID); count != window {
		t.Fatalf("stale compactor left %d of %d source rows", count, window)
	}
	blocks, err = prepareJournalBlocks(t.Context(), store.DB(), session.ID, 1, window)
	if err != nil || len(blocks) != 1 {
		t.Fatalf("refresh blocks=%d err=%v", len(blocks), err)
	}
	if err := store.commitJournalBlocks(t.Context(), session.ID, blocks); err != nil {
		t.Fatal(err)
	}
	if err := store.commitJournalBlocks(t.Context(), session.ID, blocks); !errors.Is(err, errJournalWindowChanged) {
		t.Fatalf("apply same window after a competing compactor committed: %v", err)
	}
	if count := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event_block WHERE session_id=$1`, session.ID); count != 1 {
		t.Fatalf("competing compactions left %d blocks", count)
	}
}

func TestJournalCompactionWorkerResumesFromCompletedCursor(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	const window = eventcodec.MaxBlockEntries
	appendTestJournalThrough(t, store, session.ID, 2*window+1, 0)
	// A retained window with no packable pair is still a completed attempt.
	migrationExec(t, store.DB(), `DELETE FROM sampler_event WHERE session_id=$1 AND sequence BETWEEN 2 AND $2`, session.ID, window)
	for _, want := range []uint64{0, window} {
		pending, err := store.pendingJournalCompactions(t.Context(), "")
		if err != nil || len(pending) != 1 || pending[0].id != session.ID || pending[0].after != want {
			t.Fatalf("pending=%+v, expected one Session after %d: %v", pending, want, err)
		}
		if err := store.compactPendingJournal(t.Context(), pending[0]); err != nil {
			t.Fatal(err)
		}
		store = New(store.DB(), store.operationTimeout, nil)
	}
	if pending, err := store.pendingJournalCompactions(t.Context(), ""); err != nil || len(pending) != 0 {
		t.Fatalf("incomplete tail pending=%+v err=%v", pending, err)
	}
	if count := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event WHERE session_id=$1`, session.ID); count != 2 {
		t.Fatalf("unpackable entry and incomplete tail left %d standalone rows", count)
	}
	if count := postgres.Count(t, store.DB(), `SELECT count(*) FROM sampler_event_block WHERE session_id=$1`, session.ID); count != 1 {
		t.Fatalf("worker packed %d blocks", count)
	}
}
