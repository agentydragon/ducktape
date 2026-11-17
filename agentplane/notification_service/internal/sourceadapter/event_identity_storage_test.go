package sourceadapter

import (
	"database/sql"
	"reflect"
	"strings"
	"testing"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // GitHub and common notification models share a package name.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

func identityText(value string) sql.NullString  { return sql.NullString{String: value, Valid: true} }
func identityInteger(value int64) sql.NullInt64 { return sql.NullInt64{Int64: value, Valid: true} }

func TestEventIdentityFromStorageRoutesOwnedVariants(t *testing.T) {
	requestID, messageID, deliveryID := uuid.New(), uuid.New(), uuid.New()
	for _, test := range []struct {
		name string
		row  storagecodec.EntryIdentityStorage
		want EventIdentity
	}{
		{"Action", storagecodec.EntryIdentityStorage{
			Provider: identityText("actions"), ActionsRequestID: identityText(requestID.String()), ActionsSequence: identityInteger(7),
		}, WrapActionsEvent(model.ActionsEvent{RequestID: requestID, Sequence: 7})},
		{"Message without Identity", storagecodec.EntryIdentityStorage{
			Provider: identityText("message"), MessageID: identityText(messageID.String()),
			MessageSenderNamespace: identityText("ns"), MessageSenderName: identityText("sender"),
		}, WrapMessageEvent(model.MessageEvent{MessageID: messageID, Sender: model.ServiceAccount{Namespace: "ns", Name: "sender"}})},
		{"Message with Identity", storagecodec.EntryIdentityStorage{
			Provider: identityText("message"), MessageID: identityText(messageID.String()),
			MessageSenderNamespace: identityText("ns"), MessageSenderName: identityText("sender"),
			MessageSenderIdentityName: identityText("identity"), MessageSenderIdentityUID: identityText("identity-uid"),
		}, WrapMessageEvent(model.MessageEvent{
			MessageID: messageID, Sender: model.ServiceAccount{Namespace: "ns", Name: "sender"},
			SenderIdentity: &model.Identity{Name: "identity", UID: "identity-uid"},
		})},
		{"webhook", storagecodec.EntryIdentityStorage{
			Provider: identityText("github"), GitHubAppID: identityInteger(1), GitHubRepositoryID: identityInteger(2),
			GitHubDeliveryID: identityText(deliveryID.String()), GitHubEvent: identityText(string(githubmodel.EventPullRequest)),
		}, WrapGitHubEvent(githubmodel.WebhookEvent{AppID: 1, DeliveryID: deliveryID, RepositoryID: 2, Event: githubmodel.EventPullRequest})},
		{"observation", storagecodec.EntryIdentityStorage{
			Provider: identityText("github_observation"), GitHubAppID: identityInteger(1), GitHubRepositoryID: identityInteger(2),
			GitHubEvent: identityText(string(githubmodel.EventCheckRun)), GitHubObservationSequence: identityInteger(3),
		}, WrapGitHubObservationEvent(githubmodel.ObservationEvent{AppID: 1, RepositoryID: 2, Event: githubmodel.EventCheckRun, Sequence: 3})},
		{"mergeability", storagecodec.EntryIdentityStorage{
			Provider: identityText("github_mergeability"), GitHubAppID: identityInteger(1), GitHubRepositoryID: identityInteger(2),
			GitHubPullRequestNumber: identityInteger(3), GitHubMergeabilitySequence: identityInteger(4),
		}, WrapGitHubMergeabilityEvent(githubmodel.MergeabilityEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Sequence: 4})},
		{"snapshot", storagecodec.EntryIdentityStorage{
			Provider: identityText("github_mergeability"), GitHubAppID: identityInteger(1), GitHubRepositoryID: identityInteger(2),
			GitHubPullRequestNumber: identityInteger(3), GitHubSnapshotVersion: identityInteger(5),
		}, WrapGitHubMergeabilitySnapshotEvent(githubmodel.MergeabilitySnapshotEvent{AppID: 1, RepositoryID: 2, PullRequestNumber: 3, Version: 5})},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := EventIdentityFromStorage(test.row)
			if err != nil || !reflect.DeepEqual(got, test.want) {
				t.Fatalf("identity = %#v, %v; want %#v", got, err, test.want)
			}
		})
	}
}

func TestEventIdentityFromStorageRejectsMissingIdentityAndPartialSenderMetadata(t *testing.T) {
	message := storagecodec.EntryIdentityStorage{
		Provider: identityText("message"), MessageID: identityText(uuid.NewString()),
		MessageSenderNamespace: identityText("ns"), MessageSenderName: identityText("sender"),
	}
	nameOnly, uidOnly := message, message
	nameOnly.MessageSenderIdentityName = identityText("identity")
	uidOnly.MessageSenderIdentityUID = identityText("uid")
	for _, test := range []struct {
		row  storagecodec.EntryIdentityStorage
		want string
	}{
		{storagecodec.EntryIdentityStorage{}, "entry has no relational event identity"},
		{storagecodec.EntryIdentityStorage{Provider: identityText("unknown")}, `unknown relational event identity provider "unknown"`},
		{storagecodec.EntryIdentityStorage{Provider: identityText("actions")}, "entry has no actions_request_id"},
		{storagecodec.EntryIdentityStorage{Provider: identityText("actions"), ActionsRequestID: identityText("not-a-uuid")}, "entry actions_request_id:"},
		{nameOnly, "entry has an incomplete message sender Identity"},
		{uidOnly, "entry has an incomplete message sender Identity"},
	} {
		if _, err := EventIdentityFromStorage(test.row); err == nil || !strings.HasPrefix(err.Error(), test.want) {
			t.Fatalf("identity error = %v, want %s", err, test.want)
		}
	}
}
