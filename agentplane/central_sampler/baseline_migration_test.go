package centralsampler

import (
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

func TestSamplerBaselineRejectsUnmarkedDatabase(t *testing.T) {
	store := newTestStore(t)
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id=11`)
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
