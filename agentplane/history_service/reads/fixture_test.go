package reads

import (
	"database/sql"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/history_service/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	testEpoch    = "test-epoch"
	testSource   = "test-runner"
	testPageSize = 8
)

var testOrigin = time.Date(2026, 10, 10, 12, 0, 0, 0, time.UTC)

type fixture struct {
	db        *sql.DB
	history   *storage.Store
	reader    *Reader
	projector *projection.Projector
}

func newFixture(t *testing.T) fixture {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	history := storage.NewStore(db, false)
	return fixture{
		db: db, history: history, reader: &Reader{DB: db, History: history, NativeFrameReadBatchSize: 200},
		projector: &projection.Projector{DB: db, History: history, Epoch: testEpoch, PageSize: testPageSize},
	}
}

// session records a Session written by a Sandbox called `sandbox`, with the summary row the fold's discovery gives it.
// Its entries are stored with `history`.
func (f fixture) session(t *testing.T, history *storage.Store, sandbox string, entries ...*eventlogpb.EventEntry) uuid.UUID {
	t.Helper()
	sessionID := uuid.New()
	writer := storage.Writer{Namespace: "test-sandbox-namespace", Name: sandbox, UID: "test-runner-account-uid"}
	if _, err := history.BindWriter(t.Context(), sessionID, writer, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := f.db.Exec("INSERT INTO thread_history_summary (thread_id) VALUES ($1)", sessionID); err != nil {
		t.Fatal(err)
	}
	f.appendTo(t, history, sessionID, entries...)
	return sessionID
}

func (f fixture) appendTo(t *testing.T, history *storage.Store, sessionID uuid.UUID, entries ...*eventlogpb.EventEntry) {
	t.Helper()
	if len(entries) == 0 {
		return
	}
	for _, entry := range entries {
		if entry.Event.At == nil {
			entry.Event.At = timestamppb.New(testOrigin.Add(time.Duration(entry.Cursor) * time.Second))
		}
	}
	if _, err := history.Append(t.Context(), sessionID, entries); err != nil {
		t.Fatal(err)
	}
}

// fold projects the Session's history until the fold reaches it.
func (f fixture) fold(t *testing.T, sessionID uuid.UUID) {
	t.Helper()
	var lease *projection.Lease
	tx, err := f.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if lease, err = projection.AcquireLease(t.Context(), tx, sessionID, time.Minute); err != nil {
		t.Fatal(err)
	}
	if lease == nil {
		t.Fatalf("lease for %s is held", sessionID)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	for {
		progress, err := f.projector.ProjectBatch(t.Context(), sessionID, *lease)
		if err != nil {
			t.Fatal(err)
		}
		if progress.ThroughCursor >= progress.CommittedCursor {
			return
		}
	}
}

func entry(cursor uint64, observation *eventpb.Event, sourceSequences ...uint64) *eventlogpb.EventEntry {
	observation.SourceSequences = sourceSequences
	observation.At = timestamppb.New(testOrigin.Add(time.Duration(cursor) * time.Second))
	return &eventlogpb.EventEntry{
		Cursor: cursor,
		Origin: &eventlogpb.EventOrigin{SourceId: testSource, Sequence: cursor},
		Event:  observation,
	}
}

func native(cursor uint64, line string) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_Native{Native: &eventpb.Native{Line: line}}})
}

func harnessStarted(cursor uint64) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_HarnessStarted{HarnessStarted: &eventpb.HarnessStarted{}}})
}

func turnStarted(cursor uint64) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "test-turn"}}})
}

func replyStarted(cursor uint64, sourceSequences ...uint64) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_ItemStarted{
		ItemStarted: &eventpb.ItemStarted{ItemId: "test-reply", Kind: eventpb.ItemKind_ITEM_KIND_ASSISTANT_TEXT},
	}}, sourceSequences...)
}

func replyDelta(cursor uint64, text string) *eventlogpb.EventEntry {
	return entry(cursor, &eventpb.Event{Observation: &eventpb.Event_TextDelta{
		TextDelta: &eventpb.TextDelta{ItemId: "test-reply", Text: text},
	}})
}
