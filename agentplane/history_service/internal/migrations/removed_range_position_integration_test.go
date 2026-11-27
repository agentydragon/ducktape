package migrations

import (
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	"github.com/google/uuid"
	"google.golang.org/protobuf/proto"
)

func TestRemovedRangePositionsKeepRemovalsRecordedBeforeTheUpgrade(t *testing.T) {
	db := baselineDB(t)
	upTo(t, db, 19)
	followed, other := uuid.New(), uuid.New()
	for _, id := range []uuid.UUID{followed, other} {
		execute(t, db, `INSERT INTO session_history(id,last_cursor,source_id) VALUES($1,10,'fixture-source')`, id)
	}
	// Inserted out of cursor order, interleaved with another Session's removal.
	for _, removed := range []struct {
		session     uuid.UUID
		first, last uint64
	}{{followed, 7, 8}, {other, 4, 4}, {followed, 2, 3}} {
		execute(t, db, `INSERT INTO session_event_removed_range(session_id,first_cursor,last_cursor) VALUES($1,$2,$3)`,
			removed.session, removed.first, removed.last)
	}
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}

	history := storage.NewStore(db, storage.Options{}).WithReadScope(storage.AllSessions())
	read := func(position uint64) storage.FollowRemovals {
		t.Helper()
		removed, err := history.ReadFollowRemoved(t.Context(), followed, position, 10)
		if err != nil {
			t.Fatal(err)
		}
		return removed
	}
	equal := func(got, want []*historypb.CursorRange) bool {
		return proto.Equal(&historypb.ReadSessionEventsResponse{Removed: got}, &historypb.ReadSessionEventsResponse{Removed: want})
	}
	// A follower opened after the upgrade reports each retained removal once, then none on later catch-ups.
	opening := read(0)
	if !equal(opening.Ranges, []*historypb.CursorRange{{First: 2, Last: 3}, {First: 7, Last: 8}}) || opening.Position != 2 {
		t.Fatalf("opening removals %+v", opening)
	}
	if again := read(opening.Position); len(again.Ranges) != 0 || again.Position != opening.Position {
		t.Fatalf("catch-up repeated retained removals: %+v", again)
	}
	// A cleanup after the upgrade continues the Session's positions and is reported once.
	execute(t, db, `INSERT INTO session_event_removed_range(session_id,first_cursor,last_cursor) VALUES($1,1,1)`, followed)
	later := read(opening.Position)
	if !equal(later.Ranges, []*historypb.CursorRange{{First: 1, Last: 1}}) || later.Position != 3 {
		t.Fatalf("removal after the upgrade %+v", later)
	}
	if again := read(later.Position); len(again.Ranges) != 0 {
		t.Fatalf("catch-up repeated a new removal: %+v", again)
	}
	otherRemovals, err := history.ReadFollowRemoved(t.Context(), other, 0, 10)
	if err != nil || !equal(otherRemovals.Ranges, []*historypb.CursorRange{{First: 4, Last: 4}}) || otherRemovals.Position != 1 {
		t.Fatalf("other Session's removals %+v, error %v", otherRemovals, err)
	}
}
