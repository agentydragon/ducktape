package store

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

var creationContractProofs = []string{
	"subscription_creation_storage_pair",
	"subscription_source_provider",
	"subscription_typed_source_state",
	"subscription_creation_typed_required",
}

func TestSubscriptionCreationContractFreshDatabase(t *testing.T) {
	g := newIsolatedFixture(t, defaultQuotas())
	if got := countQuery(t, t.Context(), g.db, `SELECT count(*) FROM subscription`); got != 0 {
		t.Fatalf("fresh database has %d subscriptions, want none", got)
	}
	assertColumnExists(t, g.db, "subscription", "creation", false)
	assertColumnExists(t, g.db, "subscription", "creation_proto", true)
	assertColumnExists(t, g.db, "subscription", "source_provider", true)
	assertColumnNullability(t, g.db, "subscription", "creation_proto", false)
	assertColumnNullability(t, g.db, "subscription", "source_provider", false)
	assertTableExists(t, g.db, "notification_storage_backfill_progress", false)
	assertCreationContractConstraints(t, g.db)
}

func TestSubscriptionCreationContractRefusesPopulatedDatabaseWithoutProofsThenRetryPreservesTypedData(t *testing.T) {
	t.Run("refuses without partial DDL", func(t *testing.T) {
		g := newCreationContractFixture(t, defaultQuotas())
		creation := body("creation-contract-refusal")
		subscription := g.subscribe(t, creation)
		actionSource := creation.Source.(model.ActionsSource)
		legacyJSON, err := json.Marshal(struct {
			DestinationRef model.DestinationRef `json:"destination_ref"`
			SessionID      string               `json:"session_id"`
			IdempotencyKey string               `json:"idempotency_key"`
			Source         struct {
				Provider      string    `json:"provider"`
				RequestID     uuid.UUID `json:"request_id"`
				AfterSequence int64     `json:"after_sequence"`
			} `json:"source"`
			LifetimeDays int64 `json:"lifetime_days"`
		}{
			DestinationRef: creation.DestinationRef, SessionID: creation.SessionID, IdempotencyKey: creation.IdempotencyKey,
			Source: struct {
				Provider      string    `json:"provider"`
				RequestID     uuid.UUID `json:"request_id"`
				AfterSequence int64     `json:"after_sequence"`
			}{Provider: "actions", RequestID: actionSource.RequestID, AfterSequence: actionSource.AfterSequence},
			LifetimeDays: creation.LifetimeDays,
		})
		if err != nil {
			t.Fatal(err)
		}
		protoBefore, providerBefore := retainLegacyCreationCopy(t, g.db, subscription.ID, legacyJSON)
		for _, constraint := range creationContractProofs {
			if _, err := g.db.ExecContext(t.Context(), `ALTER TABLE subscription VALIDATE CONSTRAINT `+constraint); err != nil {
				t.Fatalf("validate %s: %v", constraint, err)
			}
		}
		if _, err := g.db.ExecContext(t.Context(), `ALTER TABLE subscription DROP CONSTRAINT subscription_creation_storage_pair`); err != nil {
			t.Fatal(err)
		}

		err = migrations.Up(t.Context(), g.db)
		if err == nil || !strings.Contains(err.Error(), "requires all four validated typed-storage constraints") {
			t.Fatalf("contract migration error = %v, want populated database refusal for missing proof", err)
		}
		assertCreationContractExpansionRetained(t, g.db, subscription.ID, legacyJSON, protoBefore, providerBefore, true, 0)
		assertLatestGooseVersion(t, g.db, 17)

		if _, err := g.db.ExecContext(t.Context(), `ALTER TABLE subscription ADD CONSTRAINT subscription_creation_storage_pair
			CHECK ((creation_proto IS NULL) = (source_provider IS NULL)) NOT VALID`); err != nil {
			t.Fatal(err)
		}
		err = migrations.Up(t.Context(), g.db)
		if err == nil || !strings.Contains(err.Error(), "all four typed-storage constraints to be validated") {
			t.Fatalf("contract migration error = %v, want refusal for unvalidated proof", err)
		}
		assertCreationContractExpansionRetained(t, g.db, subscription.ID, legacyJSON, protoBefore, providerBefore, false, 1)
		assertLatestGooseVersion(t, g.db, 17)

		if _, err := g.db.ExecContext(t.Context(), `ALTER TABLE subscription VALIDATE CONSTRAINT subscription_creation_storage_pair`); err != nil {
			t.Fatal(err)
		}

		if err := migrations.Up(t.Context(), g.db); err != nil {
			t.Fatalf("apply contract after validated proofs: %v", err)
		}
		assertGooseVersionApplied(t, g.db, 18)
		assertColumnExists(t, g.db, "subscription", "creation", false)
		assertColumnExists(t, g.db, "subscription", "creation_proto", true)
		assertColumnExists(t, g.db, "subscription", "source_provider", true)
		assertColumnNullability(t, g.db, "subscription", "creation_proto", false)
		assertColumnNullability(t, g.db, "subscription", "source_provider", false)
		assertTableExists(t, g.db, "notification_storage_backfill_progress", false)
		assertCreationContractConstraints(t, g.db)

		var protoAfter []byte
		var providerAfter string
		if err := g.db.QueryRowContext(t.Context(), `SELECT creation_proto, source_provider FROM subscription WHERE id = $1`, subscription.ID).
			Scan(&protoAfter, &providerAfter); err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(protoAfter, protoBefore) || providerAfter != providerBefore {
			t.Fatalf("typed values changed during contract: provider %q (was %q), protobuf bytes equal %t",
				providerAfter, providerBefore, bytes.Equal(protoAfter, protoBefore))
		}
		stored, err := g.GetSubscription(t.Context(), g.incarnation(), subscription.ID)
		if err != nil || stored.ID != subscription.ID || !model.SourcesEqual(stored.Source, creation.Source) {
			t.Fatalf("typed-only read = %+v, %v; want original subscription source %+v", stored, err, creation.Source)
		}
		if replay, err := g.Subscribe(t.Context(), g.owner, creation, nil); err != nil || replay.ID != subscription.ID {
			t.Fatalf("idempotent replay = %+v, %v; want %s", replay, err, subscription.ID)
		}
		if got := countQuery(t, t.Context(), g.db, `SELECT count(*) FROM subscription WHERE inbox_id = $1`, subscription.InboxID); got != 1 {
			t.Fatalf("rows after replay = %d, want one", got)
		}
		_, err = g.db.ExecContext(t.Context(), `UPDATE subscription SET source_provider = 'unknown' WHERE id = $1`, subscription.ID)
		requireConstraint(t, err, "23514", "subscription_source_provider")
		_, err = g.db.ExecContext(t.Context(), `UPDATE subscription SET actions_after_sequence = NULL WHERE id = $1`, subscription.ID)
		requireConstraint(t, err, "23514", "subscription_typed_source_state")
		if err := migrations.Up(t.Context(), g.db); err != nil {
			t.Fatalf("repeat full migration: %v", err)
		}
	})
}

func newCreationContractFixture(t *testing.T, quotas Quotas) fixture {
	t.Helper()
	db, err := sql.Open("postgres", integrationDatabase.Clone(t))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.ExecContext(t.Context(), `DROP SCHEMA public CASCADE; CREATE SCHEMA public`); err != nil {
		t.Fatal(err)
	}
	if err := migrations.UpTo(t.Context(), db, 17); err != nil {
		t.Fatalf("apply migrations through migration 00017 destination contract: %v", err)
	}
	return fixtureForDatabase(t, db, quotas)
}

func retainLegacyCreationCopy(t *testing.T, db *sql.DB, id uuid.UUID, legacyJSON []byte) ([]byte, string) {
	t.Helper()
	var protoBefore []byte
	var providerBefore string
	if err := db.QueryRowContext(t.Context(), `SELECT creation_proto, source_provider FROM subscription WHERE id = $1`, id).
		Scan(&protoBefore, &providerBefore); err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(t.Context(), `UPDATE subscription SET creation = $2::jsonb WHERE id = $1`, id, string(legacyJSON)); err != nil {
		t.Fatal(err)
	}
	return protoBefore, providerBefore
}

func assertLatestGooseVersion(t *testing.T, db *sql.DB, want int64) {
	t.Helper()
	var got int64
	if err := db.QueryRowContext(t.Context(), `SELECT max(version_id) FROM notification_service_goose_version`).Scan(&got); err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("latest Goose migration = %d, want %d", got, want)
	}
}

func assertGooseVersionApplied(t *testing.T, db *sql.DB, version int64) {
	t.Helper()
	var applied bool
	if err := db.QueryRowContext(t.Context(), `SELECT EXISTS (
		SELECT 1 FROM notification_service_goose_version WHERE version_id = $1 AND is_applied
	)`, version).Scan(&applied); err != nil {
		t.Fatal(err)
	}
	if !applied {
		t.Fatalf("Goose migration %d is not marked applied", version)
	}
}

func assertCreationContractExpansionRetained(
	t *testing.T, db *sql.DB, id uuid.UUID, legacyJSON, protoBefore []byte, providerBefore string, wantMissingStoragePair bool, unvalidatedConstraints int,
) {
	t.Helper()
	assertColumnExists(t, db, "subscription", "creation", true)
	assertColumnExists(t, db, "subscription", "creation_proto", true)
	assertColumnExists(t, db, "subscription", "source_provider", true)
	assertColumnNullability(t, db, "subscription", "creation_proto", true)
	assertColumnNullability(t, db, "subscription", "source_provider", true)
	assertTableExists(t, db, "notification_storage_backfill_progress", true)
	assertColumnExists(t, db, "notification_storage_backfill_progress", "subscription_creation_last_id", true)
	assertColumnExists(t, db, "notification_storage_backfill_progress", "subscription_creation_complete", true)
	var protoAfter []byte
	var providerAfter string
	var creationMatches bool
	if err := db.QueryRowContext(t.Context(), `SELECT creation_proto, source_provider, creation = $2::jsonb
		FROM subscription WHERE id = $1`, id, string(legacyJSON)).Scan(&protoAfter, &providerAfter, &creationMatches); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(protoAfter, protoBefore) || providerAfter != providerBefore || !creationMatches {
		t.Fatalf("refused contract changed row: provider %q (was %q), protobuf bytes equal %t, creation preserved %t",
			providerAfter, providerBefore, bytes.Equal(protoAfter, protoBefore), creationMatches)
	}
	var unvalidatedCount int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM pg_constraint
		WHERE conrelid = 'subscription'::regclass AND conname = ANY($1::text[]) AND NOT convalidated`,
		pq.Array(creationContractProofs)).Scan(&unvalidatedCount); err != nil {
		t.Fatal(err)
	}
	if unvalidatedCount != unvalidatedConstraints {
		t.Fatalf("constraints after refusal = %d unvalidated, want %d", unvalidatedCount, unvalidatedConstraints)
	}
	var pairCount int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*) FROM pg_constraint
		WHERE conrelid = 'subscription'::regclass AND conname = 'subscription_creation_storage_pair'`).Scan(&pairCount); err != nil {
		t.Fatal(err)
	}
	wantPairCount := 1
	if wantMissingStoragePair {
		wantPairCount = 0
	}
	if pairCount != wantPairCount {
		t.Fatalf("storage-pair constraint count after refusal = %d, want %d", pairCount, wantPairCount)
	}
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

func assertTableExists(t *testing.T, db *sql.DB, table string, want bool) {
	t.Helper()
	var exists bool
	if err := db.QueryRowContext(t.Context(), `SELECT to_regclass($1) IS NOT NULL`, table).Scan(&exists); err != nil {
		t.Fatal(err)
	}
	if exists != want {
		t.Fatalf("table %s exists = %t, want %t", table, exists, want)
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
