package store

import (
	"bytes"
	"context"
	"database/sql"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
	"github.com/lib/pq"
)

var nativeContentContractConstraints = []string{
	"entry_action_content_source",
	"entry_mergeability_content_source",
	"entry_message_content_source",
	"entry_action_content_shape",
	"entry_mergeability_content_shape",
	"entry_message_content_shape",
	"entry_message_body_bytes",
	"entry_message_from_session_length",
	"entry_action_actor_length",
	"entry_owned_legacy_content_absent",
}

func TestNativeContentContractFreshEmptyDatabaseValidatesAndRetiresCleanup(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	resetNotificationSchema(t, db)
	if err := migrations.UpTo(ctx, db, 22); err != nil {
		t.Fatalf("build predecessor schema through migration 00022: %v", err)
	}
	if got := countQuery(t, ctx, db, `SELECT count(*) FROM entry`); got != 0 {
		t.Fatalf("fresh entry table has %d rows, want none", got)
	}
	assertNativeContentContractProofs(t, db, 10, 1)

	if err := migrations.Up(ctx, db); err != nil {
		t.Fatalf("apply native-content contract to fresh database: %v", err)
	}
	assertGooseVersionApplied(t, db, 23)
	assertTableExists(t, db, "notification_entry_legacy_cleanup", false)
	assertColumnExists(t, db, "entry", "event", false)
	assertColumnExists(t, db, "entry", "payload", true)
	assertColumnExists(t, db, "subscription", "creation_proto", true)
	assertColumnExists(t, db, "notice", "runner_entry", true)
	assertNativeContentContractProofs(t, db, 10, 10)
}

func TestNativeContentContractRefusesPopulatedDatabaseWithoutAllProofs(t *testing.T) {
	tests := []struct {
		name      string
		mutate    func(*testing.T, *sql.DB)
		wantError string
	}{
		{
			name: "missing content proof",
			mutate: func(t *testing.T, db *sql.DB) {
				dropNativeContentContractConstraint(t, db, "entry_action_content_shape")
			},
			wantError: "requires all ten native-content constraints",
		},
		{
			name: "unvalidated legacy absence proof",
			mutate: func(t *testing.T, db *sql.DB) {
				dropNativeContentContractConstraint(t, db, "entry_owned_legacy_content_absent")
				if _, err := db.ExecContext(t.Context(), `ALTER TABLE entry ADD CONSTRAINT entry_owned_legacy_content_absent
					CHECK (identity_provider IS NULL OR identity_provider NOT IN ('actions', 'github_mergeability', 'message') OR payload IS NULL) NOT VALID`); err != nil {
					t.Fatalf("replace proof with NOT VALID check: %v", err)
				}
			},
			wantError: "requires all ten native-content constraints to be validated",
		},
		{
			name: "cleanup is not terminal",
			mutate: func(t *testing.T, db *sql.DB) {
				if _, err := db.ExecContext(t.Context(), `UPDATE notification_entry_legacy_cleanup SET phase = 'scanned' WHERE singleton`); err != nil {
					t.Fatalf("set incomplete cleanup phase: %v", err)
				}
			},
			wantError: "cleanup phase is scanned, want cleaned",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			db, ctx, f, subscription := newNativeContentContractDatabase(t)
			seedNativeActionContentContractEntry(t, f, subscription)
			prepareNativeContentContractProofs(t, db, "cleaned")
			test.mutate(t, db)
			before := f.read(t, subscription.InboxID, 0)
			var proofCountBefore, validatedProofCountBefore int
			if err := db.QueryRowContext(ctx, `SELECT count(*), count(*) FILTER (WHERE convalidated)
				FROM pg_constraint WHERE conrelid = 'entry'::regclass AND conname = ANY($1::text[])`,
				pq.Array(nativeContentContractConstraints)).Scan(&proofCountBefore, &validatedProofCountBefore); err != nil {
				t.Fatal(err)
			}
			var phaseBefore string
			if err := db.QueryRowContext(ctx, `SELECT phase FROM notification_entry_legacy_cleanup WHERE singleton`).Scan(&phaseBefore); err != nil {
				t.Fatal(err)
			}

			err := migrations.Up(ctx, db)
			if err == nil || !strings.Contains(err.Error(), test.wantError) {
				t.Fatalf("contract migration error = %v, want %q", err, test.wantError)
			}
			assertLatestGooseVersion(t, db, 22)
			assertTableExists(t, db, "notification_entry_legacy_cleanup", true)
			assertColumnExists(t, db, "entry", "payload", true)
			assertColumnExists(t, db, "entry", "event", false)
			assertNativeContentContractProofs(t, db, proofCountBefore, validatedProofCountBefore)
			var phaseAfter string
			if err := db.QueryRowContext(ctx, `SELECT phase FROM notification_entry_legacy_cleanup WHERE singleton`).Scan(&phaseAfter); err != nil || phaseAfter != phaseBefore {
				t.Fatalf("cleanup phase after rejected contract = %q, err = %v; want %q", phaseAfter, err, phaseBefore)
			}
			after := f.read(t, subscription.InboxID, 0)
			assertNativeContentContractPage(t, after, before, 1)
		})
	}
}

func TestNativeContentContractRetainsNativeContentAndBinarySnapshots(t *testing.T) {
	db, ctx, f, subscription := newNativeContentContractDatabase(t)
	payloads := appendNativeContentContractEntries(t, f, subscription)
	_, runnerEntry := goldenRunnerEntry(t, "origin")
	if _, err := db.ExecContext(ctx, `INSERT INTO notice
		(inbox_id, command_id, through_cursor, text, attempted, admitted, confirmed, runner_cursor, runner_entry)
		VALUES ($1, $2, 4, 'notice', false, false, false, 4, $3)`,
		subscription.InboxID, uuid.New(), runnerEntry); err != nil {
		t.Fatalf("seed runner notice checkpoint: %v", err)
	}
	var creationProtoBefore []byte
	if err := db.QueryRowContext(ctx, `SELECT creation_proto FROM subscription WHERE id = $1`, subscription.ID).Scan(&creationProtoBefore); err != nil {
		t.Fatal(err)
	}
	var runnerEntryBefore []byte
	if err := db.QueryRowContext(ctx, `SELECT runner_entry FROM notice WHERE inbox_id = $1`, subscription.InboxID).Scan(&runnerEntryBefore); err != nil {
		t.Fatal(err)
	}
	if len(creationProtoBefore) == 0 || len(runnerEntryBefore) == 0 {
		t.Fatal("expected populated subscription and runner protobuf snapshots before contract")
	}
	before := f.read(t, subscription.InboxID, 0)
	prepareNativeContentContractProofs(t, db, "cleaned")

	if err := migrations.Up(ctx, db); err != nil {
		t.Fatalf("apply native-content contract after cleanup proofs: %v", err)
	}
	assertGooseVersionApplied(t, db, 23)
	assertTableExists(t, db, "notification_entry_legacy_cleanup", false)
	assertColumnExists(t, db, "entry", "payload", true)
	assertColumnExists(t, db, "entry", "event", false)
	assertNativeContentContractProofs(t, db, 10, 10)

	after := f.read(t, subscription.InboxID, 0)
	assertNativeContentContractPage(t, after, before, len(payloads))
	for i, want := range payloads {
		sameJSON(t, after.Entries[i].Payload, []byte(want))
	}
	if got := countQuery(t, ctx, db, `SELECT count(*) FROM entry WHERE identity_provider IN ('actions', 'github_mergeability', 'message') AND payload IS NOT NULL`); got != 0 {
		t.Fatalf("owned entries with legacy JSON payload = %d, want none", got)
	}
	if got := countQuery(t, ctx, db, `SELECT count(*) FROM entry WHERE identity_provider = 'github' AND payload IS NOT NULL`); got != 1 {
		t.Fatalf("GitHub webhook JSON payload rows = %d, want one", got)
	}
	for _, column := range []struct{ name, condition string }{
		{"Action", "action_at IS NOT NULL"},
		{"mergeability", "mergeability_observed_at IS NOT NULL"},
		{"Message", "message_body IS NOT NULL"},
	} {
		if got := countQuery(t, ctx, db, `SELECT count(*) FROM entry WHERE `+column.condition); got != 1 {
			t.Fatalf("%s native content rows = %d, want one", column.name, got)
		}
	}

	var creationProtoAfter, runnerEntryAfter []byte
	if err := db.QueryRowContext(ctx, `SELECT creation_proto FROM subscription WHERE id = $1`, subscription.ID).Scan(&creationProtoAfter); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRowContext(ctx, `SELECT runner_entry FROM notice WHERE inbox_id = $1`, subscription.InboxID).Scan(&runnerEntryAfter); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(creationProtoAfter, creationProtoBefore) || !bytes.Equal(runnerEntryAfter, runnerEntryBefore) {
		t.Fatalf("binary snapshots changed: subscription=%t runner=%t",
			bytes.Equal(creationProtoAfter, creationProtoBefore), bytes.Equal(runnerEntryAfter, runnerEntryBefore))
	}

	// The retired command reads this checkpoint before it can update entries. Its first database access now fails.
	var stalePhase string
	if err := db.QueryRowContext(ctx, `SELECT phase FROM notification_entry_legacy_cleanup WHERE singleton`).Scan(&stalePhase); err == nil {
		t.Fatal("retired cleanup checkpoint remains queryable")
	}
	if got := countQuery(t, ctx, db, `SELECT count(*) FROM entry`); got != len(payloads) {
		t.Fatalf("entries after stale cleanup checkpoint read = %d, want %d", got, len(payloads))
	}
}

func newNativeContentContractDatabase(t *testing.T) (*sql.DB, context.Context, fixture, Subscription) {
	t.Helper()
	db, ctx := newStorageIntegrationDatabase(t)
	resetNotificationSchema(t, db)
	if err := migrations.UpTo(ctx, db, 22); err != nil {
		t.Fatalf("build predecessor schema through migration 00022: %v", err)
	}
	f := fixtureForDatabase(t, db, defaultQuotas())
	subscription := f.subscribe(t, body("native-content-contract"))
	return db, ctx, f, subscription
}

func seedNativeActionContentContractEntry(t *testing.T, f fixture, subscription Subscription) {
	t.Helper()
	action := model.ActionsEvent{RequestID: subscription.Source.(model.ActionsSource).RequestID, Sequence: 1}
	f.mustAppendEvent(t, subscription.InboxID, subscription.ID, action, entryPayloadForTest(t, action))
}

func appendNativeContentContractEntries(t *testing.T, f fixture, subscription Subscription) []string {
	t.Helper()
	requestID := subscription.Source.(model.ActionsSource).RequestID
	action := model.ActionsEvent{RequestID: requestID, Sequence: 1}
	webhookAction := "opened"
	entries := []struct {
		event   model.EventIdentity
		payload string
	}{
		{action, entryPayloadForTest(t, action)},
		{model.GitHubMergeabilityEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 7, Sequence: 2}, ""},
		{model.MessageEvent{MessageID: uuid.New(), Sender: model.ServiceAccount{Namespace: "sender", Name: "agent"}}, ""},
		{model.GitHubEvent{AppID: 1, DeliveryID: uuid.New(), RepositoryID: 2, Event: model.EventPullRequest, Action: &webhookAction}, `{"action":"opened","pull_request":{"number":7}}`},
	}
	payloads := make([]string, len(entries))
	for i, entry := range entries {
		if entry.payload == "" {
			entry.payload = entryPayloadForTest(t, entry.event)
		}
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, entry.event, entry.payload)
		payloads[i] = entry.payload
	}
	return payloads
}

func prepareNativeContentContractProofs(t *testing.T, db *sql.DB, phase string) {
	t.Helper()
	for _, constraint := range nativeContentContractConstraints {
		if _, err := db.ExecContext(t.Context(), `ALTER TABLE entry VALIDATE CONSTRAINT `+constraint); err != nil {
			t.Fatalf("validate %s: %v", constraint, err)
		}
	}
	if _, err := db.ExecContext(t.Context(), `UPDATE notification_entry_legacy_cleanup SET phase = $1 WHERE singleton`, phase); err != nil {
		t.Fatalf("set cleanup phase %q: %v", phase, err)
	}
}

func dropNativeContentContractConstraint(t *testing.T, db *sql.DB, constraint string) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), `ALTER TABLE entry DROP CONSTRAINT `+constraint); err != nil {
		t.Fatalf("drop %s: %v", constraint, err)
	}
}

func assertNativeContentContractProofs(t *testing.T, db *sql.DB, wantCount, wantValidated int) {
	t.Helper()
	var count, validated int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*), count(*) FILTER (WHERE convalidated)
		FROM pg_constraint WHERE conrelid = 'entry'::regclass AND conname = ANY($1::text[])`,
		pq.Array(nativeContentContractConstraints)).Scan(&count, &validated); err != nil {
		t.Fatal(err)
	}
	if count != wantCount || validated != wantValidated {
		t.Fatalf("native-content constraints = %d (%d validated), want %d (%d validated)", count, validated, wantCount, wantValidated)
	}
}

func assertNativeContentContractPage(t *testing.T, got, want InboxPage, entryCount int) {
	t.Helper()
	if len(got.Entries) != entryCount || len(want.Entries) != entryCount {
		t.Fatalf("entry counts after contract = %d (before %d), want %d", len(got.Entries), len(want.Entries), entryCount)
	}
	for i := range want.Entries {
		requireEqual(t, got.Entries[i].Cursor, want.Entries[i].Cursor)
		requireEqual(t, got.Entries[i].Event, want.Entries[i].Event)
		sameJSON(t, got.Entries[i].Payload, want.Entries[i].Payload)
		requireEqual(t, got.Entries[i].SubscriptionIDs, want.Entries[i].SubscriptionIDs)
	}
}
