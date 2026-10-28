package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

func TestEventIdentityBackfillResumesAfterACommittedBatch(t *testing.T) {
	f := newEventIdentityBackfillFixture(t)
	resetEventIdentityBackfill(t, f)
	inboxID := uuid.Nil
	f.exec(t, `INSERT INTO inbox (id, owner_namespace, owner_name, destination_key,
		destination_namespace, destination_name, destination_uid, session_id,
		last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until)
		VALUES ($1, 'identity-backfill-test', 'owner', 'destination', 'identity-ns', 'identity-name', 'identity-uid', 'session', 6, 0, 0, 0,
			false, now(), now(), now())`, inboxID)
	requestID := uuid.New()
	deliveryID := uuid.New()
	messageID := uuid.New()
	events := []model.EventIdentity{
		model.ActionsEvent{RequestID: requestID, Sequence: 1},
		model.GitHubEvent{AppID: 11, DeliveryID: deliveryID, RepositoryID: 22, Event: model.EventPullRequest},
		model.GitHubEvent{AppID: 11, DeliveryID: uuid.New(), RepositoryID: 22, Event: model.EventPullRequest, Action: ptr("")},
		model.GitHubObservationEvent{AppID: 11, RepositoryID: 22, Event: model.EventCheckRun, Sequence: 2},
		model.GitHubMergeabilityEvent{AppID: 11, RepositoryID: 22, PullRequestNumber: 7, Sequence: 3},
		model.GitHubMergeabilitySnapshotEvent{AppID: 11, RepositoryID: 22, PullRequestNumber: 7, Version: 4},
		model.MessageEvent{MessageID: messageID, Sender: model.ServiceAccount{Namespace: "sender-ns", Name: "sender"}},
	}
	for i, event := range events {
		data, err := json.Marshal(event)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := f.db.ExecContext(t.Context(), `INSERT INTO entry (inbox_id, cursor, event, payload, created_at)
			VALUES ($1, $2, $3::jsonb, NULL, now())`, inboxID, i+1, string(data)); err != nil {
			t.Fatal(err)
		}
	}
	// Keep a representative analyzed cohort in a later native UUID range. Its rows already have relational
	// identity, so the backfill exercises keyset planning without spending thousands of batches converting it.
	seedInboxID := uuid.MustParse("ffffffff-ffff-ffff-ffff-ffffffffffff")
	f.exec(t, `INSERT INTO inbox (id, owner_namespace, owner_name, destination_key,
		destination_namespace, destination_name, destination_uid, session_id,
		last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until)
		VALUES ($1, 'identity-plan-test', 'owner', 'destination', 'identity-ns', 'identity-name', 'identity-uid', 'session', 10000, 0, 0, 0,
			false, now(), now(), now())`, seedInboxID)
	planRequestID := uuid.MustParse("00000000-0000-0000-0000-000000000001")
	if _, err := f.db.ExecContext(t.Context(), `INSERT INTO entry (inbox_id, cursor, event, payload, created_at,
		identity_provider, actions_request_id, actions_sequence)
		SELECT $1, sequence,
			jsonb_build_object('provider', 'actions', 'request_id', $2::text, 'sequence', sequence),
			NULL, now(), 'actions', $2::uuid, sequence
		FROM generate_series(1, 10000) AS series(sequence)`, seedInboxID, planRequestID); err != nil {
		t.Fatal(err)
	}
	conn, err := f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := checkEventIdentityBackfillPlans(t.Context(), conn); err != nil {
		t.Fatal(err)
	}
	finished, err := backfillEventIdentityBatch(t.Context(), conn, 1)
	if err != nil {
		t.Fatal(err)
	}
	if finished {
		t.Fatal("backfill finished after one row in a multi-row fixture")
	}
	var lastInboxID uuid.UUID
	var lastCursor int64
	var phase string
	if err := f.db.QueryRowContext(t.Context(), `SELECT last_inbox_id, last_cursor, phase
		FROM notification_event_identity_backfill WHERE singleton`).Scan(&lastInboxID, &lastCursor, &phase); err != nil {
		t.Fatal(err)
	}
	if lastInboxID != inboxID || lastCursor != 1 || phase != "backfilling" {
		t.Fatalf("progress after one batch = (%s, %d, %q)", lastInboxID, lastCursor, phase)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND identity_provider IS NOT NULL`, inboxID); got != 1 {
		t.Fatalf("first batch converted %d entries, want 1", got)
	}
	if err := BackfillEventIdentities(t.Context(), f.db, 4096); err != nil {
		t.Fatal(err)
	}
	if err := f.db.QueryRowContext(t.Context(), `SELECT last_inbox_id, last_cursor, phase
		FROM notification_event_identity_backfill WHERE singleton`).Scan(&lastInboxID, &lastCursor, &phase); err != nil {
		t.Fatal(err)
	}
	if phase != "indexed" {
		t.Fatalf("completed progress = (%s, %d, %q)", lastInboxID, lastCursor, phase)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND identity_provider IS NULL`, inboxID); got != 0 {
		t.Fatalf("%d legacy identities remain", got)
	}
	var provider, event string
	var appID, repositoryID, observationSequence int64
	if err := f.db.QueryRowContext(t.Context(), `SELECT identity_provider, github_app_id, github_repository_id,
		github_event, github_observation_sequence FROM entry WHERE inbox_id = $1 AND cursor = 4`, inboxID).
		Scan(&provider, &appID, &repositoryID, &event, &observationSequence); err != nil {
		t.Fatal(err)
	}
	if provider != "github_observation" || appID != 11 || repositoryID != 22 || event != string(model.EventCheckRun) || observationSequence != 2 {
		t.Fatalf("backfilled GitHub observation identity = (%q, %d, %d, %q, %d)",
			provider, appID, repositoryID, event, observationSequence)
	}
	if got := f.count(t, `SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
		WHERE c.relname LIKE 'entry_identity_%_key' AND i.indisvalid`); got != 6 {
		t.Fatalf("valid identity indexes = %d, want 6", got)
	}
}

func TestEventIdentityBackfillPrefixBoundsSerializedJSONBytes(t *testing.T) {
	rowBytes := eventIdentityBackfillMaxBatchBytes/2 + 1024
	candidates := []eventIdentityBackfillRow{
		{cursor: 1, eventBytes: rowBytes},
		{cursor: 2, eventBytes: rowBytes},
		{cursor: 3, eventBytes: 1},
	}
	got := eventIdentityBackfillPrefix(candidates)
	if len(got) != 1 || got[0].cursor != 1 {
		t.Fatalf("selected cursors after byte bound = %v, want [1]", eventIdentityBackfillCursors(got))
	}

	oversize := []eventIdentityBackfillRow{
		{cursor: 4, eventBytes: eventIdentityBackfillMaxBatchBytes + 1},
		{cursor: 5, eventBytes: 1},
	}
	got = eventIdentityBackfillPrefix(oversize)
	if len(got) != 1 || got[0].cursor != 4 {
		t.Fatalf("oversized identity batch = %v, want [4] alone", eventIdentityBackfillCursors(got))
	}
}

func eventIdentityBackfillCursors(rows []eventIdentityBackfillRow) []int64 {
	cursors := make([]int64, len(rows))
	for i, row := range rows {
		cursors[i] = row.cursor
	}
	return cursors
}

func TestEventIdentityBackfillUsesSerializedByteBudgetBeforeLoadingDocuments(t *testing.T) {
	f := newEventIdentityBackfillFixture(t)
	resetEventIdentityBackfill(t, f)
	inboxID := uuid.New()
	const rowCount = 3000
	f.exec(t, `INSERT INTO inbox (id, owner_namespace, owner_name, destination_key,
		destination_namespace, destination_name, destination_uid, session_id,
		last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until)
		VALUES ($1, 'identity-byte-budget-test', 'owner', 'destination', 'identity-ns', 'identity-name', 'identity-uid', 'session', $2, 0, 0, 0,
			false, now(), now(), now())`, inboxID, rowCount)
	random := rand.New(rand.NewSource(1))
	const alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
	actionBytes := make([]byte, 1800)
	for i := range actionBytes {
		actionBytes[i] = alphabet[random.Intn(len(alphabet))]
	}
	largeAction := string(actionBytes)
	f.exec(t, `INSERT INTO entry (inbox_id, cursor, event, payload, created_at)
		SELECT $1, sequence,
			jsonb_build_object('provider', 'github', 'app_id', 1, 'delivery_id', (md5(sequence::text)::uuid)::text,
				'repository_id', 2, 'event', 'pull_request', 'action', $3::text),
			NULL, now()
		FROM generate_series(1, $2) AS series(sequence)`, inboxID, rowCount, largeAction)
	conn, err := f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	finished, err := backfillEventIdentityBatch(t.Context(), conn, rowCount)
	if err != nil {
		t.Fatal(err)
	}
	if finished {
		t.Fatal("backfill finished before processing all large identities")
	}
	var lastCursor int
	if err := f.db.QueryRowContext(t.Context(), `SELECT last_cursor FROM notification_event_identity_backfill WHERE singleton`).Scan(&lastCursor); err != nil {
		t.Fatal(err)
	}
	if lastCursor <= 0 || lastCursor >= rowCount {
		t.Fatalf("byte-bounded progress cursor = %d, want between 1 and %d", lastCursor, rowCount-1)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND identity_provider IS NOT NULL`, inboxID); got != lastCursor {
		t.Fatalf("entries converted in first byte-bounded batch = %d, want %d", got, lastCursor)
	}
	var convertedBytes int64
	if err := f.db.QueryRowContext(t.Context(), `SELECT COALESCE(sum(octet_length(event::text)), 0)
		FROM entry WHERE inbox_id = $1 AND identity_provider IS NOT NULL`, inboxID).Scan(&convertedBytes); err != nil {
		t.Fatal(err)
	}
	if convertedBytes > eventIdentityBackfillMaxBatchBytes {
		t.Fatalf("serialized identity bytes converted in one batch = %d, exceeds %d", convertedBytes, eventIdentityBackfillMaxBatchBytes)
	}
}

func resetEventIdentityBackfill(t *testing.T, f fixture) {
	t.Helper()
	f.exec(t, `TRUNCATE entry CASCADE`)
	for _, index := range eventIdentityIndexes {
		if _, err := f.db.ExecContext(t.Context(), `DROP INDEX IF EXISTS `+index.name); err != nil {
			t.Fatalf("drop event identity test index %s: %v", index.name, err)
		}
	}
	f.exec(t, `DELETE FROM notification_event_identity_backfill`)
}

func TestEventIdentityBackfillRefusesSameNamedIndexOnAnotherRelation(t *testing.T) {
	f := newEventIdentityBackfillFixture(t)
	resetEventIdentityBackfill(t, f)
	index := eventIdentityIndexes[0]
	f.exec(t, `CREATE TABLE event_identity_index_target_elsewhere (id BIGINT)`)
	f.exec(t, `CREATE INDEX `+index.name+` ON event_identity_index_target_elsewhere (id)`)
	f.exec(t, `INSERT INTO notification_event_identity_backfill (singleton, phase) VALUES (true, 'backfilled')`)

	conn, err := f.db.Conn(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	err = buildEventIdentityIndexes(t.Context(), conn)
	if err == nil || !strings.Contains(err.Error(), "exists on a relation other than entry") {
		t.Fatalf("index builder error = %v, want an unrelated-relation error", err)
	}
	if got := f.count(t, `SELECT count(*) FROM pg_index i
		JOIN pg_class c ON c.oid = i.indexrelid
		JOIN pg_namespace n ON n.oid = c.relnamespace
		WHERE n.nspname = current_schema() AND c.relname = $1
		AND i.indrelid = 'event_identity_index_target_elsewhere'::regclass`, index.name); got != 1 {
		t.Fatalf("unrelated index remaining = %d, want 1", got)
	}
	var phase string
	if err := f.db.QueryRowContext(t.Context(), `SELECT phase FROM notification_event_identity_backfill WHERE singleton`).Scan(&phase); err != nil {
		t.Fatal(err)
	}
	if phase != "backfilled" {
		t.Fatalf("phase after unrelated index rejection = %q, want backfilled", phase)
	}
}

func TestEventIdentityBackfillReportsProjectionCollisionsAndRetainsEvidence(t *testing.T) {
	f := newEventIdentityBackfillFixture(t)
	resetEventIdentityBackfill(t, f)
	inboxID := uuid.New()
	f.exec(t, `INSERT INTO inbox (id, owner_namespace, owner_name, destination_key,
		destination_namespace, destination_name, destination_uid, session_id,
		last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until)
		VALUES ($1, 'identity-collision-test', 'owner', 'destination', 'identity-ns', 'identity-name', 'identity-uid', 'session', 8, 0, 0, 0,
			false, now(), now(), now())`, inboxID)
	deliveryID := uuid.New()
	messageID := uuid.New()
	events := []string{
		fmt.Sprintf(`{"provider":"github","app_id":1,"delivery_id":%q,"repository_id":2,"event":"pull_request"}`, deliveryID),
		fmt.Sprintf(`{"provider":"github","app_id":1,"delivery_id":%q,"repository_id":2,"event":"pull_request","action":null}`, deliveryID),
		fmt.Sprintf(`{"provider":"message","message_id":%q,"sender":{"name":"sender"}}`, messageID),
		fmt.Sprintf(`{"provider":"message","message_id":%q,"sender":{"namespace":"","name":"sender"}}`, messageID),
		fmt.Sprintf(`{"provider":"message","message_id":%q,"sender":{"namespace":null,"name":"sender"}}`, messageID),
		fmt.Sprintf(`{"provider":"message","message_id":%q,"sender":{"namespace":"ns"}}`, messageID),
		fmt.Sprintf(`{"provider":"message","message_id":%q,"sender":{"namespace":"ns","name":""}}`, messageID),
		fmt.Sprintf(`{"provider":"message","message_id":%q,"sender":{"namespace":"ns","name":null}}`, messageID),
	}
	for i, event := range events {
		if _, err := f.db.ExecContext(t.Context(), `INSERT INTO entry (inbox_id, cursor, event, payload, created_at)
			VALUES ($1, $2, $3::jsonb, NULL, now())`, inboxID, i+1, event); err != nil {
			t.Fatal(err)
		}
	}
	err := BackfillEventIdentities(t.Context(), f.db, 16)
	if err == nil {
		t.Fatal("backfill indexed colliding legacy identities")
	}
	for _, want := range []string{
		"natural-key collision groups",
		fmt.Sprintf("inbox %s provider github cursors {1,2}", inboxID),
		fmt.Sprintf("inbox %s provider message cursors {3,4,5}", inboxID),
		fmt.Sprintf("inbox %s provider message cursors {6,7,8}", inboxID),
	} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("backfill error %q does not contain %q", err, want)
		}
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, inboxID); got != len(events) {
		t.Fatalf("retained entries = %d, want %d", got, len(events))
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND identity_provider IS NOT NULL`, inboxID); got != len(events) {
		t.Fatalf("relational identities retained = %d, want %d", got, len(events))
	}
	if got := f.count(t, `SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
		WHERE c.relname LIKE 'entry_identity_%_key' AND i.indisvalid`); got != 0 {
		t.Fatalf("built %d identity indexes despite unresolved collisions", got)
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND event ? 'action'`, inboxID); got != 1 {
		t.Fatalf("retained GitHub JSON action field variants = %d, want 1 explicit null", got)
	}
}

func TestEventIdentityBackfillRejectsUnknownLegacyShapeWithLocation(t *testing.T) {
	f := newEventIdentityBackfillFixture(t)
	resetEventIdentityBackfill(t, f)
	inboxID := uuid.New()
	f.exec(t, `INSERT INTO inbox (id, owner_namespace, owner_name, destination_key,
		destination_namespace, destination_name, destination_uid, session_id,
		last_cursor, acknowledged, covered, expired_through, retired, updated_at, next_attempt, claim_until)
		VALUES ($1, 'identity-unknown-test', 'owner', 'destination', 'identity-ns', 'identity-name', 'identity-uid', 'session', 1, 0, 0, 0,
			false, now(), now(), now())`, inboxID)
	event := `{"provider":"actions","request_id":"00000000-0000-0000-0000-000000000001","sequence":1,"future":true}`
	if _, err := f.db.ExecContext(t.Context(), `INSERT INTO entry (inbox_id, cursor, event, payload, created_at)
		VALUES ($1, 1, $2::jsonb, NULL, now())`, inboxID, event); err != nil {
		t.Fatal(err)
	}
	err := BackfillEventIdentities(t.Context(), f.db, 16)
	if err == nil {
		t.Fatal("backfilled an unknown legacy event shape")
	}
	for _, want := range []string{fmt.Sprintf("inbox %s entry 1", inboxID), "unknown field \"future\""} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("backfill error %q does not contain %q", err, want)
		}
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND identity_provider IS NULL`, inboxID); got != 1 {
		t.Fatalf("untouched legacy entries = %d, want 1", got)
	}
}

func newEventIdentityBackfillFixture(t *testing.T) fixture {
	t.Helper()
	db, ctx := newStorageIntegrationDatabase(t)
	resetNotificationSchema(t, db)
	if err := migrations.UpTo(ctx, db, 16); err != nil {
		t.Fatalf("build pre-provider-proof schema through migration 00016: %v", err)
	}
	return fixtureForDatabase(t, db, defaultQuotas())
}

func checkEventIdentityBackfillPlans(ctx context.Context, conn *sql.Conn) error {
	if _, err := conn.ExecContext(ctx, `ANALYZE entry`); err != nil {
		return fmt.Errorf("analyze entry before checking backfill plans: %w", err)
	}
	var estimatedRows float64
	if err := conn.QueryRowContext(ctx, `SELECT reltuples FROM pg_class WHERE oid = 'entry'::regclass`).Scan(&estimatedRows); err != nil {
		return err
	}
	if estimatedRows < 10000 {
		return fmt.Errorf("plan fixture has only %.0f estimated entry rows; want at least 10000", estimatedRows)
	}
	var sampleInboxID uuid.UUID
	var sampleCursor int64
	var sampleEvent string
	if err := conn.QueryRowContext(ctx, `SELECT inbox_id, cursor, event::text FROM entry
		ORDER BY inbox_id, cursor OFFSET 37 LIMIT 1`).Scan(&sampleInboxID, &sampleCursor, &sampleEvent); err != nil {
		return fmt.Errorf("read representative event identity backfill row: %w", err)
	}
	plans := []struct {
		name  string
		query string
		args  []any
	}{
		{
			name:  "initial ordered read",
			query: `EXPLAIN (COSTS OFF) ` + eventIdentityBackfillInitialSelect,
			args:  []any{eventIdentityBackfillBatchSize},
		},
		{
			name:  "keyset ordered read",
			query: `EXPLAIN (COSTS OFF) ` + eventIdentityBackfillSelect,
			args:  []any{sampleInboxID, sampleCursor, eventIdentityBackfillBatchSize},
		},
		{
			name:  "bulk event load",
			query: `EXPLAIN (COSTS OFF) ` + eventIdentityBackfillLoad,
			args:  []any{pq.Array([]string{sampleInboxID.String()}), pq.Array([]int64{sampleCursor})},
		},
		{
			name:  "bulk update",
			query: `EXPLAIN (COSTS OFF) ` + eventIdentityBackfillUpdate,
			args: []any{
				pq.Array([]string{sampleInboxID.String()}), pq.Array([]int64{sampleCursor}), pq.Array([]string{sampleEvent}),
			},
		},
	}
	for _, check := range plans {
		rows, err := conn.QueryContext(ctx, check.query, check.args...)
		if err != nil {
			return fmt.Errorf("explain event identity %s: %w", check.name, err)
		}
		var lines []string
		for rows.Next() {
			var line string
			if err := rows.Scan(&line); err != nil {
				rows.Close()
				return err
			}
			lines = append(lines, line)
		}
		if err := errors.Join(rows.Err(), rows.Close()); err != nil {
			return err
		}
		plan := strings.Join(lines, "\n")
		if !strings.Contains(plan, "entry_pkey") || strings.Contains(plan, "Seq Scan") || strings.Contains(plan, "Sort") {
			return fmt.Errorf("event identity %s plan must use entry_pkey without a full scan or sort:\n%s", check.name, plan)
		}
	}
	return nil
}
