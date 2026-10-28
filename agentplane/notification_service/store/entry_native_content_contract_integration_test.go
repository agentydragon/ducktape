package store

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"reflect"
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

func TestFinalSchemaRetainsNativeContentAndBinarySnapshots(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	f := fixtureForDatabase(t, db, defaultQuotas())
	subscription := f.subscribe(t, body("native-content-final-contract"))
	payloads := appendNativeContentContractEntries(t, f, subscription)
	_, runnerEntry := goldenRunnerEntry(t, "origin")
	if _, err := db.ExecContext(ctx, `INSERT INTO notice
		(inbox_id, command_id, through_cursor, text, attempted, admitted, confirmed, runner_cursor, runner_entry)
		VALUES ($1, $2, 4, 'notice', false, false, false, 4, $3)`, subscription.InboxID, uuid.New(), runnerEntry); err != nil {
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
		t.Fatal("expected populated subscription and runner protobuf snapshots")
	}
	before := f.read(t, subscription.InboxID, 0)
	assertNativeContentContractProofs(t, db)
	assertColumnExists(t, db, "entry", "payload", true)
	assertColumnExists(t, db, "subscription", "creation_proto", true)
	assertColumnExists(t, db, "notice", "runner_entry", true)

	if err := migrations.Up(ctx, db); err != nil {
		t.Fatalf("repeat full migration on the completed schema: %v", err)
	}
	assertNativeContentContractProofs(t, db)
	after := f.read(t, subscription.InboxID, 0)
	assertNativeContentContractPage(t, after, before)
	if len(after.Entries) != len(payloads) {
		t.Fatalf("entries after repeat migration = %d, want %d", len(after.Entries), len(payloads))
	}
	var gotPayload, wantPayload any
	if err := json.Unmarshal(after.Entries[3].Payload, &gotPayload); err != nil {
		t.Fatalf("decode retained GitHub webhook payload %s: %v", after.Entries[3].Payload, err)
	}
	if err := json.Unmarshal([]byte(payloads[3]), &wantPayload); err != nil {
		t.Fatalf("decode seeded GitHub webhook payload %s: %v", payloads[3], err)
	}
	if !reflect.DeepEqual(gotPayload, wantPayload) {
		t.Fatalf("retained GitHub webhook payload = %s, want JSON value %#v", after.Entries[3].Payload, wantPayload)
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

func assertNativeContentContractProofs(t *testing.T, db *sql.DB) {
	t.Helper()
	var count, validated int
	if err := db.QueryRowContext(t.Context(), `SELECT count(*), count(*) FILTER (WHERE convalidated)
		FROM pg_constraint WHERE conrelid = 'entry'::regclass AND conname = ANY($1::text[])`,
		pq.Array(nativeContentContractConstraints)).Scan(&count, &validated); err != nil {
		t.Fatal(err)
	}
	if count != len(nativeContentContractConstraints) || validated != count {
		t.Fatalf("native-content constraints = %d (%d validated), want %d validated", count, validated, len(nativeContentContractConstraints))
	}
}

func assertNativeContentContractPage(t *testing.T, got, want InboxPage) {
	t.Helper()
	if len(got.Entries) != len(want.Entries) {
		t.Fatalf("entry counts after repeat migration = %d (before %d), want equal", len(got.Entries), len(want.Entries))
	}
	for i := range want.Entries {
		requireEqual(t, got.Entries[i].Cursor, want.Entries[i].Cursor)
		requireEqual(t, got.Entries[i].Event, want.Entries[i].Event)
		if !bytes.Equal(got.Entries[i].Payload, want.Entries[i].Payload) {
			t.Fatalf("entry %d payload changed: %s before, %s after", got.Entries[i].Cursor, want.Entries[i].Payload, got.Entries[i].Payload)
		}
		requireEqual(t, got.Entries[i].SubscriptionIDs, want.Entries[i].SubscriptionIDs)
	}
}
