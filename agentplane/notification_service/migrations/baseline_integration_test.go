package migrations_test

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(migrations.Up, func(database *postgres.Database) int {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
		defer cancel()
		if err := database.PrepareCloneTemplate(ctx); err != nil {
			fmt.Fprintln(os.Stderr, "prepare migrated PostgreSQL clone template:", err)
			return 1
		}
		integrationDatabase = database
		return m.Run()
	}))
}

func cloneDatabase(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	db.SetMaxOpenConns(4)
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func resetSchema(t *testing.T, db *sql.DB) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), `DROP SCHEMA public CASCADE; CREATE SCHEMA public`); err != nil {
		t.Fatal(err)
	}
}

func markVersion12History(t *testing.T, db *sql.DB) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), `DELETE FROM notification_service_goose_version WHERE version_id = 13`); err != nil {
		t.Fatal(err)
	}
	for version := 10; version <= 12; version++ {
		if _, err := db.ExecContext(t.Context(),
			`INSERT INTO notification_service_goose_version (version_id, is_applied) VALUES ($1, true)`, version); err != nil {
			t.Fatal(err)
		}
	}
}

func newStore(db *sql.DB) *store.Store {
	return store.New(db, store.Quotas{InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000})
}

func seedSubscription(t *testing.T, db *sql.DB, sessionID string) (model.ServiceAccount, store.Subscription) {
	t.Helper()
	owner := model.ServiceAccount{Namespace: "notifications-test", Name: "adoption"}
	subscription, err := newStore(db).Subscribe(t.Context(), owner, model.Subscribe{
		DestinationRef: model.DestinationRef{Namespace: owner.Namespace, Name: owner.Name, UID: "adoption-uid"},
		SessionID:      sessionID, IdempotencyKey: "adoption-subscription",
		Source: model.ActionsSource{RequestID: uuid.New()}, LifetimeDays: 7,
	}, nil)
	if err != nil {
		t.Fatal("seed subscription:", err)
	}
	return owner, subscription
}

func TestFreshBaselineSupportsNotificationStore(t *testing.T) {
	db := cloneDatabase(t)
	owner, subscription := seedSubscription(t, db, "fresh-session")
	inboxes, err := newStore(db).ListInboxes(t.Context(), store.Owner{Account: owner, SandboxUID: "adoption-uid"})
	if err != nil || len(inboxes) != 1 || inboxes[0].ID != subscription.InboxID {
		t.Fatalf("fresh inboxes = %+v, error %v", inboxes, err)
	}
	assertApplied(t, db, 13)
}

func TestAdoptVersion12PreservesStoredDataAndHistory(t *testing.T) {
	db := cloneDatabase(t)
	markVersion12History(t, db)
	owner, subscription := seedSubscription(t, db, "retained-session")
	event := model.GitHubEvent{AppID: 1, DeliveryID: uuid.New(), RepositoryID: 2, Event: model.EventPullRequest}
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	var lockedInbox uuid.UUID
	if err := tx.QueryRowContext(t.Context(), `SELECT id FROM inbox WHERE id = $1 FOR UPDATE`, subscription.InboxID).Scan(&lockedInbox); err != nil {
		_ = tx.Rollback()
		t.Fatal(err)
	}
	if err := newStore(db).AppendEvent(t.Context(), tx, subscription.InboxID, subscription.ID, event,
		json.RawMessage(`{"action":"opened","pull_request":{"number":7}}`)); err != nil {
		_ = tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	runnerEntry := []byte{0x0a, 0x05, 0x08, 0x2a, 0x10, 0x01, 0xff, 0x00}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO notice
		(inbox_id, command_id, through_cursor, text, attempted, admitted, confirmed, runner_cursor, runner_entry)
		VALUES ($1, $2, 1, 'preserved notice', true, true, false, 42, $3)`,
		subscription.InboxID, uuid.New(), runnerEntry); err != nil {
		t.Fatal(err)
	}
	creationBefore := readBytes(t, db, `SELECT creation_proto FROM subscription WHERE id = $1`, subscription.ID)
	entryBefore := readBytes(t, db, `SELECT payload FROM entry WHERE inbox_id = $1`, subscription.InboxID)
	historyBefore := readHistory(t, db)
	var inboxNodeBefore int64
	if err := db.QueryRowContext(t.Context(), `SELECT 'inbox'::regclass::oid`).Scan(&inboxNodeBefore); err != nil {
		t.Fatal(err)
	}
	if err := migrations.Up(t.Context(), db); err != nil {
		t.Fatal("adopt version 12:", err)
	}
	if got := readBytes(t, db, `SELECT creation_proto FROM subscription WHERE id = $1`, subscription.ID); !reflect.DeepEqual(got, creationBefore) {
		t.Fatalf("creation bytes changed: %x -> %x", creationBefore, got)
	}
	if got := readBytes(t, db, `SELECT payload FROM entry WHERE inbox_id = $1`, subscription.InboxID); !reflect.DeepEqual(got, entryBefore) {
		t.Fatalf("entry bytes changed: %x -> %x", entryBefore, got)
	}
	if got := readBytes(t, db, `SELECT runner_entry FROM notice WHERE inbox_id = $1`, subscription.InboxID); !reflect.DeepEqual(got, runnerEntry) {
		t.Fatalf("notice bytes changed: %x -> %x", runnerEntry, got)
	}
	var inboxNodeAfter int64
	if err := db.QueryRowContext(t.Context(), `SELECT 'inbox'::regclass::oid`).Scan(&inboxNodeAfter); err != nil {
		t.Fatal(err)
	}
	if inboxNodeAfter != inboxNodeBefore {
		t.Fatal("adoption recreated the inbox table")
	}
	historyAfter := readHistory(t, db)
	if !reflect.DeepEqual(historyBefore, historyAfter[:len(historyBefore)]) {
		t.Fatal("adoption rewrote prior Goose history")
	}
	assertApplied(t, db, 13)
	page, err := newStore(db).Read(t.Context(), store.Owner{Account: owner, SandboxUID: "adoption-uid"}, subscription.InboxID, 0, 10)
	if err != nil || len(page.Entries) != 1 || page.Entries[0].Event != event {
		t.Fatalf("adopted entries = %+v, error %v", page, err)
	}
}

func TestBaselineRejectsPartialAndInvalidPredecessors(t *testing.T) {
	for _, tc := range []struct {
		name    string
		mutate  func(*testing.T, *sql.DB)
		message string
	}{
		{"version 11", func(t *testing.T, db *sql.DB) {
			_, err := db.ExecContext(t.Context(), `DELETE FROM notification_service_goose_version WHERE version_id = 12`)
			if err != nil {
				t.Fatal(err)
			}
		}, "finish migration 12"},
		{"missing version 11", func(t *testing.T, db *sql.DB) {
			_, err := db.ExecContext(t.Context(), `DELETE FROM notification_service_goose_version WHERE version_id = 11`)
			if err != nil {
				t.Fatal(err)
			}
		}, "finish migration 12"},
		{"missing Session index", func(t *testing.T, db *sql.DB) {
			_, err := db.ExecContext(t.Context(), `DROP INDEX inbox_session_id_unique`)
			if err != nil {
				t.Fatal(err)
			}
		}, "Session inbox schema is incomplete"},
		{"missing content column", func(t *testing.T, db *sql.DB) {
			_, err := db.ExecContext(t.Context(), `ALTER TABLE entry DROP COLUMN message_body`)
			if err != nil {
				t.Fatal(err)
			}
		}, "schema columns are incomplete"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			db := cloneDatabase(t)
			markVersion12History(t, db)
			tc.mutate(t, db)
			err := migrations.Up(t.Context(), db)
			if err == nil || !strings.Contains(err.Error(), tc.message) {
				t.Fatalf("Up error = %v, want %q", err, tc.message)
			}
			for _, row := range readHistory(t, db) {
				if row.version == 13 && row.applied {
					t.Fatal("rejected adoption recorded version 13")
				}
			}
		})
	}
}

func TestBaselineAdoptsCompletePreBaselineHistory(t *testing.T) {
	db := cloneDatabase(t)
	markVersion12History(t, db)
	for version := 1; version <= 9; version++ {
		if _, err := db.ExecContext(t.Context(),
			`INSERT INTO notification_service_goose_version (version_id, is_applied) VALUES ($1, true)`, version); err != nil {
			t.Fatal(err)
		}
	}
	if err := migrations.Up(t.Context(), db); err != nil {
		t.Fatal("adopt complete 1–12 history:", err)
	}
	assertApplied(t, db, 13)
}

func TestBaselineRejectsUnknownObjectsOnFreshHistory(t *testing.T) {
	db := cloneDatabase(t)
	resetSchema(t, db)
	if _, err := db.ExecContext(t.Context(), `CREATE TABLE retained_user_data (id int)`); err != nil {
		t.Fatal(err)
	}
	err := migrations.Up(t.Context(), db)
	if err == nil || !strings.Contains(err.Error(), "unrecognized objects") {
		t.Fatalf("Up error = %v", err)
	}
	var count int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM retained_user_data`).Scan(&count); err != nil {
		t.Fatal(err)
	}
}

func TestConcurrentUpSerializesFreshBaseline(t *testing.T) {
	db := cloneDatabase(t)
	resetSchema(t, db)
	start := make(chan struct{})
	errors := make(chan error, 2)
	var wg sync.WaitGroup
	for range 2 {
		wg.Add(1)
		go func() { defer wg.Done(); <-start; errors <- migrations.Up(t.Context(), db) }()
	}
	close(start)
	wg.Wait()
	close(errors)
	for err := range errors {
		if err != nil {
			t.Fatal(err)
		}
	}
	assertApplied(t, db, 13)
}

func readBytes(t *testing.T, db *sql.DB, query string, args ...any) []byte {
	t.Helper()
	var value []byte
	if err := db.QueryRowContext(t.Context(), query, args...).Scan(&value); err != nil {
		t.Fatal(err)
	}
	return value
}

type gooseRow struct {
	id      int64
	version int64
	applied bool
	stamp   time.Time
}

func readHistory(t *testing.T, db *sql.DB) []gooseRow {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), `SELECT id, version_id, is_applied, tstamp FROM notification_service_goose_version ORDER BY id`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var result []gooseRow
	for rows.Next() {
		var row gooseRow
		if err := rows.Scan(&row.id, &row.version, &row.applied, &row.stamp); err != nil {
			t.Fatal(err)
		}
		result = append(result, row)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return result
}

func assertApplied(t *testing.T, db *sql.DB, version int64) {
	t.Helper()
	for _, row := range readHistory(t, db) {
		if row.version == version && row.applied {
			return
		}
	}
	t.Fatalf("Goose version %d is not applied", version)
}
