package store

import (
	"database/sql"
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/google/uuid"
	"github.com/lib/pq"
)

func TestNativeEntryContentRejectsIncompleteAndWrongSourceRows(t *testing.T) {
	f := newEventIdentityWriterOnlyFixture(t)
	subscription := f.subscribe(t, body("native-content-shape"))
	action := model.ActionsEvent{RequestID: uuid.New(), Sequence: 1}
	mergeability := model.GitHubMergeabilityEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Sequence: 1}
	message := model.MessageEvent{MessageID: uuid.New(), Sender: model.ServiceAccount{Namespace: "sender", Name: "account"}}
	for _, event := range []model.EventIdentity{action, mergeability, message} {
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, entryPayloadForTest(t, event))
	}
	checks := []struct {
		name, update, constraint string
		cursor                   int
	}{
		{"null provider with content", `UPDATE entry SET identity_provider = NULL WHERE inbox_id = $1 AND cursor = $2`, "", 1},
		{"wrong source", `UPDATE entry SET message_body = 'hello' WHERE inbox_id = $1 AND cursor = $2`, "entry_message_content_source", 1},
		{"null actor kind", `UPDATE entry SET action_actor_kind = NULL, action_actor_namespace = 'ns', action_actor_name = 'name', action_actor_issuer = NULL, action_actor_subject = NULL WHERE inbox_id = $1 AND cursor = $2`, "entry_action_content_shape", 1},
		{"empty caller namespace", `UPDATE entry SET action_actor_kind = 'caller', action_actor_namespace = '', action_actor_name = 'name', action_actor_issuer = NULL, action_actor_subject = NULL WHERE inbox_id = $1 AND cursor = $2`, "entry_action_actor_length", 1},
		{"null mergeability residual", `UPDATE entry SET mergeability_observed_at_submicro_nanos = NULL WHERE inbox_id = $1 AND cursor = $2`, "entry_mergeability_content_shape", 2},
		{"null mergeability state", `UPDATE entry SET mergeability_previous = NULL WHERE inbox_id = $1 AND cursor = $2`, "entry_mergeability_content_shape", 2},
		{"partial merge commit", `UPDATE entry SET merge_commit_sha = 'sha' WHERE inbox_id = $1 AND cursor = $2`, "entry_mergeability_content_shape", 2},
		{"oversize message", `UPDATE entry SET message_body = repeat('x', 262145) WHERE inbox_id = $1 AND cursor = $2`, "entry_message_body_bytes", 3},
		{"long source session", `UPDATE entry SET message_from_session = repeat('x', 201) WHERE inbox_id = $1 AND cursor = $2`, "entry_message_from_session_length", 3},
	}
	for _, check := range checks {
		t.Run(check.name, func(t *testing.T) {
			_, err := f.db.ExecContext(t.Context(), check.update, subscription.InboxID, check.cursor)
			var refused *pq.Error
			if !errors.As(err, &refused) || string(refused.Code) != "23514" || (check.constraint != "" && string(refused.Constraint) != check.constraint) {
				t.Fatalf("update error = %v, want constraint %s", err, check.constraint)
			}
		})
	}
}

func TestAppendEventWritesEverySourceIdentityWithoutCollapsingGitHubAction(t *testing.T) {
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
		var content storagecodec.EntryContentStorage
		if err := f.db.QueryRowContext(t.Context(), `SELECT `+storagecodec.EntryContentReadColumns+`
			FROM entry WHERE inbox_id = $1 AND cursor = $2`, subscription.InboxID, i+1).Scan(content.Destinations()...); err != nil {
			t.Fatal(err)
		}
		if wantNull {
			if content.ActionAt != nil || content.MergeabilityObservedAt != nil || content.MessageBody != nil {
				t.Errorf("event %d unexpectedly has native content: %+v", i, content)
			}
			continue
		}
		switch events[i].(type) {
		case model.ActionsEvent:
			if content.ActionAt == nil || content.ActionActorKind == nil || *content.ActionActorKind != "operator" ||
				content.ActionActorIssuer == nil || *content.ActionActorIssuer != "https://issuer.example" {
				t.Errorf("Action native content = %+v", content)
			}
		case model.GitHubMergeabilityEvent, model.GitHubMergeabilitySnapshotEvent:
			if content.MergeabilityRepository == nil || *content.MergeabilityRepository != "owner/repository" ||
				content.MergeabilityPrevious == nil || *content.MergeabilityPrevious != "UNKNOWN" ||
				content.MergeabilityCurrent == nil || *content.MergeabilityCurrent != "CONFLICTING" {
				t.Errorf("mergeability native content = %+v", content)
			}
		case model.MessageEvent:
			if content.MessageBody == nil || *content.MessageBody != "stored message" || content.MessageFromSession == nil || *content.MessageFromSession != "sender-session" {
				t.Errorf("message native content = %+v", content)
			}
		default:
			t.Fatalf("unexpected event in native content test: %T", events[i])
		}
	}
}

func TestEventReadersAndReplayDedupUseRelationalIdentity(t *testing.T) {
	f := newEventIdentityWriterOnlyFixture(t)
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
	// use the populated columns even before init drops the legacy column.
	legacyEvent, err := marshalHistoricalEventIdentity(model.ActionsEvent{RequestID: requestID, Sequence: 999})
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
