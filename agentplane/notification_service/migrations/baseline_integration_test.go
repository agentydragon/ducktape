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
	os.Exit(postgres.RunIntegrationTests(
		migrations.Up,
		func(database *postgres.Database) int {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
			defer cancel()
			if err := database.PrepareCloneTemplate(ctx); err != nil {
				fmt.Fprintln(os.Stderr, "prepare migrated PostgreSQL clone template:", err)
				return 1
			}
			integrationDatabase = database
			return m.Run()
		},
	))
}

func cloneDatabase(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	db.SetMaxOpenConns(1)
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func TestFreshBaselineSupportsNotificationStoreWritesAndReads(t *testing.T) {
	db := cloneDatabase(t)
	ctx := t.Context()
	owner := model.ServiceAccount{Namespace: "notifications-test", Name: "fresh"}
	creation := model.Subscribe{
		DestinationRef: model.DestinationRef{Namespace: owner.Namespace, Name: owner.Name, UID: "fresh-uid"},
		SessionID:      "fresh-session",
		IdempotencyKey: "fresh-subscription",
		Source:         model.ActionsSource{RequestID: uuid.New()},
		LifetimeDays:   7,
	}
	service := store.New(db, store.Quotas{InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000})
	subscription, err := service.Subscribe(ctx, owner, creation, nil)
	if err != nil {
		t.Fatal("create subscription on fresh schema:", err)
	}
	if subscription.ID == uuid.Nil || subscription.InboxID == uuid.Nil || subscription.Version != 1 {
		t.Fatalf("subscription = %+v, want persisted version 1", subscription)
	}
	inboxes, err := service.ListInboxes(ctx, store.Owner{Account: owner, SandboxUID: "fresh-uid"})
	if err != nil {
		t.Fatal("list inboxes on fresh schema:", err)
	}
	if len(inboxes) != 1 || inboxes[0].ID != subscription.InboxID {
		t.Fatalf("inboxes = %+v, want the subscription inbox %s", inboxes, subscription.InboxID)
	}
	page, err := service.Read(ctx, store.Owner{Account: owner, SandboxUID: "fresh-uid"}, subscription.InboxID, 0, 10)
	if err != nil {
		t.Fatal("read inbox on fresh schema:", err)
	}
	if page.Inbox.LastCursor != 0 || len(page.Entries) != 0 {
		t.Fatalf("empty inbox read = %+v, want cursor 0 and no entries", page)
	}
}

func TestUpAdoptsCompletedVersion9AndPreservesRowsAndBytes(t *testing.T) {
	db := cloneDatabase(t)
	ctx := t.Context()
	owner := model.ServiceAccount{Namespace: "notifications-test", Name: "adoption"}
	creation := model.Subscribe{
		DestinationRef: model.DestinationRef{Namespace: owner.Namespace, Name: owner.Name, UID: "adoption-uid"},
		SessionID:      "adoption-session",
		IdempotencyKey: "adoption-subscription",
		Source:         model.ActionsSource{RequestID: uuid.New()},
		LifetimeDays:   7,
	}
	service := store.New(db, store.Quotas{InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000})
	subscription, err := service.Subscribe(ctx, owner, creation, nil)
	if err != nil {
		t.Fatal("seed adoption rows:", err)
	}
	if err := createAlembicHistory(ctx, db); err != nil {
		t.Fatal("seed Alembic history:", err)
	}
	event := model.GitHubEvent{AppID: 1, DeliveryID: uuid.New(), RepositoryID: 2, Event: model.EventPullRequest}
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	var lockedInbox uuid.UUID
	if err := tx.QueryRowContext(ctx, `SELECT id FROM inbox WHERE id = $1 FOR UPDATE`, subscription.InboxID).Scan(&lockedInbox); err != nil {
		_ = tx.Rollback()
		t.Fatal(err)
	}
	if err := service.AppendEvent(ctx, tx, subscription.InboxID, subscription.ID, event, json.RawMessage(`{"action":"opened","pull_request":{"number":7}}`)); err != nil {
		_ = tx.Rollback()
		t.Fatal("seed entry row:", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	commandID := uuid.New()
	runnerEntry := []byte{0x0a, 0x05, 0x08, 0x2a, 0x10, 0x01, 0xff, 0x00}
	if _, err := db.ExecContext(ctx, `
		INSERT INTO notice (inbox_id, command_id, through_cursor, text, attempted, admitted, confirmed, runner_cursor, runner_entry)
		VALUES ($1, $2, 1, 'preserved notice', true, true, false, 42, $3)`, subscription.InboxID, commandID, runnerEntry); err != nil {
		t.Fatal("seed notice receipt bytes:", err)
	}
	creationBefore := readBytes(t, db, `SELECT creation_proto FROM subscription WHERE id = $1`, subscription.ID)
	runnerBefore := readBytes(t, db, `SELECT runner_entry FROM notice WHERE inbox_id = $1`, subscription.InboxID)
	if string(runnerBefore) != string(runnerEntry) {
		t.Fatalf("seeded runner entry = %x, want %x", runnerBefore, runnerEntry)
	}
	if err := markVersion9Complete(ctx, db); err != nil {
		t.Fatal("mark completed version 9 fixture:", err)
	}
	historyBefore := readHistoryRows(t, db)

	// This durable fixture starts from the current baseline and synthesizes the version-9 history and completion
	// marker. The separate staging-dump parity experiment checks the actual version-9 catalog shape.
	if err := migrations.Up(ctx, db); err != nil {
		t.Fatal("adopt completed version 9 schema:", err)
	}

	if got := readBytes(t, db, `SELECT creation_proto FROM subscription WHERE id = $1`, subscription.ID); string(got) != string(creationBefore) {
		t.Fatalf("subscription creation bytes changed: got %x, want %x", got, creationBefore)
	}
	if got := readBytes(t, db, `SELECT runner_entry FROM notice WHERE inbox_id = $1`, subscription.InboxID); string(got) != string(runnerBefore) {
		t.Fatalf("runner entry bytes changed: got %x, want %x", got, runnerBefore)
	}
	var inboxRows, subscriptionRows, noticeRows int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM inbox WHERE id = $1`, subscription.InboxID).Scan(&inboxRows); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM subscription WHERE id = $1`, subscription.ID).Scan(&subscriptionRows); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM notice WHERE inbox_id = $1 AND command_id = $2`, subscription.InboxID, commandID).Scan(&noticeRows); err != nil {
		t.Fatal(err)
	}
	if inboxRows != 1 || subscriptionRows != 1 || noticeRows != 1 {
		t.Fatalf("preserved rows: inbox=%d subscription=%d notice=%d, want 1 each", inboxRows, subscriptionRows, noticeRows)
	}
	page, err := service.Read(ctx, store.Owner{Account: owner, SandboxUID: "adoption-uid"}, subscription.InboxID, 0, 10)
	if err != nil {
		t.Fatal("read adopted inbox:", err)
	}
	if len(page.Entries) != 1 || page.Entries[0].Cursor != 1 || page.Entries[0].Event != event {
		t.Fatalf("adopted entries = %+v, want the seeded GitHub event", page.Entries)
	}
	var markerExists bool
	if err := db.QueryRowContext(ctx, `SELECT to_regclass('notification_storage_cutover') IS NOT NULL`).Scan(&markerExists); err != nil {
		t.Fatal(err)
	}
	if markerExists {
		t.Fatal("completed cutover marker remains after adoption")
	}
	var alembicVersion string
	if err := db.QueryRowContext(ctx, `SELECT version_num FROM alembic_version`).Scan(&alembicVersion); err != nil {
		t.Fatal("read preserved Alembic history:", err)
	}
	if alembicVersion != "notification-v9" {
		t.Fatalf("Alembic version = %q, want notification-v9", alembicVersion)
	}
	historyAfter := readHistoryRows(t, db)
	var priorHistoryAfter []gooseVersionRow
	for _, row := range historyAfter {
		if row.versionID != 10 {
			priorHistoryAfter = append(priorHistoryAfter, row)
		}
	}
	if !reflect.DeepEqual(priorHistoryAfter, historyBefore) {
		t.Fatalf("adoption changed prior Goose history: before %+v, after %+v", historyBefore, priorHistoryAfter)
	}
	assertAppliedVersions(t, db, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
}

func TestUpIsRepeatable(t *testing.T) {
	db := cloneDatabase(t)
	ctx := t.Context()
	before := readHistoryRows(t, db)
	if err := migrations.Up(ctx, db); err != nil {
		t.Fatal("repeat migration Up:", err)
	}
	if after := readHistoryRows(t, db); !reflect.DeepEqual(after, before) {
		t.Fatalf("Goose history changed on repeat Up: before %v, after %v", before, after)
	}
}

func TestConcurrentUpSerializesFreshBaselineCreation(t *testing.T) {
	db := cloneDatabase(t)
	resetToFreshGooseHistory(t, db)
	var wg sync.WaitGroup
	errs := make(chan error, 2)
	start := make(chan struct{})
	for range 2 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			errs <- migrations.Up(t.Context(), db)
		}()
	}
	close(start)
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal("concurrent migration Up:", err)
		}
	}
	assertAppliedVersions(t, db, 10)
	var inboxExists bool
	if err := db.QueryRowContext(t.Context(), `SELECT to_regclass('inbox') IS NOT NULL`).Scan(&inboxExists); err != nil {
		t.Fatal(err)
	}
	if !inboxExists {
		t.Fatal("concurrent Up did not create the application schema")
	}
}

func TestUpRejectsOlderAndPartialSchemas(t *testing.T) {
	t.Run("completed marker with history only through version 8", func(t *testing.T) {
		db := cloneDatabase(t)
		seedVersionHistory(t, db, 8)
		if err := createCompleteMarker(t, db); err != nil {
			t.Fatal(err)
		}
		err := migrations.Up(t.Context(), db)
		if err == nil || !strings.Contains(err.Error(), "version 9") {
			t.Fatalf("Up error = %v, want an incomplete version-9 rejection", err)
		}
		assertMarkerStillExists(t, db)
	})

	t.Run("completed marker with an incomplete native schema", func(t *testing.T) {
		db := cloneDatabase(t)
		seedVersionHistory(t, db, 9)
		if err := createCompleteMarker(t, db); err != nil {
			t.Fatal(err)
		}
		if _, err := db.ExecContext(t.Context(), `ALTER TABLE entry DROP COLUMN action_state CASCADE`); err != nil {
			t.Fatal("make partial native schema:", err)
		}
		err := migrations.Up(t.Context(), db)
		if err == nil || !strings.Contains(err.Error(), "native schema is incomplete") {
			t.Fatalf("Up error = %v, want incomplete native schema rejection", err)
		}
		assertMarkerStillExists(t, db)
	})

	t.Run("version 9 schema without cutover marker", func(t *testing.T) {
		db := cloneDatabase(t)
		seedVersionHistory(t, db, 9)
		err := migrations.Up(t.Context(), db)
		if err == nil || !strings.Contains(err.Error(), "not fresh") {
			t.Fatalf("Up error = %v, want rejection of an unmarked existing schema", err)
		}
	})

	t.Run("Alembic history is not a fresh database", func(t *testing.T) {
		db := cloneDatabase(t)
		resetToFreshGooseHistory(t, db)
		if err := createAlembicHistory(t.Context(), db); err != nil {
			t.Fatal("seed Alembic history:", err)
		}
		err := migrations.Up(t.Context(), db)
		if err == nil || !strings.Contains(err.Error(), "no completed migration 9") {
			t.Fatalf("Up error = %v, want rejection of preexisting Alembic metadata", err)
		}
	})
}

func markVersion9Complete(ctx context.Context, db *sql.DB) error {
	if err := rewriteVersionHistory(ctx, db, 9); err != nil {
		return err
	}
	return createCompleteMarkerContext(ctx, db)
}

func readBytes(t *testing.T, db *sql.DB, query string, args ...any) []byte {
	t.Helper()
	var value []byte
	if err := db.QueryRowContext(t.Context(), query, args...).Scan(&value); err != nil {
		t.Fatal(err)
	}
	return value
}

func seedVersionHistory(t *testing.T, db *sql.DB, version int) {
	t.Helper()
	if err := rewriteVersionHistory(t.Context(), db, version); err != nil {
		t.Fatal(err)
	}
}

func rewriteVersionHistory(ctx context.Context, db *sql.DB, version int) error {
	if _, err := db.ExecContext(ctx, `DELETE FROM notification_service_goose_version`); err != nil {
		return err
	}
	for n := 0; n <= version; n++ {
		if _, err := db.ExecContext(ctx, `INSERT INTO notification_service_goose_version (version_id, is_applied) VALUES ($1, true)`, n); err != nil {
			return err
		}
	}
	return nil
}

func createCompleteMarker(t *testing.T, db *sql.DB) error {
	t.Helper()
	return createCompleteMarkerContext(t.Context(), db)
}

func createCompleteMarkerContext(ctx context.Context, db *sql.DB) error {
	if _, err := db.ExecContext(ctx, `CREATE TABLE notification_storage_cutover (
		singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
		phase varchar NOT NULL CHECK (phase IN ('expanded', 'fenced', 'complete'))
	)`); err != nil {
		return err
	}
	_, err := db.ExecContext(ctx, `INSERT INTO notification_storage_cutover (singleton, phase) VALUES (true, 'complete')`)
	return err
}

func createAlembicHistory(ctx context.Context, db *sql.DB) error {
	if _, err := db.ExecContext(ctx, `CREATE TABLE alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY)`); err != nil {
		return err
	}
	_, err := db.ExecContext(ctx, `INSERT INTO alembic_version (version_num) VALUES ('notification-v9')`)
	return err
}

func assertMarkerStillExists(t *testing.T, db *sql.DB) {
	t.Helper()
	var exists bool
	if err := db.QueryRowContext(t.Context(), `SELECT to_regclass('notification_storage_cutover') IS NOT NULL`).Scan(&exists); err != nil {
		t.Fatal(err)
	}
	if !exists {
		t.Fatal("rejected schema lost its cutover marker")
	}
}

func assertAppliedVersions(t *testing.T, db *sql.DB, versions ...int) {
	t.Helper()
	history := readHistory(t, db)
	for _, version := range versions {
		if history[int64(version)] != 1 {
			t.Fatalf("Goose version %d has %d latest applied rows, history=%v", version, history[int64(version)], history)
		}
	}
}

func readHistory(t *testing.T, db *sql.DB) map[int64]int {
	t.Helper()
	history := make(map[int64]int)
	for _, row := range readHistoryRows(t, db) {
		if row.applied {
			history[row.versionID]++
		}
	}
	return history
}

type gooseVersionRow struct {
	id        int64
	versionID int64
	applied   bool
	timestamp time.Time
}

func readHistoryRows(t *testing.T, db *sql.DB) []gooseVersionRow {
	t.Helper()
	rows, err := db.QueryContext(t.Context(), `
		SELECT id, version_id, is_applied, tstamp FROM notification_service_goose_version ORDER BY id`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var history []gooseVersionRow
	for rows.Next() {
		var row gooseVersionRow
		if err := rows.Scan(&row.id, &row.versionID, &row.applied, &row.timestamp); err != nil {
			t.Fatal(err)
		}
		history = append(history, row)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return history
}

func resetToFreshGooseHistory(t *testing.T, db *sql.DB) {
	t.Helper()
	db.SetMaxOpenConns(4)
	if _, err := db.ExecContext(t.Context(), `DROP SCHEMA public CASCADE; CREATE SCHEMA public`); err != nil {
		t.Fatal("reset clone schema:", err)
	}
	_, err := db.ExecContext(t.Context(), `CREATE TABLE notification_service_goose_version (
		id bigserial PRIMARY KEY,
		version_id bigint NOT NULL,
		is_applied boolean NOT NULL,
		tstamp timestamp NOT NULL DEFAULT now()
	)`)
	if err != nil {
		t.Fatal("create empty Goose history:", err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO notification_service_goose_version (version_id, is_applied) VALUES (0, true)`); err != nil {
		t.Fatal("create Goose bootstrap version:", err)
	}
}
