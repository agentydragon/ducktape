package storagecodec

import (
	"database/sql"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/google/uuid"
)

func identityText(value string) sql.NullString  { return sql.NullString{String: value, Valid: true} }
func identityInteger(value int64) sql.NullInt64 { return sql.NullInt64{Int64: value, Valid: true} }

func TestEventIdentityFromStoragePreservesSourceVariants(t *testing.T) {
	deliveryID := uuid.New()
	empty := ""
	webhook := storagecodec.EntryIdentityStorage{
		Provider: identityText("github"), GitHubAppID: identityInteger(1),
		GitHubDeliveryID: identityText(deliveryID.String()), GitHubRepositoryID: identityInteger(2),
		GitHubEvent: identityText(string(model.EventPullRequest)),
	}
	webhookEmptyAction := webhook
	webhookEmptyAction.GitHubAction = identityText("")
	for _, test := range []struct {
		name string
		row  storagecodec.EntryIdentityStorage
		want model.EventIdentity
	}{
		{"webhook NULL action", webhook, model.WebhookEvent{AppID: 1, DeliveryID: deliveryID, RepositoryID: 2, Event: model.EventPullRequest}},
		{"webhook empty action", webhookEmptyAction, model.WebhookEvent{AppID: 1, DeliveryID: deliveryID, RepositoryID: 2, Event: model.EventPullRequest, Action: &empty}},
		{"observation", storagecodec.EntryIdentityStorage{
			Provider: identityText("github_observation"), GitHubAppID: identityInteger(1), GitHubRepositoryID: identityInteger(2),
			GitHubEvent: identityText(string(model.EventCheckRun)), GitHubObservationSequence: identityInteger(3),
		}, model.ObservationEvent{AppID: 1, RepositoryID: 2, Event: model.EventCheckRun, Sequence: 3}},
		{"mergeability", storagecodec.EntryIdentityStorage{
			Provider: identityText("github_mergeability"), GitHubAppID: identityInteger(1), GitHubRepositoryID: identityInteger(2),
			GitHubPullRequestNumber: identityInteger(3), GitHubMergeabilitySequence: identityInteger(4),
		}, model.MergeabilityEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Sequence: 4}},
		{"snapshot", storagecodec.EntryIdentityStorage{
			Provider: identityText("github_mergeability"), GitHubAppID: identityInteger(1), GitHubRepositoryID: identityInteger(2),
			GitHubPullRequestNumber: identityInteger(3), GitHubSnapshotVersion: identityInteger(5),
		}, model.MergeabilitySnapshotEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Version: 5}},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := EventIdentityFromStorage(test.row)
			if err != nil || !reflect.DeepEqual(got, test.want) {
				t.Fatalf("identity = %#v, %v; want %#v", got, err, test.want)
			}
		})
	}
}

func TestEventIdentityFromStorageRejectsAmbiguousMergeabilityAndUnknownEvents(t *testing.T) {
	mergeability := storagecodec.EntryIdentityStorage{
		Provider: identityText("github_mergeability"), GitHubAppID: identityInteger(1), GitHubRepositoryID: identityInteger(2),
		GitHubPullRequestNumber: identityInteger(3),
	}
	both := mergeability
	both.GitHubMergeabilitySequence, both.GitHubSnapshotVersion = identityInteger(4), identityInteger(5)
	for _, row := range []storagecodec.EntryIdentityStorage{mergeability, both} {
		if _, err := EventIdentityFromStorage(row); err == nil || err.Error() != "entry must have exactly one mergeability sequence or snapshot version" {
			t.Fatalf("ambiguous mergeability error = %v", err)
		}
	}
	for _, provider := range []string{"github", "github_observation"} {
		row := storagecodec.EntryIdentityStorage{
			Provider: identityText(provider), GitHubAppID: identityInteger(1), GitHubRepositoryID: identityInteger(2),
			GitHubDeliveryID: identityText(uuid.NewString()), GitHubEvent: identityText("unknown"),
			GitHubObservationSequence: identityInteger(3),
		}
		want := `unknown GitHub event "unknown"`
		if provider == "github_observation" {
			want = `unknown GitHub observation event "unknown"`
		}
		if _, err := EventIdentityFromStorage(row); err == nil || err.Error() != want {
			t.Fatalf("%s unknown event error = %v, want %s", provider, err, want)
		}
	}
}
