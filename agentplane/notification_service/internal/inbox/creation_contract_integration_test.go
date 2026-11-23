package inbox

import (
	"database/sql"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"

	"github.com/lib/pq"
)

func TestSubscriptionCreationUsesTypedStorageAndEnforcesConstraints(t *testing.T) {
	g := newIsolatedFixture(t, defaultQuotas())
	if got := postgres.Count(t, g.db, `SELECT count(*) FROM subscription`); got != 0 {
		t.Fatalf("fresh database has %d subscriptions, want none", got)
	}
	assertColumnExists(t, g.db, "subscription", "creation_proto", true)
	assertColumnExists(t, g.db, "subscription", "source_provider", true)
	assertColumnNullability(t, g.db, "subscription", "creation_proto", false)
	assertColumnNullability(t, g.db, "subscription", "source_provider", false)
	assertCreationContractConstraints(t, g.db)

	creation := g.body("typed-creation-final-schema")
	subscription := g.subscribe(t, creation)
	var protoBytes []byte
	var provider string
	if err := g.db.QueryRowContext(t.Context(), `SELECT creation_proto, source_provider FROM subscription WHERE id = $1`, subscription.ID).
		Scan(&protoBytes, &provider); err != nil {
		t.Fatal(err)
	}
	if len(protoBytes) == 0 || provider != "actions" {
		t.Fatalf("stored typed subscription creation has %d protobuf bytes and provider %q", len(protoBytes), provider)
	}
	stored, err := g.GetSubscription(t.Context(), g.incarnation(), subscription.ID)
	if err != nil || stored.ID != subscription.ID || !sourceadapter.SourcesEqual(stored.Source, creation.Source) {
		t.Fatalf("typed-only read = %+v, %v; want original subscription source %+v", stored, err, creation.Source)
	}
	if replay, err := g.Subscribe(t.Context(), g.incarnation(), creation, sourceadapter.ActionsAuthorization{}); err != nil || replay.ID != subscription.ID {
		t.Fatalf("idempotent replay = %+v, %v; want %s", replay, err, subscription.ID)
	}
	if got := postgres.Count(t, g.db, `SELECT count(*) FROM subscription WHERE inbox_id = $1`, subscription.InboxID); got != 1 {
		t.Fatalf("rows after replay = %d, want one", got)
	}
	_, err = g.db.ExecContext(t.Context(), `UPDATE subscription SET source_provider = 'unknown' WHERE id = $1`, subscription.ID)
	requireConstraint(t, err, "23514", "subscription_source_provider")
	_, err = g.db.ExecContext(t.Context(), `UPDATE subscription SET actions_after_sequence = NULL WHERE id = $1`, subscription.ID)
	requireConstraint(t, err, "23514", "subscription_typed_source_state")
}

func assertColumnExists(t *testing.T, db *sql.DB, table, column string, want bool) {
	t.Helper()
	var nullable string
	err := db.QueryRowContext(t.Context(), `SELECT is_nullable FROM information_schema.columns
		WHERE table_schema = current_schema() AND table_name = $1 AND column_name = $2`, table, column).Scan(&nullable)
	if want && err == sql.ErrNoRows {
		t.Fatalf("%s.%s is absent", table, column)
	}
	if !want && err == nil {
		t.Fatalf("%s.%s remains present with is_nullable=%s", table, column, nullable)
	}
	if err != nil && !(err == sql.ErrNoRows && !want) {
		t.Fatal(err)
	}
}

func assertColumnNullability(t *testing.T, db *sql.DB, table, column string, wantNullable bool) {
	t.Helper()
	var nullable string
	if err := db.QueryRowContext(t.Context(), `SELECT is_nullable FROM information_schema.columns
		WHERE table_schema = current_schema() AND table_name = $1 AND column_name = $2`, table, column).Scan(&nullable); err != nil {
		t.Fatal(err)
	}
	want := "NO"
	if wantNullable {
		want = "YES"
	}
	if nullable != want {
		t.Fatalf("%s.%s is_nullable=%s, want %s", table, column, nullable, want)
	}
}

func assertCreationContractConstraints(t *testing.T, db *sql.DB) {
	t.Helper()
	var retained, obsolete int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM pg_constraint
		WHERE conrelid = 'subscription'::regclass AND conname = ANY($1::text[]) AND convalidated`,
		pq.Array([]string{"subscription_source_provider", "subscription_typed_source_state"})).Scan(&retained); err != nil {
		t.Fatal(err)
	}
	if retained != 2 {
		t.Fatalf("contract left %d of 2 validated permanent source constraints", retained)
	}
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM pg_constraint
		WHERE conrelid = 'subscription'::regclass AND conname = ANY($1::text[])`,
		pq.Array([]string{"subscription_creation_storage_pair", "subscription_creation_typed_required"})).Scan(&obsolete); err != nil {
		t.Fatal(err)
	}
	if obsolete != 0 {
		t.Fatalf("contract left %d obsolete creation-storage constraints, want none", obsolete)
	}
}
