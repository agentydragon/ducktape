package store

import (
	"database/sql"
	"encoding/json"
	"testing"

	"github.com/google/uuid"
)

func TestAppendEventDualWritesEverySourceIdentityWithoutCollapsingGitHubAction(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("identity-columns"))
	requestID := uuid.New()
	deliveryID := uuid.New()
	messageID := uuid.New()
	empty := ""
	opened := "opened"
	events := []EventIdentity{
		ActionsEvent{RequestID: requestID, Sequence: 17},
		GitHubEvent{AppID: 1, DeliveryID: deliveryID, RepositoryID: 2, Event: EventPullRequest},
		GitHubEvent{AppID: 1, DeliveryID: uuid.New(), RepositoryID: 2, Event: EventPullRequest, Action: &empty},
		GitHubEvent{AppID: 1, DeliveryID: uuid.New(), RepositoryID: 2, Event: EventPullRequest, Action: &opened},
		GitHubMergeabilityEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Sequence: 4},
		GitHubMergeabilitySnapshotEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Version: 5},
		MessageEvent{MessageID: messageID, Sender: ServiceAccount{Namespace: "sender-ns", Name: "sender"}},
	}
	for _, event := range events {
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, `{}`)
	}
	rows, err := f.db.QueryContext(t.Context(), `SELECT identity_provider,
		actions_request_id::text, actions_sequence,
		github_app_id, github_delivery_id::text, github_repository_id, github_event,
		github_action, github_pull_request_number,
		github_mergeability_sequence, github_snapshot_version,
		message_id::text, message_sender_namespace, message_sender_name
		FROM entry WHERE inbox_id = $1 ORDER BY cursor`, subscription.InboxID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	type identityColumns struct {
		provider                                                                                          string
		actionsSequence, githubAppID, githubRepositoryID, prNumber, mergeabilitySequence, snapshotVersion sql.NullInt64
		actionsRequestID, githubDeliveryID, githubEvent, githubAction                                     sql.NullString
		messageID, messageSenderNamespace, messageSenderName                                              sql.NullString
	}
	var got []identityColumns
	for rows.Next() {
		var row identityColumns
		if err := rows.Scan(&row.provider, &row.actionsRequestID, &row.actionsSequence,
			&row.githubAppID, &row.githubDeliveryID, &row.githubRepositoryID, &row.githubEvent,
			&row.githubAction, &row.prNumber,
			&row.mergeabilitySequence, &row.snapshotVersion,
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
	if got[4].provider != "github_mergeability" || got[4].githubAppID.Int64 != 1 ||
		got[4].githubRepositoryID.Int64 != 2 || got[4].prNumber.Int64 != 3 || got[4].mergeabilitySequence.Int64 != 4 ||
		got[4].snapshotVersion.Valid {
		t.Errorf("mergeability sequence columns = %+v", got[4])
	}
	if got[5].provider != "github_mergeability" || got[5].githubAppID.Int64 != 1 ||
		got[5].githubRepositoryID.Int64 != 2 || got[5].prNumber.Int64 != 3 || got[5].snapshotVersion.Int64 != 5 ||
		got[5].mergeabilitySequence.Valid {
		t.Errorf("mergeability snapshot columns = %+v", got[5])
	}
	if got[6].provider != "message" || got[6].messageID.String != messageID.String() ||
		got[6].messageSenderNamespace.String != "sender-ns" || got[6].messageSenderName.String != "sender" {
		t.Errorf("message identity columns = %+v", got[6])
	}
}

func TestEventReadersAndReplayDedupUseRelationalIdentity(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	subscription := f.subscribe(t, body("identity-runtime"))
	requestID := uuid.New()
	deliveryID := uuid.New()
	messageID := uuid.New()
	empty := ""
	events := []EventIdentity{
		ActionsEvent{RequestID: requestID, Sequence: 17},
		ActionsEvent{RequestID: requestID, Sequence: 18},
		GitHubEvent{AppID: 1, DeliveryID: deliveryID, RepositoryID: 2, Event: EventPullRequest},
		GitHubEvent{AppID: 1, DeliveryID: uuid.New(), RepositoryID: 2, Event: EventPullRequest, Action: &empty},
		GitHubMergeabilityEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Sequence: 4},
		GitHubMergeabilitySnapshotEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Version: 5},
		MessageEvent{MessageID: messageID, Sender: ServiceAccount{Namespace: "sender-ns", Name: "sender"}},
	}
	for _, event := range events {
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, `{}`)
	}
	// Make one legacy copy disagree with the relational identity so this test proves that new reads and retries
	// use the populated columns. Production writers still dual-write the correct JSON for old readers in rollout.
	legacyEvent, err := json.Marshal(ActionsEvent{RequestID: requestID, Sequence: 999})
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
	if len(summaries) != len(events)-1 || summaries[0].Summary != "Action "+requestID.String()+" · event 18" {
		t.Fatalf("pending summaries from relational identity = %+v", summaries)
	}
}
