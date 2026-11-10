package migrations

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"testing/fstest"
	"time"

	"github.com/pressly/goose/v3"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	_ "github.com/lib/pq"
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(Up, func(database *postgres.Database) int {
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
	}, nil, "test-account-uid")
	if err != nil {
		t.Fatal("seed subscription:", err)
	}
	return owner, subscription
}

func TestFreshBaselineSupportsNotificationStore(t *testing.T) {
	db := cloneDatabase(t)
	owner, subscription := seedSubscription(t, db, "fresh-session")
	inboxes, err := newStore(db).ListInboxes(t.Context(), store.AccountOwner{Account: owner, AccountUID: "test-account-uid", SandboxUID: "adoption-uid"})
	if err != nil || len(inboxes) != 1 || inboxes[0].ID != subscription.InboxID {
		t.Fatalf("fresh inboxes = %+v, error %v", inboxes, err)
	}
	assertApplied(t, db, 15)
	if err := applyBaseline(t.Context(), db); err != nil {
		t.Fatal("previous binary baseline provider on expanded schema:", err)
	}
}

func TestBaselineRejectsUnknownObjectsOnFreshHistory(t *testing.T) {
	db := cloneDatabase(t)
	resetSchema(t, db)
	if _, err := db.ExecContext(t.Context(), `CREATE TABLE retained_user_data (id int)`); err != nil {
		t.Fatal(err)
	}
	err := Up(t.Context(), db)
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
		go func() { defer wg.Done(); <-start; errors <- Up(t.Context(), db) }()
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

// applyBaseline isolates the current baseline's adoption guard from later additive migrations.
func applyBaseline(ctx context.Context, db *sql.DB) error {
	script, err := scripts.ReadFile("notifications/00013_baseline.sql")
	if err != nil {
		return err
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, fstest.MapFS{"00013_baseline.sql": &fstest.MapFile{Data: script}},
		goose.WithTableName("notification_service_goose_version"), goose.WithDisableGlobalRegistry(true))
	if err != nil {
		return err
	}
	_, err = provider.Up(ctx)
	return err
}
