package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/entrystoragepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
)

func TestEventIdentityProviderProofIsValidatedOfflineAndConstrainsNewWrites(t *testing.T) {
	db, ctx := newStorageIntegrationDatabase(t)
	resetNotificationSchema(t, db)
	if err := migrations.UpTo(ctx, db, 19); err != nil {
		t.Fatalf("build schema through event identity writer migration 00019: %v", err)
	}
	f := fixtureForDatabase(t, db, defaultQuotas())
	subscription := f.subscribe(t, body("event-identity-proof"))
	event := model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}
	insertHistoricalActionEntry(t, db, subscription.InboxID, 1, event)

	var validated bool
	if err := f.db.QueryRowContext(t.Context(), `SELECT convalidated FROM pg_constraint
		WHERE conrelid = 'entry'::regclass AND conname = 'entry_identity_provider_required'`).Scan(&validated); err != nil {
		t.Fatal(err)
	}
	if validated {
		t.Fatal("migration validated the identity provider proof on the serving path")
	}
	_, err := f.db.ExecContext(t.Context(), `UPDATE entry SET identity_provider = NULL WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID)
	var postgresError *pq.Error
	if !errors.As(err, &postgresError) || postgresError.Constraint != "entry_identity_provider_required" {
		t.Fatalf("writing an incomplete relational identity returned %v, want entry_identity_provider_required", err)
	}
	if err := ValidateEntryIdentityProviderConstraint(t.Context(), f.db); err != nil {
		t.Fatalf("validate provider proof in the offline validator: %v", err)
	}
	if err := f.db.QueryRowContext(t.Context(), `SELECT convalidated FROM pg_constraint
		WHERE conrelid = 'entry'::regclass AND conname = 'entry_identity_provider_required'`).Scan(&validated); err != nil {
		t.Fatal(err)
	}
	if !validated {
		t.Fatal("offline validation did not publish the provider proof")
	}
}

func TestAppendEventDualWritesEverySourceIdentityWithoutCollapsingGitHubAction(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("identity-columns"))
	requestID := uuid.New()
	deliveryID := uuid.New()
	messageID := uuid.New()
	empty := ""
	opened := "opened"
	events := []model.EventIdentity{
		model.ActionsEvent{RequestID: requestID, Sequence: 17},
		model.GitHubEvent{AppID: 1, DeliveryID: deliveryID, RepositoryID: 2, Event: model.EventPullRequest},
		model.GitHubEvent{AppID: 1, DeliveryID: uuid.New(), RepositoryID: 2, Event: model.EventPullRequest, Action: &empty},
		model.GitHubEvent{AppID: 1, DeliveryID: uuid.New(), RepositoryID: 2, Event: model.EventPullRequest, Action: &opened},
		model.GitHubObservationEvent{AppID: 1, RepositoryID: 2, Event: model.EventCheckRun, Sequence: 3},
		model.GitHubMergeabilityEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Sequence: 4},
		model.GitHubMergeabilitySnapshotEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Version: 5},
		model.MessageEvent{MessageID: messageID, Sender: model.ServiceAccount{Namespace: "sender-ns", Name: "sender"}},
	}
	for _, event := range events {
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))
	}
	rows, err := f.db.QueryContext(t.Context(), `SELECT identity_provider,
		actions_request_id::text, actions_sequence,
		github_app_id, github_delivery_id::text, github_repository_id, github_event,
		github_action, github_pull_request_number,
		github_mergeability_sequence, github_snapshot_version, github_observation_sequence,
		message_id::text, message_sender_namespace, message_sender_name
		FROM entry WHERE inbox_id = $1 ORDER BY cursor`, subscription.InboxID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	type identityColumns struct {
		provider                                                                                                               string
		actionsSequence, githubAppID, githubRepositoryID, prNumber, mergeabilitySequence, snapshotVersion, observationSequence sql.NullInt64
		actionsRequestID, githubDeliveryID, githubEvent, githubAction                                                          sql.NullString
		messageID, messageSenderNamespace, messageSenderName                                                                   sql.NullString
	}
	var got []identityColumns
	for rows.Next() {
		var row identityColumns
		if err := rows.Scan(&row.provider, &row.actionsRequestID, &row.actionsSequence,
			&row.githubAppID, &row.githubDeliveryID, &row.githubRepositoryID, &row.githubEvent,
			&row.githubAction, &row.prNumber,
			&row.mergeabilitySequence, &row.snapshotVersion, &row.observationSequence,
			&row.messageID, &row.messageSenderNamespace, &row.messageSenderName); err != nil {
			t.Fatal(err)
		}
		got = append(got, row)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if len(got) != len(events) {
		t.Fatalf("stored identities = %d, want %d", len(got), len(events))
	}
	if got[0].provider != "actions" || !got[0].actionsRequestID.Valid || got[0].actionsRequestID.String != requestID.String() ||
		got[0].actionsSequence.Int64 != 17 || !got[0].actionsSequence.Valid {
		t.Errorf("Actions identity columns = %+v", got[0])
	}
	for i, wantAction := range []sql.NullString{{}, {String: "", Valid: true}, {String: "opened", Valid: true}} {
		row := got[i+1]
		if row.provider != "github" || row.githubAction.Valid != wantAction.Valid || row.githubAction.String != wantAction.String {
			t.Errorf("GitHub identity %d columns = %+v", i, row)
		}
	}
	if got[4].provider != "github_observation" || got[4].githubAppID.Int64 != 1 ||
		got[4].githubRepositoryID.Int64 != 2 || got[4].githubEvent.String != string(model.EventCheckRun) ||
		got[4].observationSequence.Int64 != 3 || got[4].githubDeliveryID.Valid {
		t.Errorf("GitHub observation identity columns = %+v", got[4])
	}
	if got[5].provider != "github_mergeability" || got[5].githubAppID.Int64 != 1 ||
		got[5].githubRepositoryID.Int64 != 2 || got[5].prNumber.Int64 != 3 || got[5].mergeabilitySequence.Int64 != 4 ||
		got[5].snapshotVersion.Valid {
		t.Errorf("mergeability sequence columns = %+v", got[5])
	}
	if got[6].provider != "github_mergeability" || got[6].githubAppID.Int64 != 1 ||
		got[6].githubRepositoryID.Int64 != 2 || got[6].prNumber.Int64 != 3 || got[6].snapshotVersion.Int64 != 5 ||
		got[6].mergeabilitySequence.Valid {
		t.Errorf("mergeability snapshot columns = %+v", got[6])
	}
	if got[7].provider != "message" || got[7].messageID.String != messageID.String() ||
		got[7].messageSenderNamespace.String != "sender-ns" || got[7].messageSenderName.String != "sender" {
		t.Errorf("message identity columns = %+v", got[7])
	}
	for i, wantNull := range []bool{false, true, true, true, true, false, false, false} {
		var legacyPayloadNull bool
		if err := f.db.QueryRowContext(t.Context(), `SELECT payload IS NULL
			FROM entry WHERE inbox_id = $1 AND cursor = $2`, subscription.InboxID, i+1).Scan(&legacyPayloadNull); err != nil {
			t.Fatal(err)
		}
		wantLegacyPayloadNull := i == 0 || i == 5 || i == 6
		if legacyPayloadNull != wantLegacyPayloadNull {
			t.Errorf("event %d legacy JSON payload is NULL = %t, want %t", i, legacyPayloadNull, wantLegacyPayloadNull)
		}
		var isNull bool
		var encoded []byte
		if err := f.db.QueryRowContext(t.Context(), `SELECT typed_payload IS NULL, typed_payload
			FROM entry WHERE inbox_id = $1 AND cursor = $2`, subscription.InboxID, i+1).Scan(&isNull, &encoded); err != nil {
			t.Fatal(err)
		}
		if isNull != wantNull {
			t.Errorf("event %d typed_payload IS NULL = %t, want %t", i, isNull, wantNull)
			continue
		}
		if wantNull {
			if _, ok := events[i].(model.GitHubObservationEvent); ok {
				var payload []byte
				if err := f.db.QueryRowContext(t.Context(), `SELECT payload FROM entry WHERE inbox_id = $1 AND cursor = $2`,
					subscription.InboxID, i+1).Scan(&payload); err != nil {
					t.Fatal(err)
				}
				var native map[string]any
				if err := json.Unmarshal(payload, &native); err != nil {
					t.Fatalf("decode native GitHub observation payload: %v", err)
				}
				if native["repositoryId"] != float64(2) || native["headSha"] != "0123456789abcdef0123456789abcdef01234567" {
					t.Errorf("native GitHub observation payload = %s", payload)
				}
			}
			continue
		}
		var content entrystoragepb.EntryContent
		if err := proto.Unmarshal(encoded, &content); err != nil {
			t.Fatalf("event %d typed payload is not protobuf: %v", i, err)
		}
		switch event := events[i].(type) {
		case model.ActionsEvent:
			action := content.GetAction()
			wantAt := time.Date(2026, time.January, 1, 0, 0, 0, 0, time.UTC)
			wantActor := &action_servicepb.Principal{Principal: &action_servicepb.Principal_Operator{Operator: &action_servicepb.Operator{
				Issuer: "https://issuer.example", Subject: "subject-123",
			}}}
			if action == nil || action.GetState() != action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING ||
				action.GetAt() == nil || !action.GetAt().AsTime().Equal(wantAt) || !proto.Equal(action.GetActor(), wantActor) {
				t.Errorf("Action typed content = %v", content.GetAction())
			}
		case model.GitHubMergeabilityEvent, model.GitHubMergeabilitySnapshotEvent:
			mergeability := content.GetMergeability()
			if mergeability == nil || mergeability.GetRepository() != "owner/repository" ||
				mergeability.GetPreviousMergeability() != entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_UNKNOWN ||
				mergeability.GetMergeability() != entrystoragepb.GitHubMergeabilityState_GITHUB_MERGEABILITY_STATE_CONFLICTING {
				t.Errorf("mergeability typed content = %v", mergeability)
			}
		case model.MessageEvent:
			message := content.GetMessage()
			if message == nil || message.GetBody() != "stored message" || message.FromSession == nil || *message.FromSession != "sender-session" {
				t.Errorf("message typed content = %v", message)
			}
		default:
			t.Fatalf("unexpected event in typed content test: %T", event)
		}
	}
}

func TestEventReadersAndReplayDedupUseRelationalIdentity(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("identity-runtime"))
	requestID := uuid.New()
	deliveryID := uuid.New()
	messageID := uuid.New()
	empty := ""
	events := []model.EventIdentity{
		model.ActionsEvent{RequestID: requestID, Sequence: 17},
		model.ActionsEvent{RequestID: requestID, Sequence: 18},
		model.GitHubEvent{AppID: 1, DeliveryID: deliveryID, RepositoryID: 2, Event: model.EventPullRequest},
		model.GitHubEvent{AppID: 1, DeliveryID: uuid.New(), RepositoryID: 2, Event: model.EventPullRequest, Action: &empty},
		model.GitHubObservationEvent{AppID: 1, RepositoryID: 2, Event: model.EventCheckRun, Sequence: 19},
		model.GitHubMergeabilityEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Sequence: 4},
		model.GitHubMergeabilitySnapshotEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Version: 5},
		model.MessageEvent{MessageID: messageID, Sender: model.ServiceAccount{Namespace: "sender-ns", Name: "sender"}},
	}
	for _, event := range events {
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))
	}
	// Make one legacy copy disagree with the relational identity so this test proves that new reads and retries
	// use the populated columns. Production writers still dual-write the correct JSON for old readers in rollout.
	legacyEvent, err := json.Marshal(model.ActionsEvent{RequestID: requestID, Sequence: 999})
	if err != nil {
		t.Fatal(err)
	}
	f.exec(t, `UPDATE entry SET event = $3::jsonb WHERE inbox_id = $1 AND cursor = $2`, subscription.InboxID, 1, string(legacyEvent))
	page := f.read(t, subscription.InboxID, 0)
	if len(page.Entries) != len(events) {
		t.Fatalf("read %d entries, want %d", len(page.Entries), len(events))
	}
	for i, event := range events {
		requireEqual(t, page.Entries[i].Event, event)
	}

	// An expired payload remains deduplicated by its permanent identity. A replay does not create an entry or
	// restore content after cleanup has advanced the expired prefix.
	f.exec(t, `UPDATE entry SET payload = NULL WHERE inbox_id = $1 AND cursor = 1`, subscription.InboxID)
	f.exec(t, `UPDATE inbox SET expired_through = 1 WHERE id = $1`, subscription.InboxID)
	for _, event := range events {
		if err := f.transaction(t.Context(), func(tx *sql.Tx) error {
			return f.AppendEvent(t.Context(), tx, subscription.InboxID, subscription.ID, event, []byte(`{"retry":true}`))
		}); err != nil {
			t.Fatal(err)
		}
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1`, subscription.InboxID); got != len(events) {
		t.Fatalf("replay left %d entries, want %d", got, len(events))
	}
	if got := f.count(t, `SELECT count(*) FROM entry WHERE inbox_id = $1 AND cursor = 1 AND payload IS NULL`, subscription.InboxID); got != 1 {
		t.Fatalf("expired payload rows = %d, want 1 after replay", got)
	}

	tx, err := f.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	summaries, err := pendingEntrySummaries(t.Context(), tx, subscription.InboxID, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(summaries) != len(events)-1 || summaries[0].Summary != "Action "+requestID.String()+" · event 18 · decision_pending" {
		t.Fatalf("pending summaries from relational identity = %+v", summaries)
	}
}
