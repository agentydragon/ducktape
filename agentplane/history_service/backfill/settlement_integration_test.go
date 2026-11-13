package backfill

import (
	"database/sql"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	"github.com/agentydragon/ducktape/agentplane/history_service/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/testing"
	"github.com/agentydragon/ducktape/agentplane/history_service/testing/spans"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/lease"
	testpostgres "github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
)

var database *testpostgres.Database

func TestMain(m *testing.M) {
	os.Exit(testpostgres.RunIntegrationTests(historytesting.SetupIntegrationDatabase, func(db *testpostgres.Database) int {
		database = db
		return m.Run()
	}))
}

type fixture struct {
	db      *sql.DB
	history *storage.Store
	id      uuid.UUID
	span    *spans.Span
}

func retained(t *testing.T, span *spans.Span) fixture {
	t.Helper()
	db := testpostgres.Open(t, database.URL)
	f := fixture{db: db, history: storage.NewStore(db, storage.Options{}), id: uuid.New(), span: span}
	if _, err := f.history.BindWriter(t.Context(), f.id, storage.ServiceAccountWriter{
		Namespace: "test-namespace", Name: "test-runner", UID: "test-uid",
	}, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := f.history.Append(t.Context(), f.id, span.Entries); err != nil {
		t.Fatal(err)
	}
	return f
}

func (f fixture) count(t *testing.T, table string) int64 {
	t.Helper()
	var count int64
	if err := f.db.QueryRow("SELECT count(*) FROM "+table+" WHERE session_id = $1", f.id).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func (f fixture) projectionSnapshot(t *testing.T) string {
	t.Helper()
	var snapshot string
	if err := f.db.QueryRow(`SELECT jsonb_build_object(
		'entities', (SELECT jsonb_agg(to_jsonb(e) ORDER BY entity_index) FROM thread_entity e WHERE thread_id = $1),
		'chunks', (SELECT jsonb_agg(to_jsonb(c) ORDER BY owner_cursor, owner_id, field, generation, chunk_index)
			FROM thread_payload_chunk c WHERE thread_id = $1),
		'checkpoint', (SELECT to_jsonb(p) FROM thread_checkpoint p WHERE thread_id = $1))::text`, f.id).Scan(&snapshot); err != nil {
		t.Fatal(err)
	}
	return snapshot
}

func (f fixture) deltaEvidence(t *testing.T) int64 {
	t.Helper()
	var count int64
	if err := f.db.QueryRow(`SELECT
		(SELECT count(*) FROM thread_evidence WHERE thread_id = $1 AND observation_cursor BETWEEN 3 AND 6) +
		(SELECT count(*) FROM thread_native_link WHERE thread_id = $1 AND source_sequence BETWEEN 3 AND 6)`, f.id).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func reasoningWithStderr() *spans.Span {
	span := spans.CodexReasoning([][]string{{"Hel", "lo"}})
	completion := span.Entries[len(span.Entries)-1].GetEvent()
	span.Entries = span.Entries[:len(span.Entries)-1]
	span.Stderr("test diagnostic between chunks and semantic completion")
	span.Derived(completion.GetSourceSequences(), completion)
	return span
}

func TestHistoricalSettlementPreservesFoldedContentAndCorrectsPastReaders(t *testing.T) {
	f := retained(t, reasoningWithStderr())
	page, err := f.history.ReadPage(t.Context(), f.id, 0, 1000)
	if err != nil {
		t.Fatal(err)
	}
	if err := postgres.Transaction(t.Context(), f.db, sql.TxOptions{}, func(tx *sql.Tx) error {
		return projection.RecordThreadFold(t.Context(), tx, f.id, f.span.Entries[0].GetOrigin().GetSourceId(), page, "test-epoch")
	}); err != nil {
		t.Fatal(err)
	}
	before := f.projectionSnapshot(t)
	if f.deltaEvidence(t) == 0 {
		t.Fatal("fixture has no folded delta evidence")
	}
	dry, err := RunSession(t.Context(), f.db, f.id, false)
	if err != nil || dry.Items != 1 || dry.Rows != 4 || dry.PayloadBytes <= 0 {
		t.Fatalf("dry run: %+v %v", dry, err)
	}
	if f.count(t, "session_event") != 9 || f.count(t, "session_event_settlement") != 0 ||
		f.count(t, "session_event_removed_range") != 0 || f.count(t, "session_projection_lease") != 0 || f.deltaEvidence(t) == 0 {
		t.Fatal("dry run changed history or projection metadata")
	}
	applied, err := RunSession(t.Context(), f.db, f.id, true)
	if err != nil || applied != dry {
		t.Fatalf("apply: %+v %v, want %+v", applied, err, dry)
	}
	if f.count(t, "session_event") != 5 || f.deltaEvidence(t) != 0 || f.projectionSnapshot(t) != before {
		t.Fatal("settlement changed content/checkpoint or left delta evidence")
	}
	page, err = f.history.ReadPage(t.Context(), f.id, 0, 1000)
	if err != nil || page.GetLastCursor() != 9 || len(page.GetSettlements()) != 1 || len(page.GetEntries()) != 5 {
		t.Fatalf("settled page: %v %v", page, err)
	}
	for i, index := range []int{0, 1, 6, 7, 8} {
		if page.Entries[i].GetCursor() != f.span.Entries[index].GetCursor() {
			t.Fatalf("retained cursors: %v", page.Entries)
		}
	}
	// This is the correction query FollowSession makes after catching up, including on an idle wake.
	removed, err := f.history.ReadFollowRemoved(t.Context(), f.id, 0, 9)
	if err != nil || len(removed) != 1 || removed[0].GetFirst() != 3 || removed[0].GetLast() != 6 {
		t.Fatalf("late reader correction: %v %v", removed, err)
	}
	if _, err := f.history.Append(t.Context(), f.id, f.span.Entries); err != nil {
		t.Fatal(err)
	}
	if again, err := RunSession(t.Context(), f.db, f.id, true); err != nil || again != (Result{}) || f.count(t, "session_event") != 5 {
		t.Fatalf("rerun/replay: %+v %v", again, err)
	}
}

func TestHistoricalSettlementReadsAndPrunesPackedHistory(t *testing.T) {
	db := testpostgres.Open(t, database.URL)
	history := storage.NewStore(db, storage.Options{CompactDeltas: true})
	id := uuid.New()
	if _, err := history.BindWriter(t.Context(), id, storage.ServiceAccountWriter{Namespace: "test-namespace", Name: "test-runner", UID: "test-uid"}, ""); err != nil {
		t.Fatal(err)
	}
	chunks := make([]string, 80)
	for index := range chunks {
		chunks[index] = "hello"
	}
	span := spans.ClaudeText(chunks, spans.TextOptions{})
	if _, err := history.Append(t.Context(), id, span.Entries); err != nil {
		t.Fatal(err)
	}
	var blocks int
	if err := db.QueryRowContext(t.Context(), "SELECT count(*) FROM session_event_block WHERE session_id=$1", id).Scan(&blocks); err != nil || blocks == 0 {
		t.Fatalf("blocks %d: %v", blocks, err)
	}
	dry, err := RunSession(t.Context(), db, id, false)
	if err != nil || dry.Items != 1 || dry.Rows != 160 || dry.PayloadBytes <= 0 {
		t.Fatalf("dry run: %+v, %v", dry, err)
	}
	applied, err := RunSession(t.Context(), db, id, true)
	if err != nil || applied != dry {
		t.Fatalf("apply: %+v, %v", applied, err)
	}
	page, err := history.ReadPage(t.Context(), id, 0, 1000)
	if err != nil || len(page.Entries) != 4 || len(page.Settlements) != 1 {
		t.Fatalf("page: %v, %v", page, err)
	}
	if page.Entries[0].Cursor != 1 || page.Entries[3].Cursor != uint64(len(span.Entries)) {
		t.Fatalf("retained cursor bounds: %v", page)
	}
	if page.Settlements[0].Template == "" {
		t.Fatal("missing settlement proof")
	}
	reads, err := history.ReadSequences(t.Context(), id, []uint64{3}, 10)
	if err != nil || !reads.Reads[0].Unstored || reads.Reads[0].NextEntry.Cursor != uint64(len(span.Entries)-1) {
		t.Fatalf("reads: %v, %v", reads, err)
	}
}

func TestBackfillRetainsDeltasReferencedAfterTheirCompletion(t *testing.T) {
	span := spans.ClaudeText([]string{"Hel", "lo"}, spans.TextOptions{})
	span.Stderr("test diagnostic citing an old native chunk")
	span.Entries[len(span.Entries)-1].Event.SourceSequences = []uint64{3}
	f := retained(t, span)
	result, err := RunSession(t.Context(), f.db, f.id, true)
	if err != nil || result != (Result{}) || f.count(t, "session_event") != 9 || f.count(t, "session_projection_lease") != 0 {
		t.Fatalf("referenced span: %+v %v", result, err)
	}
}

func TestBackfillFencesProjectionAndRefusesAChangedPrefix(t *testing.T) {
	f := retained(t, spans.ClaudeText(strings.Split(strings.Repeat("x", 100), ""), spans.TextOptions{}))
	found, err := scanSession(t.Context(), f.db, f.id)
	if err != nil {
		t.Fatal(err)
	}
	var held *projection.Lease
	if err := postgres.Transaction(t.Context(), f.db, sql.TxOptions{}, func(tx *sql.Tx) (err error) {
		held, err = projection.AcquireLease(t.Context(), tx, f.id, time.Minute)
		return err
	}); err != nil || held == nil {
		t.Fatalf("acquire: %v", err)
	}
	if _, err := RunSession(t.Context(), f.db, f.id, true); !errors.Is(err, ErrProjectionBusy) {
		t.Fatalf("busy projection: %v", err)
	}
	if f.count(t, "session_event_settlement") != 0 {
		t.Fatal("settled under a live projection lease")
	}
	if _, err := f.db.Exec("UPDATE session_projection_lease SET expires_at = clock_timestamp() - interval '1 second' WHERE session_id = $1", f.id); err != nil {
		t.Fatal(err)
	}
	if result, err := RunSession(t.Context(), f.db, f.id, true); err != nil || result.Rows != 200 {
		t.Fatalf("expired projection lease: %+v %v", result, err)
	}
	// A projector holding a fetched page from before settlement cannot resurrect its evidence.
	if err := postgres.Transaction(t.Context(), f.db, sql.TxOptions{}, func(tx *sql.Tx) error {
		return projection.FenceLease(t.Context(), tx, *held, f.id)
	}); !errors.Is(err, lease.ErrLost) {
		t.Fatalf("stale fold fence: %v", err)
	}
	f.span.Stderr("test new event after the scan")
	if _, err := f.history.Append(t.Context(), f.id, f.span.Entries[len(f.span.Entries)-1:]); err != nil {
		t.Fatal(err)
	}
	if _, err := settleCompletion(t.Context(), f.db, f.id, found.completions[0], found, true); !errors.Is(err, ErrSessionAdvanced) {
		t.Fatalf("advanced prefix: %v", err)
	}
	if f.count(t, "session_projection_lease") != 0 {
		t.Fatal("failed transaction left a lease")
	}
}

func TestBackfillRollsBackDeletionAndMetadataTogether(t *testing.T) {
	f := retained(t, spans.ClaudeText([]string{"Hel", "lo"}, spans.TextOptions{}))
	name := "test_reject_" + strings.ReplaceAll(f.id.String(), "-", "")
	if _, err := f.db.Exec(fmt.Sprintf(`CREATE FUNCTION %s() RETURNS trigger LANGUAGE plpgsql AS
		$$ BEGIN RAISE EXCEPTION 'test cleanup failure'; END $$;
		CREATE TRIGGER %s BEFORE INSERT ON session_event_removed_range FOR EACH ROW
		WHEN (NEW.session_id = '%s'::uuid) EXECUTE FUNCTION %s()`, name, name, f.id, name)); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if _, err := f.db.Exec("DROP FUNCTION " + name + "() CASCADE"); err != nil {
			t.Error(err)
		}
	})
	if _, err := RunSession(t.Context(), f.db, f.id, true); err == nil {
		t.Fatal("apply ignored a correction failure")
	}
	for table, want := range map[string]int64{
		"session_event": 8, "session_event_settlement": 0, "session_event_settled_range": 0,
		"session_event_removed_range": 0, "session_projection_lease": 0,
	} {
		if got := f.count(t, table); got != want {
			t.Fatalf("%s: %d, want %d", table, got, want)
		}
	}
}

func TestBackfillKeepsAnIneligibleSpan(t *testing.T) {
	wrong := "different completed content"
	span := spans.ClaudeText([]string{"Hel", "lo"}, spans.TextOptions{Completed: &wrong})
	f := retained(t, span)
	// An item that cannot settle must not contend with its live projector's write lease.
	if err := postgres.Transaction(t.Context(), f.db, sql.TxOptions{}, func(tx *sql.Tx) error {
		_, err := projection.AcquireLease(t.Context(), tx, f.id, time.Minute)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if result, err := RunSession(t.Context(), f.db, f.id, true); err != nil || result != (Result{}) || f.count(t, "session_event") != 8 {
		t.Fatalf("ineligible completion: %+v %v", result, err)
	}
}
