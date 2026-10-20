package projection

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/reads"
	"github.com/agentydragon/ducktape/agentplane/history_service/threadfold"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

// The fold's rows that grow with a Session's history. An update to an old item must reach them through their
// indexes only.
var historyTables = []string{"thread_entity", "thread_payload_manifest", "thread_payload_chunk", "thread_evidence"}

const foldPageEntries = 1_000

func itemStarted(cursor uint64, itemID string) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{
		ItemStarted: &eventpb.ItemStarted{ItemId: itemID, Kind: eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT},
	}})
}

func itemDelta(cursor uint64, itemID, text string) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_TextDelta{
		TextDelta: &eventpb.TextDelta{ItemId: itemID, Text: text},
	}})
}

func commandAdmitted(cursor uint64, commandID string) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_CommandAdmitted{CommandAdmitted: &eventpb.CommandAdmitted{
		Command: &commandpb.Command{
			CommandId: commandID, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "saved"}},
		},
	}}})
}

// historyEntry is the filler after the Session's first two entries: `materialized` items, each started and then
// given a body, followed by native packets that no entity keeps.
func historyEntry(cursor uint64, materialized int) *eventlogpb.EventEntry {
	index := int(cursor) - 3
	if index >= materialized*2 {
		return native(cursor)
	}
	itemID := fmt.Sprintf("history-%d", index/2)
	if index%2 == 0 {
		return itemStarted(cursor, itemID)
	}
	return itemDelta(cursor, itemID, "materialized")
}

// foldInPages folds the entries from `first` through `last` in pages, as a fold running behind the journal does.
func foldInPages(t *testing.T, db *sql.DB, threadID uuid.UUID, first, last uint64, at func(cursor uint64) *eventlogpb.EventEntry) {
	t.Helper()
	for start := first; start <= last; start += foldPageEntries {
		page := &historypb.ReadSessionEventsResponse{}
		for cursor := start; cursor <= min(start+foldPageEntries-1, last); cursor++ {
			page.Entries = append(page.Entries, at(cursor))
		}
		page.LastCursor = page.Entries[len(page.Entries)-1].Cursor
		mustRecord(t, db, threadID, page)
	}
}

type tableReads struct{ seqScans, indexScans, tuples uint64 }

// readsDuring reports what f read of each history table, from PostgreSQL's own scan counters. f runs on a handle
// of one connection: a connection publishes its counters only when it goes idle after being told to
// (`pg_stat_force_next_flush`), so the counters are flushed before and after f and read from `observer`.
func readsDuring(t *testing.T, observer *sql.DB, f func(db *sql.DB)) map[string]tableReads {
	t.Helper()
	worker := openDB(t, "")
	worker.SetMaxOpenConns(1)
	flush := func() {
		if _, err := worker.Exec("SELECT pg_stat_force_next_flush()"); err != nil {
			t.Fatal(err)
		}
	}
	counters := func() map[string]tableReads {
		rows, err := observer.Query(`SELECT relname, seq_scan, idx_scan, seq_tup_read + idx_tup_fetch
			FROM pg_stat_user_tables WHERE relname = ANY($1)`, pq.Array(historyTables))
		if err != nil {
			t.Fatal(err)
		}
		defer rows.Close()
		found := map[string]tableReads{}
		for rows.Next() {
			var table string
			var read tableReads
			if err := rows.Scan(&table, &read.seqScans, &read.indexScans, &read.tuples); err != nil {
				t.Fatal(err)
			}
			found[table] = read
		}
		if err := rows.Err(); err != nil {
			t.Fatal(err)
		}
		return found
	}
	flush()
	before := counters()
	f(worker)
	flush()
	after := counters()
	delta := map[string]tableReads{}
	for table, read := range after {
		delta[table] = tableReads{
			seqScans:   read.seqScans - before[table].seqScans,
			indexScans: read.indexScans - before[table].indexScans,
			tuples:     read.tuples - before[table].tuples,
		}
	}
	return delta
}

func TestAnUpdateToAnOldItemReadsTheSameRowsHoweverLongTheHistory(t *testing.T) {
	db := testDB(t)
	sizes := []int{4_000, 16_000}
	foldTuples, lookupTuples := map[int]map[string]uint64{}, map[int]map[string]uint64{}
	for _, size := range sizes {
		t.Run(fmt.Sprintf("%d entries", size), func(t *testing.T) {
			materialized := size / 2
			threadID := uuid.New()
			mustRecord(t, db, threadID, pageOf(commandAdmitted(1, "admission"), itemDelta(2, "old-item", "before history")))
			foldInPages(t, db, threadID, 3, uint64(size)+2, func(cursor uint64) *eventlogpb.EventEntry {
				return historyEntry(cursor, materialized)
			})
			if _, err := db.Exec("ANALYZE " + strings.Join(historyTables, ", ")); err != nil {
				t.Fatal(err)
			}
			if got := count(t, db, "SELECT count(*) FROM thread_entity WHERE thread_id = $1 AND entity_kind = 'item'", threadID); got != materialized+1 {
				t.Fatalf("%d items folded, want %d", got, materialized+1)
			}

			check := func(what string, reads map[string]tableReads, tuples map[int]map[string]uint64) {
				tuples[size] = map[string]uint64{}
				for _, table := range historyTables {
					read := reads[table]
					t.Logf("%s, %s: %+v", what, table, read)
					if read.seqScans != 0 {
						t.Errorf("%s scanned %s %d times with a history of %d entries; want index lookups only", what, table, read.seqScans, size)
					}
					tuples[size][table] = read.tuples
				}
				if tuples[size]["thread_entity"] == 0 {
					t.Errorf("%s read no thread_entity rows: the counters did not report it", what)
				}
			}

			update := itemDelta(uint64(size)+3, "old-item", " after history")
			check("the update", readsDuring(t, db, func(worker *sql.DB) {
				tx, err := worker.Begin()
				if err != nil {
					t.Fatal(err)
				}
				defer tx.Rollback()
				if err := RecordThreadFold(context.Background(), tx, threadID, testSource, pageOf(update), testEpoch); err != nil {
					t.Fatal(err)
				}
				if err := tx.Commit(); err != nil {
					t.Fatal(err)
				}
			}), foldTuples)
			item := entities(t, db, threadID)["item/old-item"]
			if item.revision != update.Cursor {
				t.Fatalf("old-item is at revision %d, want the update's %d", item.revision, update.Cursor)
			}
			if body := bodyOf(t, db, threadID, *item.textRef); body != "before history after history" {
				t.Fatalf("old-item body = %q", body)
			}

			// The fold tables above were written directly; the read serves only a Session that History holds.
			if _, err := db.Exec("INSERT INTO session_history (id, last_cursor) VALUES ($1, $2)", threadID, update.Cursor); err != nil {
				t.Fatal(err)
			}
			request := &historypb.ReadThreadEntitiesRequest{
				SessionId: threadID.String(),
				Window: &historypb.ReadThreadEntitiesRequest_Keys{Keys: &historypb.EntityKeys{
					Keys: []*historypb.EntityKey{{EntityKind: string(threadfold.KindCommand), EntityId: "admission"}},
				}},
			}
			check("the command lookup", readsDuring(t, db, func(worker *sql.DB) {
				response, err := (&reads.Reader{DB: worker}).ReadThreadEntities(context.Background(), request)
				if err != nil {
					t.Fatal(err)
				}
				if len(response.Entities) != 1 {
					t.Fatalf("the lookup found %d entities, want the admitted command", len(response.Entities))
				}
			}), lookupTuples)
		})
	}
	for what, tuples := range map[string]map[int]map[string]uint64{"update": foldTuples, "command lookup": lookupTuples} {
		small, large := tuples[sizes[0]], tuples[sizes[len(sizes)-1]]
		for _, table := range historyTables {
			if large[table] > small[table] {
				t.Errorf("the %s read %d rows of %s in a %d-entry history but %d in a %d-entry one", what, large[table], table, sizes[len(sizes)-1], small[table], sizes[0])
			}
		}
	}
}
