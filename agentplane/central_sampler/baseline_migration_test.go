package centralsampler

import (
	"database/sql"
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
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version WHERE version_id=15 AND is_applied`); count != 1 {
		t.Fatal("baseline not recorded")
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_conversation WHERE id=$1 AND api='responses'`, session.ConversationID); count != 1 {
		t.Fatal("current session was not created")
	}
}

func TestSamplerBaselineAdoptsCompletedRewriteWithoutTouchingData(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	migrationExec(t, store.DB(), `INSERT INTO sampler_export_checkpoint(session_id,destination,cursor) VALUES($1,'fixture-history',73)`, session.ID)
	migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id=15`)
	migrationExec(t, store.DB(), `INSERT INTO central_sampler_goose_version(version_id,is_applied) SELECT generate_series(1,14),true`)
	var relation uint64
	if err := store.DB().QueryRowContext(t.Context(), `SELECT 'sampler_event'::regclass::oid`).Scan(&relation); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := Migrate(t.Context(), store.DB()); err != nil {
			t.Fatal(err)
		}
	}
	got, err := store.GetSession(t.Context(), session.ID, "")
	if err != nil {
		t.Fatal(err)
	}
	if got.ConversationID != session.ConversationID || got.SourceID != session.SourceID || got.Cursor != session.Cursor {
		t.Fatal("adoption changed session identity")
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version WHERE version_id BETWEEN 1 AND 15 AND is_applied`); count != 15 {
		t.Fatal("adoption rewrote Goose history")
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM pg_class WHERE oid=$1 AND relname='sampler_event'`, relation); count != 1 {
		t.Fatal("adoption recreated event table")
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM sampler_export_checkpoint WHERE session_id=$1 AND destination='fixture-history' AND cursor=73`, session.ID); count != 1 {
		t.Fatal("adoption changed export checkpoint")
	}
}

func TestSamplerBaselineRejectsIncompletePredecessors(t *testing.T) {
	for _, mode := range []string{"unmarked", "before_rewrite", "partial_schema"} {
		t.Run(mode, func(t *testing.T) {
			store := newTestStore(t)
			migrationExec(t, store.DB(), `DELETE FROM central_sampler_goose_version WHERE version_id>0`)
			if mode != "unmarked" {
				version := 14
				if mode == "before_rewrite" {
					version = 13
				}
				migrationExec(t, store.DB(), `INSERT INTO central_sampler_goose_version(version_id,is_applied) VALUES($1,true)`, version)
			}
			if mode == "partial_schema" {
				migrationExec(t, store.DB(), `ALTER TABLE sampler_model_request DROP COLUMN source_context_id`)
			}
			if err := Migrate(t.Context(), store.DB()); err == nil {
				t.Fatal("accepted incomplete predecessor")
			}
		})
	}
}

func TestSamplerBaselineConcurrentFreshCreation(t *testing.T) {
	store := newTestStore(t)
	migrationExec(t, store.DB(), `DROP SCHEMA public CASCADE; CREATE SCHEMA public`)
	var workers sync.WaitGroup
	failures := make(chan error, 2)
	for range 2 {
		workers.Add(1)
		go func() { defer workers.Done(); failures <- Migrate(t.Context(), store.DB()) }()
	}
	workers.Wait()
	close(failures)
	for err := range failures {
		if err != nil {
			t.Fatal(err)
		}
	}
	if count := migrationCount(t, store.DB(), `SELECT count(*) FROM central_sampler_goose_version WHERE version_id=15 AND is_applied`); count != 1 {
		t.Fatal("baseline recorded more than once")
	}
}
