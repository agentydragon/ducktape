package storage

import (
	"database/sql"
	"math"
	"strings"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
)

func followPage(t *testing.T, store *Store, sessionID uuid.UUID, after uint64, kinds ...historypb.EventKind) FollowPage {
	t.Helper()
	page, err := store.ReadFollowEntries(t.Context(), sessionID, after, kinds, true)
	if err != nil {
		t.Fatal(err)
	}
	return page
}

func TestFollowStarts(t *testing.T) {
	store := testStore(t)
	sessionID := opened(t, store)
	appendEntries(t, store, sessionID, entry(1), nativeEntry(2, "a"), entry(3), nativeEntry(4, "b"))
	for _, test := range []struct {
		name    string
		request *historypb.FollowSessionRequest
		want    uint64
		wantErr error
	}{
		{"beginning", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_AfterCursor{}}, 0, nil},
		{"cursor", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_AfterCursor{AfterCursor: 2}}, 2, nil},
		{"tail", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_Tail{Tail: &emptypb.Empty{}}}, 4, nil},
		{"one back", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_Back{Back: &historypb.StartBack{Kind: historypb.EventKind_EVENT_KIND_NATIVE, Count: 1}}}, 3, nil},
		{"two back", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_Back{Back: &historypb.StartBack{Kind: historypb.EventKind_EVENT_KIND_NATIVE, Count: 2}}}, 1, nil},
		{"fewer than back", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_Back{Back: &historypb.StartBack{Kind: historypb.EventKind_EVENT_KIND_NATIVE, Count: 3}}}, 0, nil},
		{"no matching back", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_Back{Back: &historypb.StartBack{Kind: historypb.EventKind_EVENT_KIND_TURN_STARTED, Count: 1}}}, 0, nil},
		{"no start", &historypb.FollowSessionRequest{}, 0, ErrInvalidArgument},
		{"zero back", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_Back{Back: &historypb.StartBack{Kind: historypb.EventKind_EVENT_KIND_NATIVE}}}, 0, ErrInvalidArgument},
		{"unspecified back", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_Back{Back: &historypb.StartBack{Count: 1}}}, 0, ErrInvalidArgument},
		{"unknown kind", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_AfterCursor{}, Kinds: []historypb.EventKind{9999}}, 0, ErrInvalidArgument},
		{"unspecified filter", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_AfterCursor{}, Kinds: []historypb.EventKind{0}}, 0, ErrInvalidArgument},
		{"above tail", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_AfterCursor{AfterCursor: 5}}, 0, ErrOutOfRange},
		{"unsigned overflow", &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_AfterCursor{AfterCursor: math.MaxUint64}}, 0, ErrOutOfRange},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := store.ResolveFollowStart(t.Context(), sessionID, test.request)
			if got != test.want {
				t.Fatalf("start %d, want %d", got, test.want)
			}
			if test.wantErr != nil {
				assertError(t, err, test.wantErr)
			} else if err != nil {
				t.Fatal(err)
			}
		})
	}
	_, err := store.ResolveFollowStart(t.Context(), uuid.New(), &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_AfterCursor{}})
	assertError(t, err, ErrNotFound)
	tail, err := store.ResolveFollowStart(t.Context(), sessionID, &historypb.FollowSessionRequest{Start: &historypb.FollowSessionRequest_Tail{Tail: &emptypb.Empty{}}})
	if err != nil {
		t.Fatal(err)
	}
	appendEntries(t, store, sessionID, entry(5))
	assertEntries(t, followPage(t, store, sessionID, tail).Entries, entry(5))
}

func TestFollowEntryBoundsAndFilteredCursor(t *testing.T) {
	store := testStore(t)
	sessionID := opened(t, store)
	batch := entries(1, 260)
	batch[128] = nativeEntry(129, "selected")
	appendEntries(t, store, sessionID, batch...)
	first := followPage(t, store, sessionID, 0)
	if len(first.Entries) != FollowEntriesLimit || first.ThroughCursor != 128 || first.LastCursor != 260 {
		t.Fatalf("first page: %d entries through %d of %d", len(first.Entries), first.ThroughCursor, first.LastCursor)
	}
	second := followPage(t, store, sessionID, first.ThroughCursor)
	if len(second.Entries) != FollowEntriesLimit || second.ThroughCursor != 256 {
		t.Fatalf("second page: %d entries through %d", len(second.Entries), second.ThroughCursor)
	}
	last := followPage(t, store, sessionID, second.ThroughCursor)
	assertEntries(t, last.Entries, batch[256:]...)
	if last.ThroughCursor != 260 {
		t.Fatalf("last page through %d", last.ThroughCursor)
	}
	filtered := followPage(t, store, sessionID, 0, historypb.EventKind_EVENT_KIND_NATIVE)
	assertEntries(t, filtered.Entries, batch[128])
	if filtered.ThroughCursor != 260 {
		t.Fatalf("filtered page through %d", filtered.ThroughCursor)
	}
	// Repeated kinds are set membership, and merging multiple indexed kinds preserves cursor order.
	multiple := followPage(t, store, sessionID, 120, historypb.EventKind_EVENT_KIND_NATIVE,
		historypb.EventKind_EVENT_KIND_HARNESS_STARTED, historypb.EventKind_EVENT_KIND_NATIVE)
	assertEntries(t, multiple.Entries, batch[120:248]...)
	if multiple.ThroughCursor != 248 {
		t.Fatalf("multiple kinds through %d", multiple.ThroughCursor)
	}
	empty := followPage(t, store, sessionID, 0, historypb.EventKind_EVENT_KIND_TURN_COMPLETED)
	if len(empty.Entries) != 0 || empty.ThroughCursor != 260 {
		t.Fatalf("filtered empty page: %+v", empty)
	}
	_, err := store.ReadFollowEntries(t.Context(), sessionID, math.MaxUint64, nil, true)
	assertError(t, err, ErrOutOfRange)
	_, err = store.ReadFollowEntries(t.Context(), uuid.New(), 0, nil, true)
	assertError(t, err, ErrNotFound)
}

func TestFollowByteBoundAndOversizedEntry(t *testing.T) {
	store := testStore(t)
	sessionID := opened(t, store)
	batch := []*eventlogpb.EventEntry{
		nativeEntry(1, strings.Repeat("a", FollowBytesLimit/2)),
		nativeEntry(2, strings.Repeat("b", FollowBytesLimit/2)),
		nativeEntry(3, strings.Repeat("c", FollowBytesLimit+1)),
		entry(4),
	}
	appendEntries(t, store, sessionID, batch...)
	for i := range batch {
		page := followPage(t, store, sessionID, uint64(i))
		assertEntries(t, page.Entries, batch[i])
		wantThrough := uint64(i + 1)
		if page.ThroughCursor != wantThrough {
			t.Fatalf("page after %d through %d, want %d", i, page.ThroughCursor, wantThrough)
		}
		if i != 2 && proto.Size(page.Entries[0]) > FollowBytesLimit {
			t.Fatal("normal frame exceeds byte bound")
		}
	}
}

func TestFollowFilterDoesNotDecodeOtherKinds(t *testing.T) {
	db := testDB(t)
	store := NewStore(db, false)
	sessionID := opened(t, store)
	native := nativeEntry(2, "selected")
	appendEntries(t, store, sessionID, entry(1), native, entry(3))
	// Nonmatching payloads must stay in the database; an invalid one makes accidental decoding
	// fail instead of merely returning an extra entry the caller could filter afterward.
	if _, err := db.Exec("UPDATE session_event SET payload = $2 WHERE session_id = $1 AND cursor <> 2", sessionID, []byte{0xff}); err != nil {
		t.Fatal(err)
	}
	page := followPage(t, store, sessionID, 0, historypb.EventKind_EVENT_KIND_NATIVE)
	assertEntries(t, page.Entries, native)
	if page.ThroughCursor != 3 {
		t.Fatalf("through %d, want filtered tail 3", page.ThroughCursor)
	}
}

func TestFollowMetadataReplaysLateCorrectionsWithBoundedPagination(t *testing.T) {
	db := testDB(t)
	store := NewStore(db, false)
	sessionID := opened(t, store)
	appendEntries(t, store, sessionID, entries(1, 10)...)
	page := followPage(t, store, sessionID, 0)
	if page.ThroughCursor != 10 {
		t.Fatalf("through %d", page.ThroughCursor)
	}
	removed, err := store.ReadFollowRemoved(t.Context(), sessionID, 0, 10)
	if err != nil || len(removed) != 0 {
		t.Fatalf("initial removal %v, error %v", removed, err)
	}
	removeEntries(t, db, sessionID, 2, 3, 8)
	removed, err = store.ReadFollowRemoved(t.Context(), sessionID, 0, page.ThroughCursor)
	if err != nil || !proto.Equal(&historypb.ReadSessionEventsResponse{Removed: removed}, &historypb.ReadSessionEventsResponse{
		Removed: []*historypb.CursorRange{{First: 2, Last: 3}, {First: 8, Last: 8}},
	}) {
		t.Fatalf("removals %v, error %v", removed, err)
	}
	settlements := []*historypb.SettledDeltas{
		{ItemId: "first", CompletionCursor: 6, Ranges: []*historypb.CursorRange{{First: 4, Last: 5}}},
		{ItemId: "second", CompletionCursor: 10, Ranges: []*historypb.CursorRange{{First: 7, Last: 7}}},
	}
	for _, settlement := range settlements {
		if err := transaction(t.Context(), db, sql.TxOptions{}, func(tx *sql.Tx) error {
			return recordSettlement(t.Context(), tx, sessionID, settlement)
		}); err != nil {
			t.Fatal(err)
		}
	}
	page = followPage(t, store, sessionID, 0)
	if len(page.Settlements) != 2 || !proto.Equal(page.Settlements[0], settlements[0]) || !proto.Equal(page.Settlements[1], settlements[1]) {
		t.Fatalf("follow settlements %v, want %v", page.Settlements, settlements)
	}
	page, err = store.ReadFollowEntries(t.Context(), sessionID, 6, nil, true)
	if err != nil || len(page.Settlements) != 1 || !proto.Equal(page.Settlements[0], settlements[1]) {
		t.Fatalf("settlements after completion 6: %v, error %v", page.Settlements, err)
	}
	page, err = store.ReadFollowEntries(t.Context(), sessionID, 10, nil, true)
	if err != nil || len(page.Settlements) != 0 {
		t.Fatalf("settlements at or before the resume cursor: %v, error %v", page.Settlements, err)
	}
	page, err = store.ReadFollowEntries(t.Context(), sessionID, 0, nil, false)
	if err != nil || len(page.Settlements) != 0 {
		t.Fatalf("excluded settlements: %v, error %v", page.Settlements, err)
	}
}

func TestFollowPageBoundsSettlementsWhenKindsFilterOutCompletions(t *testing.T) {
	db := testDB(t)
	store := NewStore(db, false)
	sessionID := opened(t, store)
	appendEntries(t, store, sessionID, entries(1, FollowSettlementsLimit+1)...)
	for cursor := uint64(1); cursor <= FollowSettlementsLimit+1; cursor++ {
		settled := &historypb.SettledDeltas{CompletionCursor: cursor}
		if err := transaction(t.Context(), db, sql.TxOptions{}, func(tx *sql.Tx) error {
			return recordSettlement(t.Context(), tx, sessionID, settled)
		}); err != nil {
			t.Fatal(err)
		}
	}
	page, err := store.ReadFollowEntries(t.Context(), sessionID, 0, []historypb.EventKind{historypb.EventKind_EVENT_KIND_TEXT_DELTA}, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Entries) != 0 || len(page.Settlements) != FollowSettlementsLimit || page.ThroughCursor != FollowSettlementsLimit {
		t.Fatalf("bounded page: entries %d, settlements %d, through %d", len(page.Entries), len(page.Settlements), page.ThroughCursor)
	}
	page, err = store.ReadFollowEntries(t.Context(), sessionID, page.ThroughCursor, []historypb.EventKind{historypb.EventKind_EVENT_KIND_TEXT_DELTA}, true)
	if err != nil || len(page.Entries) != 0 || len(page.Settlements) != 1 || page.ThroughCursor != FollowSettlementsLimit+1 {
		t.Fatalf("remaining page: entries %d, settlements %d, through %d, error %v", len(page.Entries), len(page.Settlements), page.ThroughCursor, err)
	}
}
