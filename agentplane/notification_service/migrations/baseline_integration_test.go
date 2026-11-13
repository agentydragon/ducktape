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

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
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

func seedSubscription(t *testing.T, db *sql.DB, sessionID string) (store.Owner, store.Subscription) {
	t.Helper()
	owner := store.Owner{Identity: model.Identity{Name: "baseline-identity", UID: "baseline-identity-uid"}, SandboxUID: "adoption-uid"}
	subscription, err := newStore(db).Subscribe(t.Context(), owner, model.Subscribe{
		DestinationRef: model.DestinationRef{Namespace: "notifications-test", Name: "adoption", UID: owner.SandboxUID},
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
	inboxes, err := newStore(db).ListInboxes(t.Context(), owner)
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

func TestIdentityActionActorMigrationPreservesExistingRowsAndLegacyWriters(t *testing.T) {
	db := cloneDatabase(t)
	resetSchema(t, db)
	previous := fstest.MapFS{}
	files, err := scripts.ReadDir("notifications")
	if err != nil {
		t.Fatal(err)
	}
	for _, file := range files {
		if file.Name() >= "00018_" {
			continue
		}
		data, err := scripts.ReadFile("notifications/" + file.Name())
		if err != nil {
			t.Fatal(err)
		}
		previous[file.Name()] = &fstest.MapFile{Data: data}
	}
	provider, err := goose.NewProvider(goose.DialectPostgres, db, previous,
		goose.WithTableName("notification_service_goose_version"), goose.WithDisableGlobalRegistry(true))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := provider.Up(t.Context()); err != nil {
		t.Fatal(err)
	}
	_, subscription := seedSubscription(t, db, "legacy-action-actors")
	insert := func(cursor int, kind, namespace, name, issuer, subject any) {
		t.Helper()
		_, err := db.ExecContext(t.Context(), `INSERT INTO entry
			(inbox_id, cursor, created_at, identity_provider, actions_request_id, actions_sequence,
			 action_state, action_at, action_actor_kind, action_actor_namespace, action_actor_name,
			 action_actor_issuer, action_actor_subject)
			VALUES ($1, $2, now(), 'actions', $3, $2, 2, now(), $4, $5, $6, $7, $8)`,
			subscription.InboxID, cursor, uuid.New(), kind, namespace, name, issuer, subject)
		if err != nil {
			t.Fatal(err)
		}
	}
	insert(1, "caller", "legacy-namespace", "legacy-account", nil, nil)
	insert(2, "operator", nil, nil, "legacy-issuer", "legacy-subject")
	insert(3, nil, nil, nil, nil, nil)
	retained := func() string {
		t.Helper()
		var snapshot string
		if err := db.QueryRowContext(t.Context(), `SELECT jsonb_agg(
			to_jsonb(e) - 'action_actor_identity_name' - 'action_actor_identity_uid' ORDER BY cursor)::text
			FROM entry e WHERE inbox_id = $1 AND cursor <= 3`, subscription.InboxID).Scan(&snapshot); err != nil {
			t.Fatal(err)
		}
		return snapshot
	}
	before := retained()
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	assertApplied(t, db, 18)
	if after := retained(); after != before {
		t.Fatal("Identity actor migration changed retained ServiceAccount/operator events")
	}
	// A previous writer still omits the new nullable columns after the migration.
	insert(4, "caller", "legacy-namespace", "legacy-account", nil, nil)
	if _, err := provider.Up(t.Context()); err != nil {
		t.Fatalf("previous binary migration provider: %v", err)
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO entry
		(inbox_id, cursor, created_at, identity_provider, actions_request_id, actions_sequence,
		 action_state, action_at, action_actor_kind, action_actor_identity_name, action_actor_identity_uid)
		VALUES ($1, 5, now(), 'actions', $2, 1, 2, now(), 'identity', 'requester', 'original-identity-uid')`,
		subscription.InboxID, uuid.New()); err != nil {
		t.Fatalf("new Identity actor writer: %v", err)
	}
	if err := Up(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	if after := retained(); after != before {
		t.Fatal("repeat migration changed retained events")
	}
}
