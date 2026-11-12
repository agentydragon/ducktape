package centralsampler

import (
	"bytes"
	"database/sql"
	"strings"
	"sync"
	"testing"
)

func migrationExec(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
}

func migrationCount(t *testing.T, db *sql.DB, query string, args ...any) int {
	t.Helper()
	var count int
	if err := db.QueryRowContext(t.Context(), query, args...).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func TestFreshSamplerBaseline(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version
		WHERE version_id=11 AND is_applied`); got != 1 {
		t.Fatalf("fresh baseline history rows = %d, want 1", got)
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_conversation
		WHERE id=$1 AND api='responses'`, session.ConversationID); got != 1 {
		t.Fatalf("fresh conversation rows = %d, want 1", got)
	}
}

func TestFreshSamplerMigrationsRemoveRedundantEventIndex(t *testing.T) {
	store := newTestStore(t)
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version
		WHERE version_id=12 AND is_applied`); got != 1 {
		t.Fatalf("fresh index migration history rows = %d, want 1", got)
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM pg_indexes
		WHERE schemaname=current_schema() AND indexname='sampler_event_sequence_idx'`); got != 0 {
		t.Fatalf("redundant event index count = %d, want 0", got)
	}
	assertSamplerEventPrimaryKey(t, store.DB())
}

func TestSamplerVersion10AdoptionRemovesRedundantEventIndex(t *testing.T) {
	store := newInlineTestStore(t)
	const conversationID = "00000000-0000-0000-0000-000000000001"
	const sessionID = "baseline-session"
	migrationExec(t, store.DB(), `INSERT INTO sampler_conversation(id,api) VALUES($1,'responses')`, conversationID)
	migrationExec(t, store.DB(), `INSERT INTO sampler_session(id,conversation_id,account_uid,configuration,model,effort,source_id)
		VALUES($1,$2,'account','','model','high','source')`, sessionID, conversationID)
	wantEntry := []byte("version-10 event retained by adoption")
	const sequence = 1000000
	migrationExec(t, store.DB(), `INSERT INTO sampler_event(session_id, sequence, entry) VALUES($1, $2, $3)`,
		sessionID, sequence, wantEntry)
	migrationExec(t, store.DB(), `INSERT INTO sampler_export_checkpoint(session_id, destination, cursor)
		VALUES($1, 'retained-destination', 73)`, sessionID)

	// Exercise the landed version-10 adoption and index cleanup independently
	// of version 13's intentional staging reset.
	migrationExec(t, store.DB(), `CREATE INDEX sampler_event_sequence_idx ON sampler_event(session_id, sequence)`)
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id >= 11`)
	migrationExec(t, store.DB(), `INSERT INTO central_sampler_goose_version(version_id, is_applied)
		VALUES (8, true), (9, true), (10, true)`)
	provider, err := schema.Provider(store.DB())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.UpTo(t.Context(), 12); err != nil {
		t.Fatalf("Migrate version-10 database: %v", err)
	}

	for _, version := range []int{11, 12} {
		if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version
			WHERE version_id=$1 AND is_applied`, version); got != 1 {
			t.Fatalf("adopted migration %d history rows = %d, want 1", version, got)
		}
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM pg_indexes
		WHERE schemaname=current_schema() AND indexname='sampler_event_sequence_idx'`); got != 0 {
		t.Fatalf("adopted redundant event index count = %d, want 0", got)
	}
	assertSamplerEventPrimaryKey(t, store.DB())

	var gotEntry []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT entry FROM sampler_event
		WHERE session_id=$1 AND sequence=$2`, sessionID, sequence).Scan(&gotEntry); err != nil {
		t.Fatal("read retained version-10 event:", err)
	}
	if !bytes.Equal(gotEntry, wantEntry) {
		t.Fatalf("adopted event entry = %q, want %q", gotEntry, wantEntry)
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_export_checkpoint
		WHERE session_id=$1 AND destination='retained-destination' AND cursor=73`, sessionID); got != 1 {
		t.Fatalf("retained export checkpoint rows = %d, want 1", got)
	}
}

func assertSamplerEventPrimaryKey(t *testing.T, db *sql.DB) {
	t.Helper()
	if got := migrationCount(t, db, `SELECT count(*) FROM pg_constraint
		WHERE conrelid='sampler_event'::regclass AND contype='p'
		AND pg_get_constraintdef(oid)='PRIMARY KEY (session_id, sequence)'`); got != 1 {
		t.Fatalf("sampler_event composite primary key constraints = %d, want 1", got)
	}
}

func TestSamplerBaselineRejectsUnmarkedDatabase(t *testing.T) {
	store := newTestStore(t)
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id >= 11`)
	if err := Migrate(t.Context(), store.DB()); err == nil || !strings.Contains(err.Error(), "unrecognized objects") {
		t.Fatalf("Migrate error = %v, want unmarked schema rejection", err)
	}
}

func TestSamplerBaselineConcurrentFreshCreation(t *testing.T) {
	store := newTestStore(t)
	migrationExec(t, store.DB(), `DROP SCHEMA public CASCADE; CREATE SCHEMA public`)
	var workers sync.WaitGroup
	errors := make(chan error, 2)
	for range 2 {
		workers.Add(1)
		go func() { defer workers.Done(); errors <- Migrate(t.Context(), store.DB()) }()
	}
	workers.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal("concurrent fresh baseline:", err)
		}
	}
	if got := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version WHERE version_id=11 AND is_applied`); got != 1 {
		t.Fatalf("fresh baseline history rows = %d, want 1", got)
	}
}
