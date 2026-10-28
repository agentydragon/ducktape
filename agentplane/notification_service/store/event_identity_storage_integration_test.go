package store

import (
	"database/sql"
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
