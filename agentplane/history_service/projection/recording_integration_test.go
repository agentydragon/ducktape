package projection

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/history_service/threadfold"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	historypb "github.com/agentydragon/ducktape/agentplane/proto/history_service"
	"github.com/google/uuid"
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

func turnStarted(cursor uint64) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "test-turn"}}})
}

func replyStarted(cursor uint64, sourceSequences ...uint64) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{
		ItemStarted: &eventpb.ItemStarted{ItemId: "test-reply", Kind: eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT},
	}}, sourceSequences...)
}

func replyDelta(cursor uint64, text string, sourceSequences ...uint64) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_TextDelta{
		TextDelta: &eventpb.TextDelta{ItemId: "test-reply", Text: text},
	}}, sourceSequences...)
}

func replyCompleted(cursor uint64, text string) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_ItemCompleted{
		ItemCompleted: &eventpb.ItemCompleted{ItemId: "test-reply", Outcome: &eventpb.ItemCompleted_Text{Text: text}},
	}})
}

func harnessStarted(cursor uint64) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{}}})
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
	textRef          *threadfold.Reference
	state            json.RawMessage
}

func entities(t *testing.T, db *sql.DB, threadID uuid.UUID) map[string]storedEntity {
	t.Helper()
	rows, err := db.Query(`SELECT entity_kind, entity_id, cursor, revision_cursor, entity_index, state, text_ref
		FROM thread_entity WHERE thread_id = $1`, threadID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	found := map[string]storedEntity{}
	for rows.Next() {
		var entity storedEntity
		var state, textRef []byte
		if err := rows.Scan(&entity.kind, &entity.id, &entity.cursor, &entity.revision, &entity.index, &state, &textRef); err != nil {
			t.Fatal(err)
		}
		entity.state = state
		if textRef != nil {
			reference, err := threadfold.ParseReference(textRef)
			if err != nil {
				t.Fatal(err)
			}
			entity.textRef = &reference
		}
		found[entity.kind+"/"+entity.id] = entity
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return found
}

// bodyOf reads the text a reference spans, straight from the chunk table.
func bodyOf(t *testing.T, db *sql.DB, threadID uuid.UUID, reference threadfold.Reference) string {
	t.Helper()
	rows, err := db.Query(`SELECT text FROM thread_payload_chunk WHERE thread_id = $1 AND projection_epoch = $2
		AND owner_cursor = $3 AND owner_id = $4 AND field = $5 AND generation = $6 AND chunk_index < $7
		ORDER BY chunk_index`, threadID, reference.ProjectionEpoch, reference.OwnerCursor, reference.OwnerID,
		reference.Field, reference.Generation, reference.ChunkCount)
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

// completionOf is how the item finished, which stays nil while it streams.
func completionOf(t *testing.T, entity storedEntity) *string {
	t.Helper()
	var state threadfold.StoredItemState
	if err := json.Unmarshal(entity.state, &state); err != nil {
		t.Fatal(err)
	}
	return state.Completion
}

func count(t *testing.T, db *sql.DB, query string, args ...any) int {
	t.Helper()
	var n int
	if err := db.QueryRow(query, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func cursorsOf(t *testing.T, db *sql.DB, query string, threadID uuid.UUID) []uint64 {
	t.Helper()
	rows, err := db.Query(query, threadID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var cursors []uint64
	for rows.Next() {
		var cursor uint64
		if err := rows.Scan(&cursor); err != nil {
			t.Fatal(err)
		}
		cursors = append(cursors, cursor)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return cursors
}

func checkpointOf(t *testing.T, db *sql.DB, threadID uuid.UUID) (sourceID *string, through uint64) {
	t.Helper()
	if err := db.QueryRow("SELECT source_id, through_cursor FROM thread_checkpoint WHERE thread_id = $1", threadID).Scan(&sourceID, &through); err != nil {
		t.Fatal(err)
	}
	return sourceID, through
}

func TestRecordThreadFoldStoresAStreamedReplyAcrossPages(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	mustRecord(t, db, threadID, pageOf(turnStarted(1), replyStarted(2), replyDelta(3, "x"), replyDelta(4, "x"), replyDelta(5, "x")))

	streaming := entities(t, db, threadID)["item/test-reply"]
	if completionOf(t, streaming) != nil || streaming.textRef == nil || streaming.textRef.ChunkCount != 1 {
		t.Fatalf("streaming reply = %+v, want an unfinished item over one chunk", streaming)
	}
	if body := bodyOf(t, db, threadID, *streaming.textRef); body != "xxx" {
		t.Fatalf("streamed body = %q", body)
	}

	mustRecord(t, db, threadID, pageOf(replyDelta(6, "y"), replyDelta(7, "y"), replyCompleted(8, "xxxyy")))
	done := entities(t, db, threadID)["item/test-reply"]
	if completion := completionOf(t, done); completion == nil || *completion != "text" || done.textRef == nil ||
		done.textRef.ChunkCount != 2 || done.index != streaming.index {
		t.Fatalf("completed reply = %+v, want the same item over two chunks, numbered as before", done)
	}
	if done.textRef.Generation != streaming.textRef.Generation {
		t.Fatalf("a completion equal to the streamed text started generation %d, want %d", done.textRef.Generation, streaming.textRef.Generation)
	}
	if body := bodyOf(t, db, threadID, *done.textRef); body != "xxxyy" {
		t.Fatalf("completed body = %q", body)
	}
	// Both revisions stay: a reader holding the first still renders it.
	if body := bodyOf(t, db, threadID, *streaming.textRef); body != "xxx" {
		t.Fatalf("first revision's body = %q after the second page", body)
	}
	var contentBytes uint64
	if err := db.QueryRow(`SELECT content_bytes FROM thread_payload_manifest WHERE thread_id = $1 AND owner_id = 'test-reply'
		AND revision_cursor = $2`, threadID, done.textRef.RevisionCursor).Scan(&contentBytes); err != nil {
		t.Fatal(err)
	}
	if contentBytes != 5 {
		t.Fatalf("content_bytes = %d, want 5", contentBytes)
	}
	if _, through := checkpointOf(t, db, threadID); through != 8 {
		t.Fatalf("checkpoint through %d, want 8", through)
	}
}

func TestEntitiesAreNumberedDenselyAndOnlyOnce(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	mustRecord(t, db, threadID, pageOf(harnessStarted(1), turnStarted(2), replyStarted(3), replyDelta(4, "x")))
	mustRecord(t, db, threadID, pageOf(replyDelta(5, "x"), replyCompleted(6, "xx"), harnessStarted(7)))

	indexes := cursorsOf(t, db, "SELECT entity_index FROM thread_entity WHERE thread_id = $1 ORDER BY entity_index", threadID)
	want := make([]uint64, len(indexes))
	for index := range want {
		want[index] = uint64(index)
	}
	if !slices.Equal(indexes, want) || len(indexes) < 5 {
		t.Fatalf("entity indexes = %v, want 0..n-1 with every row numbered once", indexes)
	}
	// Cursor order is index order for rows created in separate batches.
	cursorsByIndex := cursorsOf(t, db, "SELECT cursor FROM thread_entity WHERE thread_id = $1 AND entity_kind <> 'view_state' ORDER BY entity_index", threadID)
	if !slices.IsSorted(cursorsByIndex) {
		t.Fatalf("cursors in index order = %v", cursorsByIndex)
	}
}

func TestACompletionThatRewritesTheStreamedTextStartsAGeneration(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	mustRecord(t, db, threadID, pageOf(turnStarted(1), replyStarted(2), replyDelta(3, "abc")))
	streaming := entities(t, db, threadID)["item/test-reply"]
	mustRecord(t, db, threadID, pageOf(replyCompleted(4, "ABC")))

	done := entities(t, db, threadID)["item/test-reply"]
	if done.textRef.Generation == streaming.textRef.Generation {
		t.Fatalf("a rewritten body kept generation %d", done.textRef.Generation)
	}
	if body := bodyOf(t, db, threadID, *done.textRef); body != "ABC" {
		t.Fatalf("rewritten body = %q", body)
	}
	if body := bodyOf(t, db, threadID, *streaming.textRef); body != "abc" {
		t.Fatalf("streamed body = %q after it was rewritten; stored bodies never change", body)
	}
}

func TestOneBatchStoresOnlyTheLastRevisionOfABody(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	mustRecord(t, db, threadID, pageOf(turnStarted(1), replyStarted(2), replyDelta(3, "a"), replyDelta(4, "b"), replyDelta(5, "c"), replyCompleted(6, "abc")))

	reply := entities(t, db, threadID)["item/test-reply"]
	if body := bodyOf(t, db, threadID, *reply.textRef); body != "abc" || reply.textRef.ChunkCount != 1 {
		t.Fatalf("body = %q over %d chunks, want \"abc\" over 1", body, reply.textRef.ChunkCount)
	}
	if manifests := count(t, db, "SELECT count(*) FROM thread_payload_manifest WHERE thread_id = $1 AND owner_id = 'test-reply'", threadID); manifests != 1 {
		t.Fatalf("%d manifests for one batch's chain of writes, want 1", manifests)
	}
	if chunks := count(t, db, "SELECT count(*) FROM thread_payload_chunk WHERE thread_id = $1 AND owner_id = 'test-reply'", threadID); chunks != 1 {
		t.Fatalf("%d chunks for one batch's chain of writes, want 1", chunks)
	}
}

func TestBodiesKeepUnusualCharacters(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	text := "café \U0001F986 nul\x00 <b>&amp;</b> \"quoted\" \\  "
	mustRecord(t, db, threadID, pageOf(turnStarted(1), replyStarted(2), replyDelta(3, text)))

	reply := entities(t, db, threadID)["item/test-reply"]
	if body := bodyOf(t, db, threadID, *reply.textRef); body != text {
		t.Fatalf("body = %q, want %q", body, text)
	}
	var contentBytes int
	if err := db.QueryRow("SELECT content_bytes FROM thread_payload_manifest WHERE thread_id = $1", threadID).Scan(&contentBytes); err != nil {
		t.Fatal(err)
	}
	if contentBytes != len(text) {
		t.Fatalf("content_bytes = %d, want the %d UTF-8 bytes", contentBytes, len(text))
	}
}

func TestAnAppendOntoAnUnstoredBodyFails(t *testing.T) {
	db := testDB(t)
	missing := threadfold.PayloadRef{ProjectionEpoch: testEpoch, OwnerCursor: 2, OwnerID: "test-reply", Field: threadfold.FieldText, RevisionCursor: 3, Generation: 2}
	err := withTx(t, db, func(tx *sql.Tx) error {
		return writePayloads(context.Background(), tx, uuid.New(), []threadfold.PayloadWrite{
			threadfold.AppendPayload{Base: &missing, Reference: threadfold.PayloadRef{ProjectionEpoch: testEpoch, OwnerCursor: 2, OwnerID: "test-reply", Field: threadfold.FieldText, RevisionCursor: 4, Generation: 2}, Text: "x"},
		})
	})
	if !errors.Is(err, ErrInconsistentFold) {
		t.Fatalf("append onto a missing manifest = %v, want ErrInconsistentFold", err)
	}
}

func TestASourceOrEpochChangeIsRefusedAtItsCursor(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	mustRecord(t, db, threadID, pageOf(harnessStarted(1)))

	other := harnessStarted(2)
	other.Origin.SourceId = "other-runner"
	err := withTx(t, db, func(tx *sql.Tx) error {
		return RecordThreadFold(context.Background(), tx, threadID, "other-runner", pageOf(other), testEpoch)
	})
	var replication *ReplicationError
	if !errors.As(err, &replication) || replication.Cursor == nil || *replication.Cursor != 2 {
		t.Fatalf("source change = %v, want a ReplicationError at cursor 2", err)
	}

	err = withTx(t, db, func(tx *sql.Tx) error {
		return RecordThreadFold(context.Background(), tx, threadID, testSource, pageOf(harnessStarted(2)), "other-epoch")
	})
	if !errors.As(err, &replication) || replication.Cursor == nil || *replication.Cursor != 2 {
		t.Fatalf("epoch change = %v, want a ReplicationError at cursor 2", err)
	}
	if _, through := checkpointOf(t, db, threadID); through != 1 {
		t.Fatalf("a refused page moved the checkpoint to %d", through)
	}
}

func TestAPageSkippingACursorIsRefused(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	mustRecord(t, db, threadID, pageOf(harnessStarted(1)))
	before := entities(t, db, threadID)

	err := record(t, db, threadID, pageOf(turnStarted(3)))
	var contract *threadfold.ContractError
	if !errors.As(err, &contract) {
		t.Fatalf("a page skipping cursor 2 = %v, want a ContractError", err)
	}
	if after := entities(t, db, threadID); len(after) != len(before) {
		t.Fatalf("rows after a refused page: %d, before: %d", len(after), len(before))
	}
	if _, through := checkpointOf(t, db, threadID); through != 1 {
		t.Fatalf("checkpoint moved to %d", through)
	}
}

func viewStateOf(t *testing.T, db *sql.DB, threadID uuid.UUID) (threadfold.StoredViewState, storedEntity) {
	t.Helper()
	entity := entities(t, db, threadID)["view_state/current"]
	view, err := threadfold.ReadViewState(entity.state)
	if err != nil {
		t.Fatal(err)
	}
	return view, entity
}

func TestAnEmptyFoldIsMaterializedOnceAndAdoptsTheFirstSource(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	created := func() bool {
		var created bool
		if err := withTx(t, db, func(tx *sql.Tx) (err error) {
			created, err = RecordEmptyThreadFold(context.Background(), tx, threadID, testEpoch)
			return err
		}); err != nil {
			t.Fatal(err)
		}
		return created
	}
	if !created() || created() {
		t.Fatal("an empty fold must be created by the first call only")
	}
	source, through := checkpointOf(t, db, threadID)
	view, entity := viewStateOf(t, db, threadID)
	if source != nil || through != 0 || entity.index != 0 || view.Operational.Status != threadfold.StatusActive {
		t.Fatalf("empty fold = source %v through %d index %d %+v", source, through, entity.index, view.Operational)
	}

	mustRecord(t, db, threadID, pageOf(harnessStarted(1)))
	source, through = checkpointOf(t, db, threadID)
	if source == nil || *source != testSource || through != 1 {
		t.Fatalf("after the first entries: source %v through %d", source, through)
	}
	if _, entity := viewStateOf(t, db, threadID); entity.index != 0 || entity.cursor != 1 {
		t.Fatalf("view state = index %d cursor %d, want the empty fold's row revised in place", entity.index, entity.cursor)
	}
}

func TestSetOperationalRecordsAndClearsAFeedFailure(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	set := func(status threadfold.OperationalStatus, message *string, cursor *uint64) bool {
		var changed bool
		if err := withTx(t, db, func(tx *sql.Tx) (err error) {
			changed, err = SetOperational(context.Background(), tx, threadID, status, message, cursor)
			return err
		}); err != nil {
			t.Fatal(err)
		}
		return changed
	}
	if set(threadfold.StatusActive, nil, nil) {
		t.Fatal("a Thread without a fold was changed")
	}

	mustRecord(t, db, threadID, pageOf(harnessStarted(1), harnessStarted(2)))
	message, cursor := "test: projection stalled", uint64(2)
	if !set(threadfold.StatusFailed, &message, &cursor) {
		t.Fatal("a new failure did not change the row")
	}
	view, _ := viewStateOf(t, db, threadID)
	if view.Operational.Status != threadfold.StatusFailed || view.Operational.LastVerifiedCursor != 2 ||
		view.Operational.FeedError == nil || view.Operational.FeedError.Message != message ||
		view.Operational.FeedError.Cursor == nil || *view.Operational.FeedError.Cursor != 2 {
		t.Fatalf("operational = %+v %+v", view.Operational, view.Operational.FeedError)
	}
	if set(threadfold.StatusFailed, &message, &cursor) {
		t.Fatal("the same failure changed the row again")
	}
	if !set(threadfold.StatusActive, nil, nil) {
		t.Fatal("clearing the failure did not change the row")
	}
	if view, _ := viewStateOf(t, db, threadID); view.Operational.FeedError != nil || view.Operational.Status != threadfold.StatusActive {
		t.Fatalf("operational after clearing = %+v", view.Operational)
	}
}

func TestRecordingKeepsTheStoredOperationalState(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	mustRecord(t, db, threadID, pageOf(harnessStarted(1)))
	message := "test: projection stalled"
	if err := withTx(t, db, func(tx *sql.Tx) error {
		_, err := SetOperational(context.Background(), tx, threadID, threadfold.StatusFailed, &message, nil)
		return err
	}); err != nil {
		t.Fatal(err)
	}

	mustRecord(t, db, threadID, pageOf(harnessStarted(2)))
	view, entity := viewStateOf(t, db, threadID)
	if entity.cursor != 2 || view.Operational.Status != threadfold.StatusFailed || view.Operational.LastVerifiedCursor != 1 ||
		view.Operational.FeedError == nil || view.Operational.FeedError.Message != message {
		t.Fatalf("view state at %d with operational %+v after a page", entity.cursor, view.Operational)
	}
}

func TestSkipRemovedSuffixCarriesTheCheckpointAndViewToTheEnd(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	skip := func(through uint64) error {
		return withTx(t, db, func(tx *sql.Tx) error { return SkipRemovedSuffix(context.Background(), tx, threadID, through) })
	}
	var replication *ReplicationError
	if err := skip(6); !errors.As(err, &replication) {
		t.Fatalf("skipping without a fold = %v, want a ReplicationError", err)
	}

	page := pageOf(harnessStarted(1), harnessStarted(3))
	page.Removed = []*historypb.CursorRange{{First: 2, Last: 2}}
	mustRecord(t, db, threadID, page)
	if err := skip(6); err != nil {
		t.Fatal(err)
	}
	if _, through := checkpointOf(t, db, threadID); through != 6 {
		t.Fatalf("checkpoint through %d, want 6", through)
	}
	if _, entity := viewStateOf(t, db, threadID); entity.cursor != 6 || entity.revision != 6 {
		t.Fatalf("view state at %d/%d, want 6/6", entity.cursor, entity.revision)
	}
}

func TestSettlementForgetsTheEvidenceOfDeltasFoldedBeforeIt(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	// 1-6 stream a reply from two native frames; 7 completes it and its page carries the settlement of 3-6.
	mustRecord(t, db, threadID, pageOf(native(1), replyStarted(2, 1), native(3), replyDelta(4, "Hel", 3), native(5), replyDelta(6, "lo", 5)))
	evidence := func() []uint64 {
		return cursorsOf(t, db, "SELECT observation_cursor FROM thread_evidence WHERE thread_id = $1 ORDER BY 1", threadID)
	}
	links := func() []uint64 {
		return cursorsOf(t, db, "SELECT source_sequence FROM thread_native_link WHERE thread_id = $1 ORDER BY 1", threadID)
	}
	if got, want := evidence(), []uint64{2, 4, 6}; !slices.Equal(got, want) {
		t.Fatalf("evidence before settlement = %v, want %v", got, want)
	}
	if got, want := links(), []uint64{1, 3, 5}; !slices.Equal(got, want) {
		t.Fatalf("native links before settlement = %v, want %v", got, want)
	}

	completed := pageOf(replyCompleted(7, "Hello"))
	completed.Settlements = []*historypb.SettledDeltas{{ItemId: "test-reply", Ranges: []*historypb.CursorRange{{First: 3, Last: 6}}}}
	mustRecord(t, db, threadID, completed)

	if got, want := evidence(), []uint64{2, 7}; !slices.Equal(got, want) {
		t.Fatalf("evidence after settlement = %v, want %v", got, want)
	}
	if got, want := links(), []uint64{1}; !slices.Equal(got, want) {
		t.Fatalf("native links after settlement = %v, want %v", got, want)
	}
	if completion := completionOf(t, entities(t, db, threadID)["item/test-reply"]); completion == nil {
		t.Fatal("the settled reply is not completed")
	}
}

func TestSettlementOfAnItemTheFoldNeverSawChangesNothing(t *testing.T) {
	db := testDB(t)
	threadID := uuid.New()
	mustRecord(t, db, threadID, pageOf(native(1), replyStarted(2, 1)))
	page := pageOf(harnessStarted(3))
	page.Settlements = []*historypb.SettledDeltas{{ItemId: "never-folded", Ranges: []*historypb.CursorRange{{First: 1, Last: 2}}}}
	mustRecord(t, db, threadID, page)
	if got, want := cursorsOf(t, db, "SELECT observation_cursor FROM thread_evidence WHERE thread_id = $1 ORDER BY 1", threadID), []uint64{2, 3}; !slices.Equal(got, want) {
		t.Fatalf("evidence = %v, want %v", got, want)
	}
}

func TestRecordingNeedsEntries(t *testing.T) {
	db := testDB(t)
	if err := record(t, db, uuid.New(), &historypb.ReadSessionEventsResponse{}); err == nil {
		t.Fatal("an empty page was recorded")
	}
}
