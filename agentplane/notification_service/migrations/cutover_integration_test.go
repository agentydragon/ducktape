package migrations_test

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"reflect"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/notification_service/storagepb"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

const (
	fixtureInboxID        = "11111111-2222-4333-8444-555555555555"
	fixtureSubscriptionID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	fixtureRequestID      = "5b9f1a2e-3c4d-4e5f-8a6b-7c8d9e0f1a2b"
	fixtureMessageID      = "66666666-7777-4888-8999-aaaaaaaaaaaa"
	fixtureDeliveryID     = "8d3c1f2a-5b6e-4c7d-9e0f-1a2b3c4d5e6f"
	fixtureInboxCursorMax = 7
	cohortRows            = 13_500
)

var integrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTests(
		func(ctx context.Context, db *sql.DB) error {
			return migrations.UpTo(ctx, db, 8)
		},
		func(database *postgres.Database) int {
			ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
			defer cancel()
			if err := database.PrepareCloneTemplate(ctx); err != nil {
				fmt.Fprintln(os.Stderr, "prepare version-8 PostgreSQL clone template:", err)
				return 1
			}
			integrationDatabase = database
			return m.Run()
		},
	))
}

func TestUpCutoverPreservesRetainedRowsAndIsIdempotent(t *testing.T) {
	db := newVersion8Database(t)
	seedLegacyRetainedRows(t, db)
	ctx, cancel := migrationContext(t, 4*time.Minute)
	defer cancel()

	if err := migrations.Up(ctx, db); err != nil {
		t.Fatalf("upgrade populated version-8 database: %v", err)
	}
	assertLatestGooseVersion(t, ctx, db, 9)
	assertPreservedRows(t, ctx, db)

	if err := migrations.Up(ctx, db); err != nil {
		t.Fatalf("repeat upgrade: %v", err)
	}
	assertLatestGooseVersion(t, ctx, db, 9)
	assertPreservedRows(t, ctx, db)
}

func TestUpFailurePreservesRowsAndCanBeRetriedAfterRepair(t *testing.T) {
	db := newVersion8Database(t)
	seedInbox(t, db, fixtureInboxID, 1, 0)
	seedLegacyEntry(t, db, fixtureInboxID, 1,
		`{"provider":"actions","request_id":"`+fixtureRequestID+`","sequence":1}`,
		`{"actor":null,"at":"2026-10-20T03:56:03Z","sequence":1,"state":"future_state"}`)
	ctx, cancel := migrationContext(t, 4*time.Minute)
	defer cancel()

	if err := migrations.Up(ctx, db); err == nil {
		t.Fatal("upgrade accepted an unknown historical Action state")
	}
	assertLatestGooseVersion(t, ctx, db, 8)
	assertGooseVersionNotApplied(t, ctx, db, 9)
	assertLegacyEntryStillPresent(t, ctx, db, fixtureInboxID, 1)

	if _, err := db.ExecContext(ctx, `UPDATE entry SET payload = $3::jsonb WHERE inbox_id = $1 AND cursor = $2`,
		fixtureInboxID, 1,
		`{"actor":null,"at":"2026-10-20T03:56:03Z","sequence":1,"state":"failed"}`); err != nil {
		t.Fatalf("repair historical Action payload: %v", err)
	}
	if err := migrations.Up(ctx, db); err != nil {
		t.Fatalf("retry upgrade after repair: %v", err)
	}
	assertLatestGooseVersion(t, ctx, db, 9)
	assertColumn(t, ctx, db, "entry", "event", false)
	var provider, requestID string
	var sequence int64
	var state int32
	if err := db.QueryRowContext(ctx, `SELECT identity_provider, actions_request_id::text, actions_sequence, action_state
		FROM entry WHERE inbox_id = $1 AND cursor = 1`, fixtureInboxID).
		Scan(&provider, &requestID, &sequence, &state); err != nil {
		t.Fatal(err)
	}
	if provider != "actions" || requestID != fixtureRequestID || sequence != 1 || state != int32(action_servicepb.ActionState_ACTION_STATE_FAILED) {
		t.Fatalf("repaired row identity/content = (%q, %q, %d, %d)", provider, requestID, sequence, state)
	}
}

func TestUpRetriesAfterFinalContractCommitsButGooseHistoryWriteFails(t *testing.T) {
	db := newVersion8Database(t)
	seedInbox(t, db, fixtureInboxID, 1, 0)
	seedLegacyEntry(t, db, fixtureInboxID, 1,
		`{"provider":"actions","request_id":"`+fixtureRequestID+`","sequence":1}`,
		`{"actor":null,"at":"2026-10-20T03:56:03Z","sequence":1,"state":"succeeded"}`)
	ctx, cancel := migrationContext(t, 4*time.Minute)
	defer cancel()

	if _, err := db.ExecContext(ctx, `CREATE FUNCTION reject_applied_notification_v9() RETURNS trigger
		LANGUAGE plpgsql AS $$ BEGIN
			IF NEW.version_id = 9 AND NEW.is_applied THEN
				RAISE EXCEPTION USING ERRCODE = 'P9999', MESSAGE = 'test rejects applied migration 9';
			END IF;
			RETURN NEW;
		END $$`); err != nil {
		t.Fatalf("install history-write fault: %v", err)
	}
	if _, err := db.ExecContext(ctx, `CREATE TRIGGER reject_applied_notification_v9
		BEFORE INSERT OR UPDATE ON notification_service_goose_version
		FOR EACH ROW WHEN (NEW.version_id = 9 AND NEW.is_applied)
		EXECUTE FUNCTION reject_applied_notification_v9()`); err != nil {
		t.Fatalf("install version 9 history trigger: %v", err)
	}
	var historyWriteFailure *pq.Error
	if err := migrations.Up(ctx, db); !errors.As(err, &historyWriteFailure) || string(historyWriteFailure.Code) != "P9999" {
		t.Fatalf("upgrade error = %v, want injected version 9 history-write failure", err)
	}
	assertLatestGooseVersion(t, ctx, db, 8)
	assertGooseVersionNotApplied(t, ctx, db, 9)
	assertColumn(t, ctx, db, "entry", "event", false)
	assertActionContent(t, ctx, db, 1, action_servicepb.ActionState_ACTION_STATE_SUCCEEDED, "", "", "", "", "")

	if _, err := db.ExecContext(ctx, `DROP TRIGGER reject_applied_notification_v9 ON notification_service_goose_version`); err != nil {
		t.Fatalf("remove version 9 history trigger: %v", err)
	}
	if _, err := db.ExecContext(ctx, `DROP FUNCTION reject_applied_notification_v9()`); err != nil {
		t.Fatalf("remove history-write fault: %v", err)
	}
	if err := migrations.Up(ctx, db); err != nil {
		t.Fatalf("retry after the completed contract's history write failed: %v", err)
	}
	assertLatestGooseVersion(t, ctx, db, 9)
	assertActionContent(t, ctx, db, 1, action_servicepb.ActionState_ACTION_STATE_SUCCEEDED, "", "", "", "", "")
	var rows int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM entry WHERE inbox_id = $1`, fixtureInboxID).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 1 {
		t.Fatalf("entries after history-write retry = %d, want 1", rows)
	}
}

func TestConcurrentUpCallsConvergeOnTheSameCutover(t *testing.T) {
	dbURL := integrationDatabase.Clone(t)
	first := openDatabase(t, dbURL)
	second := openDatabase(t, dbURL)
	seedInbox(t, first, fixtureInboxID, 1, 1)
	seedLegacyEntry(t, first, fixtureInboxID, 1,
		`{"provider":"actions","request_id":"`+fixtureRequestID+`","sequence":1}`,
		`{"actor":null,"at":"2026-10-20T03:56:03Z","sequence":1,"state":"running"}`)
	ctx, cancel := migrationContext(t, 4*time.Minute)
	defer cancel()

	start := make(chan struct{})
	errors := make(chan error, 2)
	for _, db := range []*sql.DB{first, second} {
		go func(db *sql.DB) {
			<-start
			errors <- migrations.Up(ctx, db)
		}(db)
	}
	close(start)
	for range 2 {
		if err := <-errors; err != nil {
			t.Fatalf("concurrent upgrade: %v", err)
		}
	}
	assertLatestGooseVersion(t, ctx, first, 9)

	assertColumn(t, ctx, first, "entry", "event", false)
	var count int
	if err := first.QueryRowContext(ctx, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND identity_provider = 'actions'
		AND actions_request_id = $2::uuid AND actions_sequence = 1`, fixtureInboxID, fixtureRequestID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("migrated Action entries = %d, want 1", count)
	}
}

func TestUpBackfillsAStagingScaleIdentityCohort(t *testing.T) {
	db := newVersion8Database(t)
	seedInbox(t, db, fixtureInboxID, cohortRows, cohortRows)
	ctx, cancel := migrationContext(t, 4*time.Minute)
	defer cancel()
	if _, err := db.ExecContext(ctx, `
		INSERT INTO entry (inbox_id, cursor, event, payload, created_at)
		SELECT $1, sequence,
			jsonb_build_object('provider', 'actions', 'request_id', $2::uuid::text, 'sequence', sequence),
			NULL, TIMESTAMPTZ '2026-10-20 03:56:03+00'
		FROM generate_series(1, $3::bigint) AS generated(sequence)`, fixtureInboxID, fixtureRequestID, cohortRows); err != nil {
		t.Fatalf("seed %d historical Action identities: %v", cohortRows, err)
	}
	if err := migrations.Up(ctx, db); err != nil {
		t.Fatalf("upgrade %d-row populated database: %v", cohortRows, err)
	}
	assertLatestGooseVersion(t, ctx, db, 9)

	var total, identified, first, last, payloads int
	if err := db.QueryRowContext(ctx, `SELECT count(*), count(*) FILTER (WHERE identity_provider = 'actions'
		AND actions_request_id = $2::uuid AND actions_sequence IS NOT NULL), min(actions_sequence), max(actions_sequence),
		count(*) FILTER (WHERE payload IS NOT NULL)
		FROM entry WHERE inbox_id = $1`, fixtureInboxID, fixtureRequestID).
		Scan(&total, &identified, &first, &last, &payloads); err != nil {
		t.Fatal(err)
	}
	if total != cohortRows || identified != cohortRows || first != 1 || last != cohortRows || payloads != 0 {
		t.Fatalf("migrated cohort total/identified/first/last/payloads = %d/%d/%d/%d/%d",
			total, identified, first, last, payloads)
	}
}

func newVersion8Database(t *testing.T) *sql.DB {
	t.Helper()
	return openDatabase(t, integrationDatabase.Clone(t))
}

func openDatabase(t *testing.T, dbURL string) *sql.DB {
	t.Helper()
	db, err := sql.Open("postgres", dbURL)
	if err != nil {
		t.Fatal(err)
	}
	db.SetMaxOpenConns(4)
	t.Cleanup(func() { _ = db.Close() })
	if err := db.PingContext(t.Context()); err != nil {
		t.Fatal(err)
	}
	return db
}

func migrationContext(t *testing.T, timeout time.Duration) (context.Context, context.CancelFunc) {
	t.Helper()
	return context.WithTimeout(t.Context(), timeout)
}

func assertLatestGooseVersion(t *testing.T, ctx context.Context, db *sql.DB, want int64) {
	t.Helper()
	var got int64
	if err := db.QueryRowContext(ctx, `SELECT COALESCE(max(version_id), 0)
		FROM notification_service_goose_version WHERE is_applied`).Scan(&got); err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("latest applied Goose migration = %d, want %d", got, want)
	}
}

func assertGooseVersionNotApplied(t *testing.T, ctx context.Context, db *sql.DB, version int64) {
	t.Helper()
	var applied bool
	if err := db.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM notification_service_goose_version WHERE version_id = $1 AND is_applied
	)`, version).Scan(&applied); err != nil {
		t.Fatal(err)
	}
	if applied {
		t.Fatalf("Goose migration %d is marked applied after failed upgrade", version)
	}
}

func seedLegacyRetainedRows(t *testing.T, db *sql.DB) {
	t.Helper()
	ctx := t.Context()
	seedInbox(t, db, fixtureInboxID, fixtureInboxCursorMax, 1)
	if _, err := db.ExecContext(ctx, `INSERT INTO subscription
		(id, inbox_id, idempotency_key, creation, version, actions_after_sequence, cancelled, expires_at, generation)
		VALUES ($1, $2, 'original-key', $3::jsonb, 1, 42, false, TIMESTAMPTZ '2099-01-01 00:00:00+00', 0)`,
		fixtureSubscriptionID, fixtureInboxID,
		`{"destination_ref":{"namespace":"test-namespace","name":"test-sandbox","uid":"test-uid"},"session_id":"test-session","idempotency_key":"original-key","lifetime_days":7,"source":{"provider":"actions","request_id":"`+fixtureRequestID+`","after_sequence":5}}`); err != nil {
		t.Fatalf("seed historical subscription: %v", err)
	}

	seedLegacyEntry(t, db, fixtureInboxID, 1,
		`{"provider":"actions","request_id":"`+fixtureRequestID+`","sequence":1}`, "")
	seedLegacyEntry(t, db, fixtureInboxID, 2,
		`{"provider":"actions","request_id":"`+fixtureRequestID+`","sequence":2}`,
		`{"actor":{"account":{"namespace":"test-namespace","name":"test-sandbox"}},"at":"2026-10-20T03:56:03Z","sequence":2,"state":"cancelled"}`)
	seedLegacyEntry(t, db, fixtureInboxID, 3,
		`{"provider":"actions","request_id":"`+fixtureRequestID+`","sequence":3}`,
		`{"actor":{"issuer":"https://issuer.test","subject":"test-operator"},"at":"2026-10-20T03:56:03Z","sequence":3,"state":"denied"}`)
	seedLegacyEntry(t, db, fixtureInboxID, 4,
		`{"provider":"actions","request_id":"`+fixtureRequestID+`","sequence":4}`,
		`{"actor":null,"at":"2026-10-20T03:56:03Z","sequence":4,"state":"succeeded"}`)
	seedLegacyEntry(t, db, fixtureInboxID, 5,
		`{"provider":"github","app_id":1234567,"delivery_id":"`+fixtureDeliveryID+`","repository_id":555000111,"event":"pull_request","action":"opened"}`,
		`{"action":"opened","pull_request":{"number":7,"title":"Cutover 🧰"},"repository":{"full_name":"octo/example"}}`)
	seedLegacyEntry(t, db, fixtureInboxID, 6,
		`{"provider":"github_mergeability","app_id":1234567,"repository_id":555000111,"pull_request_number":7,"sequence":17}`,
		`{"repository":"octo/example","pullRequestNumber":7,"baseRef":"main","headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","baseSha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","previousMergeability":"UNKNOWN","mergeability":"MERGEABLE","observedAt":"2026-10-20T03:56:03.123456789Z","mergeCommit":{"sha":"cccccccccccccccccccccccccccccccccccccccc","baseSha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","headSha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}`)
	seedLegacyEntry(t, db, fixtureInboxID, 7,
		`{"provider":"message","message_id":"`+fixtureMessageID+`","sender":{"namespace":"test-namespace","name":"message-sender"}}`,
		`{"body":"café 🐈","from_session":"source-session"}`)

	if _, err := db.ExecContext(ctx, `INSERT INTO message
		(id, inbox_id, cursor, sender_namespace, sender_name, idempotency_key, digest)
		VALUES ($1, $2, 7, 'test-namespace', 'message-sender', 'message-key', decode(repeat('ab', 32), 'hex'))`,
		fixtureMessageID, fixtureInboxID); err != nil {
		t.Fatalf("seed historical message row: %v", err)
	}
	if _, err := db.ExecContext(ctx, `INSERT INTO github_delivery
		(app_id, delivery_id, installation_id, repository_id, event, action, head_sha, digest, payload)
		VALUES (1234567, $1, 7654321, 555000111, 'pull_request', 'opened',
			'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', decode(repeat('cd', 32), 'hex'), $2::jsonb)`,
		fixtureDeliveryID,
		`{"action":"opened","pull_request":{"number":7,"title":"Cutover 🧰"},"repository":{"full_name":"octo/example"}}`); err != nil {
		t.Fatalf("seed historical GitHub delivery: %v", err)
	}
	runnerEntry, err := hex.DecodeString("080712110a0d72756e6e65722d736f757263651003")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.ExecContext(ctx, `INSERT INTO notice
		(inbox_id, command_id, through_cursor, text, attempted, admitted, confirmed, runner_cursor, runner_entry)
		VALUES ($1, '99999999-8888-4777-8666-555555555555', 7, 'historical notice', true, true, false, 12, $2)`,
		fixtureInboxID, runnerEntry); err != nil {
		t.Fatalf("seed historical notice: %v", err)
	}
}

func seedInbox(t *testing.T, db *sql.DB, inboxID string, cursor, expiredThrough int) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), `INSERT INTO inbox
		(id, owner_namespace, owner_name, destination_key, destination_ref, session_id,
		 last_cursor, acknowledged, covered, expired_through, retired, updated_at, claim_until)
		VALUES ($1, 'test-namespace', 'test-owner', 'destination-key',
			'{"namespace":"test-namespace","name":"test-sandbox","uid":"test-uid"}'::jsonb,
			'test-session', $2, 0, 0, $3, false, TIMESTAMPTZ '2026-10-20 03:56:03+00',
			TIMESTAMPTZ '2026-10-20 03:56:03+00')`, inboxID, cursor, expiredThrough); err != nil {
		t.Fatalf("seed historical inbox: %v", err)
	}
}

func seedLegacyEntry(t *testing.T, db *sql.DB, inboxID string, cursor int, event, payload string) {
	t.Helper()
	var payloadValue any
	if payload != "" {
		payloadValue = payload
	}
	if _, err := db.ExecContext(t.Context(), `INSERT INTO entry (inbox_id, cursor, event, payload, created_at)
		VALUES ($1, $2, $3::jsonb, $4::jsonb, TIMESTAMPTZ '2026-10-20 03:56:03+00')`,
		inboxID, cursor, event, payloadValue); err != nil {
		t.Fatalf("seed historical entry cursor %d: %v", cursor, err)
	}
}

func assertPreservedRows(t *testing.T, ctx context.Context, db *sql.DB) {
	t.Helper()
	assertColumn(t, ctx, db, "inbox", "destination_ref", false)
	assertColumn(t, ctx, db, "inbox", "destination_namespace", true)
	assertColumn(t, ctx, db, "subscription", "creation", false)
	assertColumn(t, ctx, db, "subscription", "creation_proto", true)
	assertColumn(t, ctx, db, "entry", "event", false)
	assertColumn(t, ctx, db, "entry", "identity_provider", true)
	assertColumn(t, ctx, db, "entry", "message_body", true)

	var ownerNamespace, ownerName, destinationNamespace, destinationName, destinationUID, sessionID string
	var lastCursor, expiredThrough int
	if err := db.QueryRowContext(ctx, `SELECT owner_namespace, owner_name, destination_namespace, destination_name,
		destination_uid, session_id, last_cursor, expired_through FROM inbox WHERE id = $1`, fixtureInboxID).
		Scan(&ownerNamespace, &ownerName, &destinationNamespace, &destinationName, &destinationUID,
			&sessionID, &lastCursor, &expiredThrough); err != nil {
		t.Fatalf("read migrated inbox: %v", err)
	}
	if ownerNamespace != "test-namespace" || ownerName != "test-owner" ||
		destinationNamespace != "test-namespace" || destinationName != "test-sandbox" || destinationUID != "test-uid" ||
		sessionID != "test-session" || lastCursor != fixtureInboxCursorMax || expiredThrough != 1 {
		t.Fatalf("migrated inbox owner/destination/session/cursors = %q/%q, %q/%q/%q, %q, %d/%d",
			ownerNamespace, ownerName, destinationNamespace, destinationName, destinationUID,
			sessionID, lastCursor, expiredThrough)
	}

	var sourceProvider string
	var progress int64
	var creationBytes []byte
	if err := db.QueryRowContext(ctx, `SELECT source_provider, actions_after_sequence, creation_proto
		FROM subscription WHERE id = $1`, fixtureSubscriptionID).
		Scan(&sourceProvider, &progress, &creationBytes); err != nil {
		t.Fatalf("read migrated subscription: %v", err)
	}
	if sourceProvider != "actions" || progress != 42 {
		t.Fatalf("migrated subscription source/progress = %q/%d", sourceProvider, progress)
	}
	var creation storagepb.SubscriptionCreation
	if err := proto.Unmarshal(creationBytes, &creation); err != nil {
		t.Fatalf("decode retained subscription creation: %v", err)
	}
	requestID, err := hex.DecodeString("5b9f1a2e3c4d4e5f8a6b7c8d9e0f1a2b")
	if err != nil {
		t.Fatal(err)
	}
	if got := creation.GetDestinationRef(); got == nil || got.GetNamespace() != "test-namespace" ||
		got.GetName() != "test-sandbox" || got.GetUid() != "test-uid" || creation.GetSessionId() != "test-session" ||
		creation.GetLifetimeDays() != 7 || creation.GetActions() == nil || creation.GetActions().GetAfterSequence() != 5 ||
		!bytes.Equal(creation.GetActions().GetRequestId(), requestID) {
		t.Fatalf("migrated immutable subscription creation = %v", &creation)
	}

	assertActionContent(t, ctx, db, 2, action_servicepb.ActionState_ACTION_STATE_CANCELLED, "caller", "test-namespace", "test-sandbox", "", "")
	assertActionContent(t, ctx, db, 3, action_servicepb.ActionState_ACTION_STATE_DENIED, "operator", "", "", "https://issuer.test", "test-operator")
	assertActionContent(t, ctx, db, 4, action_servicepb.ActionState_ACTION_STATE_SUCCEEDED, "", "", "", "", "")
	var tombstoneProvider, tombstoneRequestID string
	var tombstoneSequence int64
	var tombstonePayload []byte
	if err := db.QueryRowContext(ctx, `SELECT identity_provider, actions_request_id::text, actions_sequence, payload
		FROM entry WHERE inbox_id = $1 AND cursor = 1`, fixtureInboxID).
		Scan(&tombstoneProvider, &tombstoneRequestID, &tombstoneSequence, &tombstonePayload); err != nil {
		t.Fatalf("read expired tombstone identity: %v", err)
	}
	if tombstoneProvider != "actions" || tombstoneRequestID != fixtureRequestID || tombstoneSequence != 1 || tombstonePayload != nil {
		t.Fatalf("expired tombstone provider/request/sequence/payload = %q/%q/%d/%q",
			tombstoneProvider, tombstoneRequestID, tombstoneSequence, tombstonePayload)
	}

	var githubProvider, githubEvent, githubAction string
	var githubAppID, githubRepositoryID int64
	var githubDeliveryID string
	var githubPayload []byte
	if err := db.QueryRowContext(ctx, `SELECT identity_provider, github_app_id, github_delivery_id::text,
		github_repository_id, github_event, github_action, payload FROM entry WHERE inbox_id = $1 AND cursor = 5`, fixtureInboxID).
		Scan(&githubProvider, &githubAppID, &githubDeliveryID, &githubRepositoryID, &githubEvent, &githubAction, &githubPayload); err != nil {
		t.Fatalf("read migrated GitHub event: %v", err)
	}
	if githubProvider != "github" || githubAppID != 1234567 || githubDeliveryID != fixtureDeliveryID ||
		githubRepositoryID != 555000111 || githubEvent != "pull_request" || githubAction != "opened" {
		t.Fatalf("migrated GitHub event identity = %q/%d/%q/%d/%q/%q",
			githubProvider, githubAppID, githubDeliveryID, githubRepositoryID, githubEvent, githubAction)
	}
	assertJSON(t, githubPayload, `{"action":"opened","pull_request":{"number":7,"title":"Cutover 🧰"},"repository":{"full_name":"octo/example"}}`)

	var mergeIdentityProvider string
	var mergeAppID, mergeRepositoryID, mergePullRequest, mergeSequence int64
	var mergeabilityRepository, baseRef, headSHA, baseSHA, previous, current string
	var observedAt time.Time
	var residual int16
	var mergeSHA, mergeBaseSHA, mergeHeadSHA string
	if err := db.QueryRowContext(ctx, `SELECT identity_provider, github_app_id, github_repository_id,
		github_pull_request_number, github_mergeability_sequence, mergeability_repository, mergeability_base_ref, mergeability_head_sha,
		mergeability_base_sha, mergeability_previous, mergeability_current, mergeability_observed_at,
		mergeability_observed_at_submicro_nanos, merge_commit_sha, merge_commit_base_sha, merge_commit_head_sha
		FROM entry WHERE inbox_id = $1 AND cursor = 6`, fixtureInboxID).
		Scan(&mergeIdentityProvider, &mergeAppID, &mergeRepositoryID, &mergePullRequest, &mergeSequence,
			&mergeabilityRepository, &baseRef, &headSHA, &baseSHA, &previous, &current, &observedAt,
			&residual, &mergeSHA, &mergeBaseSHA, &mergeHeadSHA); err != nil {
		t.Fatalf("read migrated mergeability content: %v", err)
	}
	wantObservedAt := time.Date(2026, 10, 20, 3, 56, 3, 123456000, time.UTC)
	if mergeIdentityProvider != "github_mergeability" || mergeAppID != 1234567 || mergeRepositoryID != 555000111 ||
		mergePullRequest != 7 || mergeSequence != 17 || mergeabilityRepository != "octo/example" || baseRef != "main" ||
		headSHA != "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" || baseSHA != "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" ||
		previous != "UNKNOWN" || current != "MERGEABLE" || !observedAt.Equal(wantObservedAt) || residual != 789 ||
		mergeSHA != "cccccccccccccccccccccccccccccccccccccccc" ||
		mergeBaseSHA != "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" ||
		mergeHeadSHA != "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" {
		t.Fatalf("migrated mergeability content = %q/%q/%q/%q/%q/%q/%s+%dns/%q/%q/%q",
			mergeabilityRepository, baseRef, headSHA, baseSHA, previous, current,
			observedAt.UTC().Format(time.RFC3339Nano), residual, mergeSHA, mergeBaseSHA, mergeHeadSHA)
	}

	var messageProvider, entryMessageID, entrySenderNamespace, entrySenderName string
	var body, fromSession string
	if err := db.QueryRowContext(ctx, `SELECT identity_provider, message_id::text, message_sender_namespace,
		message_sender_name, message_body, message_from_session FROM entry WHERE inbox_id = $1 AND cursor = 7`,
		fixtureInboxID).Scan(&messageProvider, &entryMessageID, &entrySenderNamespace, &entrySenderName, &body, &fromSession); err != nil {
		t.Fatalf("read migrated Message content: %v", err)
	}
	if messageProvider != "message" || entryMessageID != fixtureMessageID ||
		entrySenderNamespace != "test-namespace" || entrySenderName != "message-sender" ||
		body != "café 🐈" || fromSession != "source-session" {
		t.Fatalf("migrated Message identity/content = %q/%q/%q/%q, %q/%q",
			messageProvider, entryMessageID, entrySenderNamespace, entrySenderName, body, fromSession)
	}
	assertJSON(t, readJSON(t, ctx, db, `SELECT payload FROM github_delivery WHERE delivery_id = $1`, fixtureDeliveryID),
		`{"action":"opened","pull_request":{"number":7,"title":"Cutover 🧰"},"repository":{"full_name":"octo/example"}}`)

	var messageID, senderNamespace, senderName, idempotencyKey string
	var digest []byte
	if err := db.QueryRowContext(ctx, `SELECT id::text, sender_namespace, sender_name, idempotency_key, digest
		FROM message WHERE inbox_id = $1 AND cursor = 7`, fixtureInboxID).
		Scan(&messageID, &senderNamespace, &senderName, &idempotencyKey, &digest); err != nil {
		t.Fatalf("read retained Message record: %v", err)
	}
	if messageID != fixtureMessageID || senderNamespace != "test-namespace" || senderName != "message-sender" ||
		idempotencyKey != "message-key" || len(digest) != 32 {
		t.Fatalf("migrated Message record = %q/%q/%q/%q (%d digest bytes)",
			messageID, senderNamespace, senderName, idempotencyKey, len(digest))
	}

	wantRunnerEntry, err := hex.DecodeString("080712110a0d72756e6e65722d736f757263651003")
	if err != nil {
		t.Fatal(err)
	}
	var runnerEntry []byte
	if err := db.QueryRowContext(ctx, `SELECT runner_entry FROM notice WHERE inbox_id = $1`, fixtureInboxID).Scan(&runnerEntry); err != nil {
		t.Fatalf("read retained notice runner entry: %v", err)
	}
	if !bytes.Equal(runnerEntry, wantRunnerEntry) {
		t.Fatalf("notice.runner_entry = %x, want %x", runnerEntry, wantRunnerEntry)
	}

	var invalidIdentityCount, retainedOwnedPayloadCount int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FILTER (WHERE identity_provider IS NULL OR
		(identity_provider = 'actions' AND (actions_request_id IS NULL OR actions_sequence IS NULL)) OR
		(identity_provider = 'github' AND (github_app_id IS NULL OR github_delivery_id IS NULL OR github_repository_id IS NULL OR github_event IS NULL)) OR
		(identity_provider = 'github_mergeability' AND (github_app_id IS NULL OR github_repository_id IS NULL OR
			github_pull_request_number IS NULL OR github_mergeability_sequence IS NULL)) OR
		(identity_provider = 'message' AND (message_id IS NULL OR message_sender_namespace IS NULL OR message_sender_name IS NULL))),
		count(*) FILTER (WHERE identity_provider IN ('actions', 'github_mergeability', 'message') AND payload IS NOT NULL)
		FROM entry WHERE inbox_id = $1`, fixtureInboxID).Scan(&invalidIdentityCount, &retainedOwnedPayloadCount); err != nil {
		t.Fatalf("validate migrated event identities/content: %v", err)
	}
	if invalidIdentityCount != 0 || retainedOwnedPayloadCount != 0 {
		t.Fatalf("invalid migrated identity rows/content payloads = %d/%d", invalidIdentityCount, retainedOwnedPayloadCount)
	}
}

func assertActionContent(t *testing.T, ctx context.Context, db *sql.DB, cursor int, wantState action_servicepb.ActionState,
	wantKind, wantNamespace, wantName, wantIssuer, wantSubject string,
) {
	t.Helper()
	var kind, namespace, name, issuer, subject sql.NullString
	var state sql.NullInt64
	var actionAt sql.NullTime
	if err := db.QueryRowContext(ctx, `SELECT action_state, action_at, action_actor_kind,
		action_actor_namespace, action_actor_name, action_actor_issuer, action_actor_subject
		FROM entry WHERE inbox_id = $1 AND cursor = $2`, fixtureInboxID, cursor).
		Scan(&state, &actionAt, &kind, &namespace, &name, &issuer, &subject); err != nil {
		t.Fatalf("read migrated Action content at cursor %d: %v", cursor, err)
	}
	wantAt := time.Date(2026, 10, 20, 3, 56, 3, 0, time.UTC)
	if !state.Valid || state.Int64 != int64(wantState) || !actionAt.Valid || !actionAt.Time.Equal(wantAt) {
		t.Fatalf("Action content at cursor %d = state %+v, time %+v; want %v at %v", cursor, state, actionAt, wantState, wantAt)
	}
	if !matchesNullable(kind, wantKind) || !matchesNullable(namespace, wantNamespace) || !matchesNullable(name, wantName) ||
		!matchesNullable(issuer, wantIssuer) || !matchesNullable(subject, wantSubject) {
		t.Fatalf("Action actor at cursor %d = %+v/%+v/%+v/%+v/%+v, want %q/%q/%q/%q/%q",
			cursor, kind, namespace, name, issuer, subject,
			wantKind, wantNamespace, wantName, wantIssuer, wantSubject)
	}
}

func matchesNullable(value sql.NullString, want string) bool {
	if want == "" {
		return !value.Valid
	}
	return value.Valid && value.String == want
}

func assertLegacyEntryStillPresent(t *testing.T, ctx context.Context, db *sql.DB, inboxID string, cursor int) {
	t.Helper()
	var event, payload []byte
	if err := db.QueryRowContext(ctx, `SELECT event, payload FROM entry WHERE inbox_id = $1 AND cursor = $2`, inboxID, cursor).
		Scan(&event, &payload); err != nil {
		t.Fatalf("read legacy row after rejected upgrade: %v", err)
	}
	assertJSON(t, event, `{"provider":"actions","request_id":"`+fixtureRequestID+`","sequence":1}`)
	assertJSON(t, payload, `{"actor":null,"at":"2026-10-20T03:56:03Z","sequence":1,"state":"future_state"}`)
}

func assertColumn(t *testing.T, ctx context.Context, db *sql.DB, table, column string, want bool) {
	t.Helper()
	var got bool
	if err := db.QueryRowContext(ctx, `SELECT EXISTS (
		SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = $1 AND column_name = $2
	)`, table, column).Scan(&got); err != nil {
		t.Fatalf("inspect %s.%s: %v", table, column, err)
	}
	if got != want {
		t.Fatalf("column %s.%s exists = %t, want %t", table, column, got, want)
	}
}

func readJSON(t *testing.T, ctx context.Context, db *sql.DB, query string, args ...any) []byte {
	t.Helper()
	var raw []byte
	if err := db.QueryRowContext(ctx, query, args...).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	return raw
}

func assertJSON(t *testing.T, got []byte, want string) {
	t.Helper()
	var gotValue, wantValue any
	if err := json.Unmarshal(got, &gotValue); err != nil {
		t.Fatalf("decode JSON %q: %v", got, err)
	}
	if err := json.Unmarshal([]byte(want), &wantValue); err != nil {
		t.Fatalf("decode expected JSON %q: %v", want, err)
	}
	if !reflect.DeepEqual(gotValue, wantValue) {
		t.Fatalf("JSON = %s, want %s", got, want)
	}
}
