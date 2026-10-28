package storage

import (
	"database/sql"
	"errors"
	"reflect"
	"slices"
	"sync"
	"testing"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

var testWriter = Writer{Namespace: "testing", Name: "worker", UID: "test-worker-account-uid"}

func testDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func testStore(t *testing.T) *Store { return NewStore(testDB(t), false) }

func entryFrom(source string, cursor uint64, resumed bool) *eventlogpb.EventEntry {
	return &eventlogpb.EventEntry{
		Cursor: cursor,
		Origin: &eventlogpb.EventOrigin{SourceId: source, Sequence: cursor},
		Event:  &eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{Resumed: resumed}}},
	}
}

func entry(cursor uint64) *eventlogpb.EventEntry { return entryFrom("source", cursor, false) }

func nativeEntry(cursor uint64, line string) *eventlogpb.EventEntry {
	native := entry(cursor)
	native.Event.Observation = &eventpb.Event_Native{Native: &eventpb.Native{Line: line}}
	return native
}

func entries(first, last uint64) []*eventlogpb.EventEntry {
	var result []*eventlogpb.EventEntry
	for cursor := first; cursor <= last; cursor++ {
		result = append(result, entry(cursor))
	}
	return result
}

func opened(t *testing.T, store *Store) uuid.UUID {
	t.Helper()
	sessionID := uuid.New()
	if _, err := store.BindWriter(t.Context(), sessionID, testWriter, ""); err != nil {
		t.Fatal(err)
	}
	return sessionID
}

func appendEntries(t *testing.T, store *Store, sessionID uuid.UUID, batch ...*eventlogpb.EventEntry) uint64 {
	t.Helper()
	last, err := store.Append(t.Context(), sessionID, batch)
	if err != nil {
		t.Fatal(err)
	}
	return last
}

func readPage(t *testing.T, store *Store, sessionID uuid.UUID, after uint64, limit int) *historypb.ReadSessionEventsResponse {
	t.Helper()
	page, err := store.ReadPage(t.Context(), sessionID, after, limit)
	if err != nil {
		t.Fatal(err)
	}
	return page
}

func observationPage(t *testing.T, store *Store, sessionID uuid.UUID, before, after *uint64, limit int) ObservationPage {
	t.Helper()
	page, err := store.ReadObservations(t.Context(), sessionID, before, after, limit)
	if err != nil {
		t.Fatal(err)
	}
	return page
}

func pointer(cursor uint64) *uint64 { return &cursor }

func assertEntries(t *testing.T, got []*eventlogpb.EventEntry, want ...*eventlogpb.EventEntry) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("got %d entries %v, want %d", len(got), got, len(want))
	}
	for i := range want {
		if !proto.Equal(got[i], want[i]) {
			t.Fatalf("entry %d: got %v, want %v", i, got[i], want[i])
		}
	}
}

func assertError(t *testing.T, err, want error) {
	t.Helper()
	if !errors.Is(err, want) {
		t.Fatalf("got error %v, want %v", err, want)
	}
}

func assertFeedState(t *testing.T, store *Store, sessionID uuid.UUID, want *historypb.SessionFeedState) {
	t.Helper()
	got := readPage(t, store, sessionID, 0, 1).FeedState
	if !proto.Equal(got, want) {
		t.Fatalf("feed state: got %v, want %v", got, want)
	}
}

func cursors(page *historypb.ReadSessionEventsResponse) []uint64 {
	var result []uint64
	for _, entry := range page.Entries {
		result = append(result, entry.Cursor)
	}
	return result
}

func removedRanges(page *historypb.ReadSessionEventsResponse) []CursorRange {
	var result []CursorRange
	for _, removed := range page.Removed {
		result = append(result, CursorRange{removed.First, removed.Last})
	}
	return result
}

func stoppedAt(sessionID uuid.UUID, cursor uint64) *historypb.SessionFeedState {
	return &historypb.SessionFeedState{Attached: &runnerpb.Attached{
		SessionId: sessionID.String(), LastCursor: cursor, HarnessState: runnerpb.HarnessState_HARNESS_STATE_STOPPED,
	}}
}

// removeEntries leaves the Session's stored history as a cleanup migration does: the entries deleted,
// their cursors removed.
func removeEntries(t *testing.T, db *sql.DB, sessionID uuid.UUID, removed ...uint64) {
	t.Helper()
	slices.Sort(removed)
	for i, cursor := range removed {
		if _, err := db.Exec("DELETE FROM session_event WHERE session_id = $1 AND cursor = $2", sessionID, cursor); err != nil {
			t.Fatal(err)
		}
		if i > 0 && removed[i-1]+1 == cursor {
			continue
		}
		last := cursor
		for _, next := range removed[i+1:] {
			if next != last+1 {
				break
			}
			last = next
		}
		if _, err := db.Exec("INSERT INTO session_event_removed_range (session_id, first_cursor, last_cursor) VALUES ($1, $2, $3)",
			sessionID, cursor, last); err != nil {
			t.Fatal(err)
		}
	}
}

func TestAppendReplaysDuplicatesAndReadsPages(t *testing.T) {
	db := testDB(t)
	store := NewStore(db, false)
	sessionID := opened(t, store)
	if _, err := store.BindWriter(t.Context(), sessionID, testWriter, ""); err != nil {
		t.Fatal(err)
	}
	resumed := entryFrom("source", 2, true)
	native := nativeEntry(3, "frame with a NUL\x00 and provider bytes")
	if last := appendEntries(t, store, sessionID, entry(1), resumed); last != 2 {
		t.Fatalf("last cursor %d", last)
	}
	// A second replica replays the prefix and extends it.
	if last := appendEntries(t, NewStore(db, false), sessionID, entry(1), resumed, native); last != 3 {
		t.Fatalf("last cursor %d", last)
	}
	page := readPage(t, NewStore(db, false), sessionID, 1, 2)
	if page.LastCursor != 3 {
		t.Fatalf("last cursor %d", page.LastCursor)
	}
	assertEntries(t, page.Entries, resumed, native)
	page = readPage(t, NewStore(db, false), sessionID, 3, 128)
	if page.LastCursor != 3 {
		t.Fatalf("last cursor %d", page.LastCursor)
	}
	assertEntries(t, page.Entries)
	_, err := store.ReadPage(t.Context(), sessionID, 4, 128)
	assertError(t, err, ErrInvalidArgument)
}

func TestBadBatchRollsBackAndConflictingDuplicateIsRejected(t *testing.T) {
	store := testStore(t)
	sessionID := opened(t, store)
	_, err := store.Append(t.Context(), sessionID, []*eventlogpb.EventEntry{entry(1), entry(3)})
	assertError(t, err, ErrConflict)
	assertEntries(t, readPage(t, store, sessionID, 0, 128).Entries)
	if last := appendEntries(t, store, sessionID, entry(1), entry(2)); last != 2 {
		t.Fatalf("last cursor %d", last)
	}
	for _, bad := range [][]*eventlogpb.EventEntry{{entryFrom("source", 2, true)}, {entryFrom("other", 3, false)}, {entry(4)}} {
		_, err := store.Append(t.Context(), sessionID, bad)
		assertError(t, err, ErrConflict)
	}
	assertEntries(t, readPage(t, store, sessionID, 0, 128).Entries, entry(1), entry(2))
	_, err = store.Append(t.Context(), uuid.New(), []*eventlogpb.EventEntry{entry(1)})
	assertError(t, err, ErrNotFound)
}

func TestAppendStoresObservationFieldNumbers(t *testing.T) {
	db := testDB(t)
	store := NewStore(db, false)
	sessionID := opened(t, store)
	batch := []*eventlogpb.EventEntry{entry(1), nativeEntry(2, "test-frame"), entry(3)}
	batch[2].Event = nil
	appendEntries(t, store, sessionID, batch...)
	// The numbers are the runner Event's wire fields, independent of observation names.
	for cursor, want := range []int{4, 20, 0} {
		var kind int
		if err := db.QueryRowContext(t.Context(), "SELECT kind FROM session_event WHERE session_id = $1 AND cursor = $2", sessionID, cursor+1).Scan(&kind); err != nil {
			t.Fatal(err)
		}
		if kind != want {
			t.Errorf("cursor %d kind = %d, want %d", cursor+1, kind, want)
		}
	}
}

func TestTwoReplicaWritersSerializeOnTheHistoryRow(t *testing.T) {
	db := testDB(t)
	left, right := NewStore(db, false), NewStore(db, false)
	sessionID := opened(t, left)
	var wg sync.WaitGroup
	results := make([]uint64, 2)
	errs := make([]error, 2)
	for i, store := range []*Store{left, right} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			results[i], errs[i] = store.Append(t.Context(), sessionID, []*eventlogpb.EventEntry{entry(1), entry(2)})
		}()
	}
	wg.Wait()
	if errs[0] != nil || errs[1] != nil || results[0] != 2 || results[1] != 2 {
		t.Fatalf("results %v, errors %v", results, errs)
	}
	assertEntries(t, readPage(t, right, sessionID, 0, 128).Entries, entry(1), entry(2))
}

func TestASessionBindsToItsFirstWriter(t *testing.T) {
	store := testStore(t)
	sessionID := uuid.New()
	for range 2 {
		if _, err := store.BindWriter(t.Context(), sessionID, testWriter, ""); err != nil {
			t.Fatal(err)
		}
	}
	recreated := testWriter
	recreated.UID = "test-recreated-uid"
	_, err := store.BindWriter(t.Context(), sessionID, recreated, "")
	assertError(t, err, ErrWriterMismatch)
}

func TestASessionBindsToItsFirstSandboxUID(t *testing.T) {
	db := testDB(t)
	store := NewStore(db, false)
	sessionID := uuid.New()
	if _, err := store.BindWriter(t.Context(), sessionID, testWriter, "sandbox-uid-first"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.BindWriter(t.Context(), sessionID, testWriter, "sandbox-uid-first"); err != nil {
		t.Fatalf("same Sandbox reconnect: %v", err)
	}
	if _, err := store.BindWriter(t.Context(), sessionID, testWriter, "sandbox-uid-replacement"); !errors.Is(err, ErrSandboxMismatch) {
		t.Fatalf("same-name replacement: %v, want ErrSandboxMismatch", err)
	}
	var got string
	if err := db.QueryRowContext(t.Context(), `SELECT sandbox_uid FROM session_history WHERE id = $1`, sessionID).Scan(&got); err != nil {
		t.Fatal(err)
	}
	if got != "sandbox-uid-first" {
		t.Fatalf("recorded Sandbox UID = %q, want the original UID", got)
	}
}

func TestAReconnectedWriterFillsAnUnknownSandboxUIDOnce(t *testing.T) {
	store := testStore(t)
	sessionID := uuid.New()
	if _, err := store.BindWriter(t.Context(), sessionID, testWriter, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := store.BindWriter(t.Context(), sessionID, testWriter, "sandbox-uid-first"); err != nil {
		t.Fatalf("fill unknown UID: %v", err)
	}
	if _, err := store.BindWriter(t.Context(), sessionID, testWriter, "sandbox-uid-replacement"); !errors.Is(err, ErrSandboxMismatch) {
		t.Fatalf("replacement after fill: %v, want ErrSandboxMismatch", err)
	}
}

func TestOpenedSessionsListInRecordingOrder(t *testing.T) {
	store := testStore(t)
	first, second := opened(t, store), opened(t, store)
	all, err := store.OpenedSessions(t.Context(), 0, 1000)
	if err != nil {
		t.Fatal(err)
	}
	index := func(sessionID uuid.UUID) int {
		return slices.IndexFunc(all, func(session OpenedSession) bool { return session.SessionID == sessionID })
	}
	i, j := index(first), index(second)
	if i < 0 || j < 0 || all[i].Position >= all[j].Position {
		t.Fatalf("sessions %v do not list %s before %s", all, first, second)
	}
	rest, err := store.OpenedSessions(t.Context(), all[i].Position, 1000)
	if err != nil {
		t.Fatal(err)
	}
	if slices.ContainsFunc(rest, func(session OpenedSession) bool { return session.SessionID == first }) || !slices.ContainsFunc(rest, func(session OpenedSession) bool { return session.SessionID == second }) {
		t.Fatalf("sessions after the first: %v", rest)
	}
	_, err = store.OpenedSessions(t.Context(), 0, 0)
	assertError(t, err, ErrInvalidArgument)
}

// notifications counts what each channel was notified while `operate` ran. A barrier notified after
// `operate` returns marks where its commits end in the notification queue, so counting needs no timing.
func notifications(t *testing.T, db *sql.DB, operate func()) map[string]int {
	t.Helper()
	const barrier = "agentplane_test_barrier"
	listener := pq.NewListener(integrationDatabase.URL, time.Second, time.Minute, nil)
	defer listener.Close()
	for _, channel := range []string{ChannelHistory, ChannelSessions, barrier} {
		if err := listener.Listen(channel); err != nil {
			t.Fatal(err)
		}
	}
	operate()
	if _, err := db.Exec("SELECT pg_notify($1, '')", barrier); err != nil {
		t.Fatal(err)
	}
	counts := map[string]int{}
	timeout := time.After(30 * time.Second)
	for {
		select {
		case notification := <-listener.Notify:
			switch {
			case notification == nil:
				t.Fatal("listener reconnected")
			case notification.Channel == barrier:
				return counts
			default:
				counts[notification.Channel]++
			}
		case <-timeout:
			t.Fatal("barrier notification never arrived")
		}
	}
}

func TestHistoryNotifiesAfterCommitAndNotAfterRollbackOrNoOp(t *testing.T) {
	db := testDB(t)
	store := NewStore(db, false)
	sessionID := uuid.New()
	for _, step := range []struct {
		name    string
		operate func()
		want    map[string]int
	}{
		{"binding a new Session", func() {
			if _, err := store.BindWriter(t.Context(), sessionID, testWriter, ""); err != nil {
				t.Fatal(err)
			}
		}, map[string]int{ChannelHistory: 1, ChannelSessions: 1}},
		{"binding it again", func() {
			if _, err := store.BindWriter(t.Context(), sessionID, testWriter, ""); err != nil {
				t.Fatal(err)
			}
		}, map[string]int{}},
		{"a rejected batch", func() {
			_, err := store.Append(t.Context(), sessionID, []*eventlogpb.EventEntry{entry(1), entry(3)})
			assertError(t, err, ErrConflict)
		}, map[string]int{}},
		{"appending", func() { appendEntries(t, store, sessionID, entry(1)) }, map[string]int{ChannelHistory: 1}},
		{"replaying", func() { appendEntries(t, store, sessionID, entry(1)) }, map[string]int{}},
		{"recording a snapshot", func() {
			if err := store.RecordFeedState(t.Context(), sessionID, stoppedAt(sessionID, 1)); err != nil {
				t.Fatal(err)
			}
		}, map[string]int{ChannelHistory: 1}},
		{"recording it again", func() {
			if err := store.RecordFeedState(t.Context(), sessionID, stoppedAt(sessionID, 1)); err != nil {
				t.Fatal(err)
			}
		}, map[string]int{}},
	} {
		if got := notifications(t, db, step.operate); !reflect.DeepEqual(got, step.want) {
			t.Errorf("%s notified %v, want %v", step.name, got, step.want)
		}
	}
}

func TestObservationPagesSeekTheRetainedPrefix(t *testing.T) {
	store := testStore(t)
	sessionID := opened(t, store)
	if page := observationPage(t, store, sessionID, nil, nil, 30); !reflect.DeepEqual(page, ObservationPage{}) {
		t.Fatalf("empty history: %v", page)
	}
	appendEntries(t, store, sessionID, entry(1), entry(2), nativeEntry(3, "retained\x00native"), entry(4), entry(5))
	started, native := "harness_started", "native"
	for _, test := range []struct {
		name          string
		before, after *uint64
		limit         int
		want          []Observation
	}{
		{"tail", nil, nil, 2, []Observation{{4, started}, {5, started}}},
		{"before", pointer(4), nil, 2, []Observation{{2, started}, {3, native}}},
		{"after", nil, pointer(2), 2, []Observation{{3, native}, {4, started}}},
		{"before the first cursor", pointer(0), nil, 30, nil},
		{"after the last cursor", nil, pointer(9), 30, nil},
	} {
		want := ObservationPage{LastCursor: 5, Observations: test.want}
		if got := observationPage(t, store, sessionID, test.before, test.after, test.limit); !reflect.DeepEqual(got, want) {
			t.Errorf("%s: got %v, want %v", test.name, got, want)
		}
	}
	_, err := store.ReadObservations(t.Context(), sessionID, pointer(3), pointer(1), 30)
	assertError(t, err, ErrInvalidArgument)
	_, err = store.ReadObservations(t.Context(), sessionID, nil, nil, 201)
	assertError(t, err, ErrInvalidArgument)
	_, err = store.ReadObservations(t.Context(), uuid.New(), nil, nil, 30)
	assertError(t, err, ErrNotFound)
}

func TestFeedSnapshotNeedsCoveredBoundHistoryAndCannotRegress(t *testing.T) {
	db := testDB(t)
	store := NewStore(db, false)
	sessionID := opened(t, store)
	if readPage(t, store, sessionID, 0, 128).FeedState != nil {
		t.Fatal("a fresh Session has a feed state")
	}
	feed := stoppedAt(sessionID, 1)
	assertError(t, store.RecordFeedState(t.Context(), sessionID, feed), ErrConflict)
	appendEntries(t, store, sessionID, entry(1))
	record := func(feed *historypb.SessionFeedState) {
		t.Helper()
		if err := store.RecordFeedState(t.Context(), sessionID, feed); err != nil {
			t.Fatal(err)
		}
	}
	record(feed)
	conflicting := proto.Clone(feed).(*historypb.SessionFeedState)
	conflicting.Attached.Spec = &runnerpb.SessionSpec{Cwd: "/another-workspace"}
	assertError(t, store.RecordFeedState(t.Context(), sessionID, conflicting), ErrConflict)

	feed.Attached.HarnessState = runnerpb.HarnessState_HARNESS_STATE_RUNNING
	record(feed)
	assertFeedState(t, NewStore(db, false), sessionID, feed)

	// A stopped snapshot at the cursor of a running one belongs to the previous run.
	record(stoppedAt(sessionID, 1))
	assertFeedState(t, NewStore(db, false), sessionID, feed)

	appendEntries(t, store, sessionID, entryFrom("source", 2, true))
	feed.Attached.LastCursor = 2
	record(feed)
	record(&historypb.SessionFeedState{Attached: &runnerpb.Attached{SessionId: sessionID.String(), LastCursor: 1}})
	page := readPage(t, store, sessionID, 2, 128)
	if page.LastCursor != 2 || len(page.Entries) != 0 || !proto.Equal(page.FeedState, feed) {
		t.Fatalf("page %v does not carry the newer snapshot", page)
	}

	feed.Attached.SessionId = "replacement-runner"
	assertError(t, store.RecordFeedState(t.Context(), sessionID, feed), ErrConflict)
}

func TestSealEndsCommittedHistoryAndIsSticky(t *testing.T) {
	store := testStore(t)
	sessionID := opened(t, store)
	appendEntries(t, store, sessionID, entry(1), entry(2))
	attachedAt := func(cursor uint64) *runnerpb.Attached {
		return &runnerpb.Attached{SessionId: sessionID.String(), LastCursor: cursor}
	}
	sealedAt := func(cursor uint64) *historypb.SessionFeedState {
		return &historypb.SessionFeedState{Attached: attachedAt(cursor), Sealed: &runnerpb.Sealed{Cursor: cursor}}
	}
	// A seal that stops short of the committed history is refused.
	assertError(t, store.RecordFeedState(t.Context(), sessionID, &historypb.SessionFeedState{
		Attached: attachedAt(1), Sealed: &runnerpb.Sealed{Cursor: 1},
	}), ErrConflict)
	if err := store.RecordFeedState(t.Context(), sessionID, sealedAt(2)); err != nil {
		t.Fatal(err)
	}
	direct := &historypb.SessionFeedState{Attached: attachedAt(2)}
	if err := store.RecordFeedState(t.Context(), sessionID, direct); err != nil {
		t.Fatal(err)
	}
	stored := readPage(t, store, sessionID, 0, 1).FeedState
	if !proto.Equal(stored.Attached, direct.Attached) || stored.Sealed.GetCursor() != 2 {
		t.Fatalf("a snapshot without a seal dropped it: %v", stored)
	}
	assertError(t, store.RecordFeedState(t.Context(), sessionID, &historypb.SessionFeedState{
		Attached: attachedAt(2), Sealed: &runnerpb.Sealed{Cursor: 3},
	}), ErrConflict)
}

func TestReopeningASealNeedsASnapshotThatReachesIt(t *testing.T) {
	store := testStore(t)
	sessionID := opened(t, store)
	appendEntries(t, store, sessionID, entry(1), entry(2))
	incarnation := "test-incarnation-1"
	sealed := &historypb.SessionFeedState{
		Attached: &runnerpb.Attached{SessionId: sessionID.String(), LastCursor: 2},
		Sealed:   &runnerpb.Sealed{Cursor: 2, IncarnationUid: &incarnation},
	}
	if err := store.RecordFeedState(t.Context(), sessionID, sealed); err != nil {
		t.Fatal(err)
	}
	_, err := store.ReopenSealed(t.Context(), sessionID, "test-incarnation-2", 1)
	assertError(t, err, ErrConflict)
	// The sealing incarnation itself cannot continue past its seal.
	checkpoint, err := store.ReopenSealed(t.Context(), sessionID, incarnation, 2)
	if err != nil || checkpoint.Seal == nil {
		t.Fatalf("checkpoint %v, error %v", checkpoint, err)
	}
	checkpoint, err = store.ReopenSealed(t.Context(), sessionID, "test-incarnation-2", 2)
	if err != nil || checkpoint.Seal != nil || checkpoint.Cursor != 2 {
		t.Fatalf("checkpoint %v, error %v", checkpoint, err)
	}
	if readPage(t, store, sessionID, 0, 1).FeedState.GetSealed() != nil {
		t.Fatal("the stored feed state keeps the seal")
	}
}

func TestRemovedCursorsAreReportedWhereAPageReachesThem(t *testing.T) {
	db := testDB(t)
	store := NewStore(db, false)
	sessionID := opened(t, store)
	appendEntries(t, store, sessionID, entries(1, 5)...)
	removeEntries(t, db, sessionID, 3, 5)
	for _, test := range []struct {
		after       uint64
		limit       int
		wantCursors []uint64
		wantRemoved []CursorRange
	}{
		{0, 10, []uint64{1, 2, 4}, []CursorRange{{3, 3}, {5, 5}}},
		{0, 2, []uint64{1, 2}, nil},
		{2, 1, []uint64{4}, []CursorRange{{3, 3}}},
		{4, 1, nil, []CursorRange{{5, 5}}},
		{5, 1, nil, nil},
	} {
		page := readPage(t, store, sessionID, test.after, test.limit)
		if !reflect.DeepEqual(cursors(page), test.wantCursors) || !reflect.DeepEqual(removedRanges(page), test.wantRemoved) {
			t.Errorf("after %d limit %d: cursors %v removed %v, want %v and %v",
				test.after, test.limit, cursors(page), removedRanges(page), test.wantCursors, test.wantRemoved)
		}
	}
	// A runner replaying its journal sends the removed entries again; they are no longer stored to compare.
	if last := appendEntries(t, store, sessionID, entries(1, 5)...); last != 5 {
		t.Fatalf("last cursor %d", last)
	}
	if got := cursors(readPage(t, store, sessionID, 0, 128)); !reflect.DeepEqual(got, []uint64{1, 2, 4}) {
		t.Fatalf("replay stored cursors %v", got)
	}
	started := "harness_started"
	for _, test := range []struct {
		name  string
		after *uint64
		limit int
		want  ObservationPage
	}{
		{"all", pointer(0), 30, ObservationPage{5, []Observation{{1, started}, {2, started}, {4, started}}, []CursorRange{{3, 3}, {5, 5}}}},
		{"first three", pointer(0), 3, ObservationPage{5, []Observation{{1, started}, {2, started}, {4, started}}, []CursorRange{{3, 3}}}},
		{"tail", nil, 2, ObservationPage{5, []Observation{{2, started}, {4, started}}, []CursorRange{{3, 3}, {5, 5}}}},
	} {
		if got := observationPage(t, store, sessionID, nil, test.after, test.limit); !reflect.DeepEqual(got, test.want) {
			t.Errorf("%s: got %v, want %v", test.name, got, test.want)
		}
	}
}

func settledRanges(page *historypb.ReadSessionEventsResponse) []CursorRange {
	var result []CursorRange
	for _, settlement := range page.Settlements {
		result = append(result, settlementRanges(settlement)...)
	}
	return result
}

func TestSettlingStoreDropsMatchedChunksOnceTheirItemCompletes(t *testing.T) {
	store := NewStore(testDB(t), true)
	sessionID := opened(t, store)
	// Cursors 1-2 start the item, 3-6 are two chunk pairs, 7-8 complete it; the completion arrives in a
	// later batch.
	span := textSpan("Hel", "lo").Entries
	if last := appendEntries(t, store, sessionID, span[:6]...); last != 6 {
		t.Fatalf("last cursor %d", last)
	}
	if got := cursors(readPage(t, store, sessionID, 0, 128)); !reflect.DeepEqual(got, []uint64{1, 2, 3, 4, 5, 6}) {
		t.Fatalf("cursors before the completion %v", got)
	}
	pageFollow := followPage(t, store, sessionID, 0)
	if len(pageFollow.Settlements) != 0 {
		t.Fatalf("follow page reported a settlement before its completion committed: %v", pageFollow.Settlements)
	}
	if last := appendEntries(t, store, sessionID, span[6:]...); last != 8 {
		t.Fatalf("last cursor %d", last)
	}
	pageFollow = followPage(t, store, sessionID, 0)
	if len(pageFollow.Settlements) != 1 || pageFollow.Settlements[0].GetCompletionCursor() != 8 {
		t.Fatalf("follow page did not return the settlement with its completion: %v", pageFollow.Settlements)
	}
	page := readPage(t, store, sessionID, 0, 128)
	if got := cursors(page); !reflect.DeepEqual(got, []uint64{1, 2, 7, 8}) || !reflect.DeepEqual(settledRanges(page), []CursorRange{{3, 6}}) ||
		page.Settlements[0].CompletionCursor != 8 {
		t.Fatalf("cursors %v, settled %v", got, settledRanges(page))
	}
	// A runner replaying the whole span, settled chunks included, is still an exact duplicate.
	if last := appendEntries(t, store, sessionID, span...); last != 8 {
		t.Fatalf("last cursor %d", last)
	}
	replay := append(append([]*eventlogpb.EventEntry{}, span[:6]...), entry(7))
	_, err := store.Append(t.Context(), sessionID, replay)
	assertError(t, err, ErrConflict)
	// A page whose window holds the gap or the completion reports the settlement.
	for after, want := range map[uint64][]uint64{2: {7}, 7: {8}} {
		page := readPage(t, store, sessionID, after, 1)
		if got := cursors(page); !reflect.DeepEqual(got, want) || !reflect.DeepEqual(settledRanges(page), []CursorRange{{3, 6}}) {
			t.Errorf("after %d: cursors %v, settled %v", after, got, settledRanges(page))
		}
	}
	if got := settledRanges(readPage(t, store, sessionID, 0, 2)); got != nil {
		t.Fatalf("a page before the gap reports settlement %v", got)
	}
	native, started, completed := "native", "item_started", "item_completed"
	if got, want := observationPage(t, store, sessionID, nil, pointer(0), 30), (ObservationPage{
		8, []Observation{{1, native}, {2, started}, {7, native}, {8, completed}}, []CursorRange{{3, 6}},
	}); !reflect.DeepEqual(got, want) {
		t.Fatalf("observations %v, want %v", got, want)
	}
	if got, want := observationPage(t, store, sessionID, nil, nil, 2), (ObservationPage{
		8, []Observation{{7, native}, {8, completed}}, nil,
	}); !reflect.DeepEqual(got, want) {
		t.Fatalf("tail observations %v, want %v", got, want)
	}
	if got := observationPage(t, store, sessionID, nil, nil, 3).Settled; !reflect.DeepEqual(got, []CursorRange{{3, 6}}) {
		t.Fatalf("tail settled %v", got)
	}
}

func TestAnItemWithoutNativeFramesCompletesUnsettled(t *testing.T) {
	store := NewStore(testDB(t), true)
	sessionID := opened(t, store)
	var batch []*eventlogpb.EventEntry
	for i, event := range []*eventpb.Event{
		{Observation: &eventpb.Event_ItemStarted{ItemStarted: &eventpb.ItemStarted{ItemId: "test-reply"}}},
		{Observation: &eventpb.Event_TextDelta{TextDelta: &eventpb.TextDelta{ItemId: "test-reply", Text: "Hi"}}},
		{Observation: &eventpb.Event_ItemCompleted{ItemCompleted: &eventpb.ItemCompleted{
			ItemId: "test-reply", Outcome: &eventpb.ItemCompleted_Text{Text: "Hi"},
		}}},
	} {
		cursor := uint64(i + 1)
		batch = append(batch, &eventlogpb.EventEntry{Cursor: cursor, Origin: &eventlogpb.EventOrigin{SourceId: "source", Sequence: cursor}, Event: event})
	}
	if last := appendEntries(t, store, sessionID, batch...); last != 3 {
		t.Fatalf("last cursor %d", last)
	}
	if page := readPage(t, store, sessionID, 0, 128); len(page.Entries) != 3 || settledRanges(page) != nil {
		t.Fatalf("entries %d, settled %v", len(page.Entries), settledRanges(page))
	}
}

func TestDefaultStoreKeepsEveryEntry(t *testing.T) {
	store := testStore(t)
	sessionID := opened(t, store)
	appendEntries(t, store, sessionID, textSpan("Hel", "lo").Entries...)
	if page := readPage(t, store, sessionID, 0, 128); len(page.Entries) != 8 || settledRanges(page) != nil {
		t.Fatalf("entries %d, settled %v", len(page.Entries), settledRanges(page))
	}
}

func TestReadSequencesRequiresAPositiveBatchAndNonzeroSequences(t *testing.T) {
	store := NewStore(nil, false)
	for _, batchSize := range []int{0, -1} {
		_, err := store.ReadSequences(t.Context(), uuid.New(), []uint64{1}, batchSize)
		assertError(t, err, ErrInvalidArgument)
	}
	for _, sequences := range [][]uint64{nil, {}, {0}} {
		_, err := store.ReadSequences(t.Context(), uuid.New(), sequences, 1)
		assertError(t, err, ErrInvalidArgument)
	}
}
