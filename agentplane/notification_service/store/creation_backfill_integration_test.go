package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

func allowLegacySubscriptionCreationTestRows(t *testing.T, ctx context.Context, db *sql.DB) {
	t.Helper()
	if _, err := db.ExecContext(ctx, `ALTER TABLE subscription DROP CONSTRAINT subscription_creation_typed_required`); err != nil {
		t.Fatal(err)
	}
}

func TestSubscriptionCreationBackfillProducesTypedReadsAndPreservesIdempotency(t *testing.T) {
	g := newIsolatedGitHubFixture(t)
	actions := body("actions-before-backfill")
	actionSubscription, err := g.Subscribe(t.Context(), g.owner, actions, nil)
	if err != nil {
		t.Fatal(err)
	}
	githubSource := g.source(
		PullRequestSubject{Number: 17},
		EventFilter{Event: EventCheckRun, Actions: []string{"completed", "created", "completed"}},
		EventFilter{Event: EventIssueComment, Actions: []string{"created"}},
	)
	githubCreation := g.creation(githubSource, "github-before-backfill")
	access := g.access()
	githubSubscription, err := g.Subscribe(t.Context(), g.owner, githubCreation, &access)
	if err != nil {
		t.Fatal(err)
	}
	actionsJSON, err := json.Marshal(actions)
	if err != nil {
		t.Fatal(err)
	}
	githubJSON, err := json.Marshal(githubCreation)
	if err != nil {
		t.Fatal(err)
	}
	// Simulate rows written before the typed dual-write release, retaining the original creation as rollback data.
	allowLegacySubscriptionCreationTestRows(t, t.Context(), g.db)
	for _, row := range []struct {
		id   uuid.UUID
		json []byte
	}{
		{actionSubscription.ID, actionsJSON},
		{githubSubscription.ID, githubJSON},
	} {
		if _, err := g.db.ExecContext(t.Context(), `UPDATE subscription
			SET creation = $2::jsonb, creation_proto = NULL, source_provider = NULL WHERE id = $1`, row.id, string(row.json)); err != nil {
			t.Fatal(err)
		}
	}

	if err := BackfillSubscriptionCreations(t.Context(), g.db); err != nil {
		t.Fatal(err)
	}
	for _, row := range []struct {
		id       uuid.UUID
		provider string
		json     []byte
	}{
		{actionSubscription.ID, "actions", actionsJSON},
		{githubSubscription.ID, "github", githubJSON},
	} {
		var provider string
		var typed []byte
		var unchanged bool
		if err := g.db.QueryRowContext(t.Context(), `SELECT source_provider, creation_proto,
			creation = $2::jsonb FROM subscription WHERE id = $1`, row.id, string(row.json)).
			Scan(&provider, &typed, &unchanged); err != nil {
			t.Fatal(err)
		}
		if provider != row.provider || len(typed) == 0 || !unchanged {
			t.Fatalf("subscription %s typed state = (%q, %d bytes, JSON unchanged %t), want provider %q, payload, unchanged JSON",
				row.id, provider, len(typed), unchanged, row.provider)
		}
	}
	for _, row := range []struct {
		id     uuid.UUID
		source Source
	}{
		{actionSubscription.ID, actions.Source},
		{githubSubscription.ID, githubCreation.Source},
	} {
		stored, err := g.GetSubscription(t.Context(), g.incarnation(), row.id)
		if err != nil || !sourcesEqual(stored.Source, row.source) {
			t.Fatalf("typed subscription %s = %+v, %v; want source %+v", row.id, stored.Source, err, row.source)
		}
	}

	// Typed values remain authoritative after conversion, while idempotency continues to compare normalized filter sets.
	if _, err := g.db.ExecContext(t.Context(), `UPDATE subscription SET creation = jsonb_set(creation,
		'{source,repository}', '"changed-retained-json"') WHERE id = $1`, githubSubscription.ID); err != nil {
		t.Fatal(err)
	}
	reordered := g.creation(g.source(
		PullRequestSubject{Number: 17},
		EventFilter{Event: EventIssueComment, Actions: []string{"created", "created"}},
		EventFilter{Event: EventCheckRun, Actions: []string{"created", "completed"}},
	), "github-before-backfill")
	if replay, err := g.Subscribe(t.Context(), g.owner, reordered, &access); err != nil || replay.ID != githubSubscription.ID {
		t.Fatalf("typed GitHub replay with normalized filters = %+v, %v", replay, err)
	}
	changed := reordered
	changed.Source = g.source(PullRequestSubject{Number: 17}, EventFilter{Event: EventCheckRun, Actions: []string{"failed"}})
	if _, err := g.Subscribe(t.Context(), g.owner, changed, &access); !errors.Is(err, ErrIdempotencyKeyReused) {
		t.Fatalf("changed typed creation error = %v, want ErrIdempotencyKeyReused", err)
	}

	_, err = g.db.ExecContext(t.Context(), `UPDATE subscription SET source_provider = NULL WHERE id = $1`, actionSubscription.ID)
	requireConstraint(t, err, "23514", "subscription_creation_storage_pair")
	_, err = g.db.ExecContext(t.Context(), `UPDATE subscription SET source_provider = 'github' WHERE id = $1`, actionSubscription.ID)
	requireConstraint(t, err, "23514", "subscription_typed_source_state")
}

func TestSubscriptionCreationBackfillResumesAndRepairsCompletedCursor(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	allowLegacySubscriptionCreationTestRows(t, ctx, db)
	const rowCount = subscriptionCreationBackfillBatchSize + 1
	inboxID := insertSubscriptionBackfillInbox(t, ctx, db, "test-subscription-resume")
	insertLegacyActionSubscriptions(t, ctx, db, inboxID, "test-subscription-resume", 1, rowCount, rowCount)

	if err := BackfillSubscriptionCreations(ctx, db); err == nil || !strings.Contains(err.Error(), "request_id") {
		t.Fatalf("backfill error = %v, want invalid legacy request_id", err)
	}
	var cursor sql.NullString
	var complete bool
	if err := db.QueryRowContext(ctx, `SELECT subscription_creation_last_id::text, subscription_creation_complete
		FROM notification_storage_backfill_progress WHERE singleton`).Scan(&cursor, &complete); err != nil {
		t.Fatal(err)
	}
	wantCursor := "00000000-0000-0000-0000-000000000200"
	if !cursor.Valid || cursor.String != wantCursor || complete {
		t.Fatalf("backfill progress = (%v, complete %t), want cursor %s and incomplete", cursor, complete, wantCursor)
	}
	if got := countQuery(t, ctx, db, `SELECT count(*) FROM subscription WHERE inbox_id = $1 AND creation_proto IS NOT NULL`, inboxID); got != subscriptionCreationBackfillBatchSize {
		t.Fatalf("committed typed rows after interruption = %d, want %d", got, subscriptionCreationBackfillBatchSize)
	}

	badID := "00000000-0000-0000-0000-000000000201"
	requestID := "12345678-1234-5678-9abc-000000000201"
	if _, err := db.ExecContext(ctx, `UPDATE subscription SET creation = jsonb_set(creation,
		'{source,request_id}', to_jsonb($2::text), true) WHERE id = $1::uuid`, badID, requestID); err != nil {
		t.Fatal(err)
	}
	if err := BackfillSubscriptionCreations(ctx, db); err != nil {
		t.Fatalf("resuming after repairing one legacy row: %v", err)
	}
	if got := countQuery(t, ctx, db, `SELECT count(*) FROM subscription WHERE inbox_id = $1 AND creation_proto IS NULL`, inboxID); got != 0 {
		t.Fatalf("incomplete rows after resume = %d, want 0", got)
	}
	if err := db.QueryRowContext(ctx, `SELECT subscription_creation_complete FROM notification_storage_backfill_progress
		WHERE singleton`).Scan(&complete); err != nil {
		t.Fatal(err)
	}
	if !complete {
		t.Fatal("resumed creation backfill did not finish")
	}

	// A rolled-back old writer can insert a legacy record below a completed cursor; completion validation must
	// reset once, revisit it, and retain the old JSON as the rollback copy.
	insertLegacyActionSubscriptions(t, ctx, db, inboxID, "test-old-writer", 0, 0, -1)
	if err := BackfillSubscriptionCreations(ctx, db); err != nil {
		t.Fatalf("backfill after old-writer insertion below completed cursor: %v", err)
	}
	if got := countQuery(t, ctx, db, `SELECT count(*) FROM subscription WHERE id = '00000000-0000-0000-0000-000000000000'
		AND creation_proto IS NOT NULL AND source_provider = 'actions' AND creation ? 'source'`); got != 1 {
		t.Fatalf("legacy row inserted behind completed cursor was not migrated; matching rows = %d", got)
	}
}

func TestSubscriptionCreationBackfillRejectsInvalidAndPartialRows(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	allowLegacySubscriptionCreationTestRows(t, ctx, db)
	inboxID := insertSubscriptionBackfillInbox(t, ctx, db, "test-subscription-invalid")
	insertLegacyActionSubscriptions(t, ctx, db, inboxID, "test-subscription-invalid", 1, 1, 1)
	if err := BackfillSubscriptionCreations(ctx, db); err == nil || !strings.Contains(err.Error(), "request_id") {
		t.Fatalf("invalid legacy row error = %v, want required request_id", err)
	}
	var nullProto, nullProvider bool
	if err := db.QueryRowContext(ctx, `SELECT creation_proto IS NULL, source_provider IS NULL FROM subscription WHERE inbox_id = $1`, inboxID).
		Scan(&nullProto, &nullProvider); err != nil {
		t.Fatal(err)
	}
	if !nullProto || !nullProvider {
		t.Fatal("invalid legacy row was partially converted")
	}

	fixture := newIsolatedFixture(t, defaultQuotas())
	subscription := fixture.subscribe(t, body("partial-typed-fields"))
	_, err := fixture.db.ExecContext(t.Context(), `UPDATE subscription SET creation_proto = NULL WHERE id = $1`, subscription.ID)
	requireConstraint(t, err, "23514", "subscription_creation_storage_pair")
}

func TestSubscriptionCreationBackfillUsesNativeUUIDKeysetAndBulkUpdateIndexes(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	allowLegacySubscriptionCreationTestRows(t, ctx, db)
	const rowCount = 50_000
	inboxID := insertSubscriptionBackfillInbox(t, ctx, db, "test-subscription-plan")
	insertLegacyActionSubscriptions(t, ctx, db, inboxID, "test-subscription-plan", 1, rowCount, 0)
	if _, err := db.ExecContext(ctx, `ANALYZE subscription`); err != nil {
		t.Fatal(err)
	}
	planTx, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	keysetPlan, err := explainLines(ctx, planTx, subscriptionCreationCandidatesAfterCursor,
		"00000000-0000-0000-0000-00000000bf68", subscriptionCreationBackfillBatchSize)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(keysetPlan, "subscription_pkey") || strings.Contains(keysetPlan, "Seq Scan on subscription") {
		t.Fatalf("native UUID keyset query did not use the primary-key index on a noninitial batch:\n%s", keysetPlan)
	}
	updatePlan, err := explainLines(ctx, planTx, updateSubscriptionCreationsFromBatch,
		pq.Array([]string{"00000000-0000-0000-0000-000000000001"}), pq.Array([]string{"actions"}),
		pq.ByteaArray([][]byte{{0}}))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(updatePlan, "subscription_pkey") || strings.Contains(updatePlan, "Seq Scan on subscription") {
		t.Fatalf("bulk conversion query did not use the primary-key index:\n%s", updatePlan)
	}
	if err := planTx.Rollback(); err != nil {
		t.Fatal(err)
	}
}

func TestBoundedSubscriptionCreationCandidatesLimitsSerializedBytes(t *testing.T) {
	candidates := []subscriptionCreationBackfillCandidate{
		{id: uuid.New(), serialized: subscriptionCreationBackfillBatchBytes / 4},
		{id: uuid.New(), serialized: subscriptionCreationBackfillBatchBytes / 4},
		{id: uuid.New(), serialized: subscriptionCreationBackfillBatchBytes / 4},
		{id: uuid.New(), serialized: subscriptionCreationBackfillBatchBytes / 4},
		{id: uuid.New(), serialized: subscriptionCreationBackfillBatchBytes / 4},
	}
	selected, total, err := boundedSubscriptionCreationCandidates(candidates)
	if err != nil {
		t.Fatal(err)
	}
	if len(selected) != 4 || total != subscriptionCreationBackfillBatchBytes {
		t.Fatalf("selected %d rows totaling %d bytes, want four rows totaling %d", len(selected), total, subscriptionCreationBackfillBatchBytes)
	}
	oversized := []subscriptionCreationBackfillCandidate{
		{id: uuid.New(), serialized: subscriptionCreationBackfillBatchBytes + 1},
		{id: uuid.New(), serialized: 1},
	}
	selected, total, err = boundedSubscriptionCreationCandidates(oversized)
	if err != nil {
		t.Fatal(err)
	}
	if len(selected) != 1 || total != subscriptionCreationBackfillBatchBytes+1 {
		t.Fatalf("oversized valid row batch = (%d rows, %d bytes), want the first row alone", len(selected), total)
	}
}

func TestSubscriptionCreationConstraintValidationValidatesPopulatedAndEmptyTables(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	owner := newOwner()
	if _, err := New(db, defaultQuotas()).Subscribe(ctx, owner, body("validate-populated-subscription"), nil); err != nil {
		t.Fatalf("create typed subscription for validation: %v", err)
	}
	if err := ValidateSubscriptionCreationConstraints(ctx, db); err != nil {
		t.Fatalf("validate typed constraints with existing subscription: %v", err)
	}
	assertSubscriptionCreationConstraintsValidated(t, ctx, db)

	emptyDB, ctx := newStorageIntegrationDatabase(t)
	if got := countQuery(t, ctx, emptyDB, `SELECT count(*) FROM subscription`); got != 0 {
		t.Fatalf("fresh validation database has %d subscriptions, want empty", got)
	}
	if err := ValidateSubscriptionCreationConstraints(ctx, emptyDB); err != nil {
		t.Fatalf("validate typed constraints with no rows: %v", err)
	}
	assertSubscriptionCreationConstraintsValidated(t, ctx, emptyDB)
}

func TestSubscriptionCreationConstraintValidationRejectsInvalidRetainedRowsAndResumes(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	allowLegacySubscriptionCreationTestRows(t, ctx, db)
	inboxID := insertSubscriptionBackfillInbox(t, ctx, db, "test-subscription-validation")
	insertLegacyActionSubscriptions(t, ctx, db, inboxID, "test-subscription-validation", 1, 1, 0)
	if _, err := db.ExecContext(ctx, `ALTER TABLE subscription ADD CONSTRAINT subscription_creation_typed_required
		CHECK (creation_proto IS NOT NULL AND source_provider IS NOT NULL) NOT VALID`); err != nil {
		t.Fatal(err)
	}
	if err := ValidateSubscriptionCreationConstraints(ctx, db); err == nil || !strings.Contains(err.Error(), "subscription_creation_typed_required") {
		t.Fatalf("validation error = %v, want typed-required violation", err)
	}
	var validated bool
	if err := db.QueryRowContext(ctx, `SELECT convalidated FROM pg_constraint
		WHERE conrelid = 'subscription'::regclass AND conname = 'subscription_creation_typed_required'`).Scan(&validated); err != nil {
		t.Fatal(err)
	}
	if validated {
		t.Fatal("typed-required constraint was marked validated despite a retained legacy row")
	}

	if err := BackfillSubscriptionCreations(ctx, db); err != nil {
		t.Fatalf("backfill retained row before retrying validation: %v", err)
	}
	if err := ValidateSubscriptionCreationConstraints(ctx, db); err != nil {
		t.Fatalf("resume validation after backfill: %v", err)
	}
	assertSubscriptionCreationConstraintsValidated(t, ctx, db)
}

func assertSubscriptionCreationConstraintsValidated(t *testing.T, ctx context.Context, db *sql.DB) {
	t.Helper()
	var validatedCount int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM pg_constraint
		WHERE conrelid = 'subscription'::regclass
		AND conname = ANY($1::text[]) AND convalidated`, pq.Array(subscriptionCreationConstraints)).Scan(&validatedCount); err != nil {
		t.Fatal(err)
	}
	if want := len(subscriptionCreationConstraints); validatedCount != want {
		t.Fatalf("validated typed subscription constraints = %d, want %d", validatedCount, want)
	}
}

func insertSubscriptionBackfillInbox(t *testing.T, ctx context.Context, db *sql.DB, ownerName string) uuid.UUID {
	t.Helper()
	id := uuid.New()
	ref := DestinationRef{Namespace: "test-namespace", Name: "test-sandbox", UID: testSandboxUID}
	if _, err := db.ExecContext(ctx, `INSERT INTO inbox (
		id, owner_namespace, owner_name, destination_key, destination_namespace, destination_name,
		destination_uid, session_id, last_cursor, acknowledged,
		covered, expired_through, retired, updated_at, next_attempt, claim_until)
		VALUES ($1, 'test-namespace', $2, $3, $4, $5, $6, 'test-session', 0, 0, 0, 0, false, now(), now(), now())`,
		id, ownerName, DestinationKey(ref, "test-session"), ref.Namespace, ref.Name, ref.UID); err != nil {
		t.Fatal(err)
	}
	return id
}

func insertLegacyActionSubscriptions(
	t *testing.T, ctx context.Context, db *sql.DB, inboxID uuid.UUID, ownerName string, first, last, invalidAt int,
) {
	t.Helper()
	if last < first {
		return
	}
	_, err := db.ExecContext(ctx, `INSERT INTO subscription (
		id, inbox_id, idempotency_key, creation, version, actions_after_sequence, generation, cancelled, expires_at)
		SELECT ('00000000-0000-0000-0000-' || lpad(to_hex(row_number), 12, '0'))::uuid,
			$1, 'key-' || row_number,
			jsonb_build_object('destination_ref', jsonb_build_object('namespace', 'test-namespace', 'name', 'test-sandbox', 'uid', 'test-uid'),
				'session_id', 'test-session', 'idempotency_key', 'key-' || row_number,
				'source', jsonb_build_object('provider', 'actions',
					'request_id', CASE WHEN row_number = $4 THEN NULL ELSE
						('12345678-1234-5678-9abc-' || lpad(to_hex(row_number), 12, '0')) END,
					'after_sequence', row_number), 'lifetime_days', 7),
			1, row_number, 0, false, now() + interval '7 days'
		FROM generate_series($2::integer, $3::integer) AS rows(row_number)`, inboxID, first, last, invalidAt)
	if err != nil {
		t.Fatalf("insert legacy rows for %s: %v", ownerName, err)
	}
}

func countQuery(t *testing.T, ctx context.Context, db *sql.DB, query string, args ...any) int {
	t.Helper()
	var count int
	if err := db.QueryRowContext(ctx, query, args...).Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}
