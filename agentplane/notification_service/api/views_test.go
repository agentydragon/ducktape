package api

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"google.golang.org/protobuf/proto"
)

func TestMergeabilitySelectorAndIdentityMapThroughTheAPI(t *testing.T) {
	filter := &notificationspb.GitHubEventFilter{Event: notificationspb.GitHubEventName_GITHUB_EVENT_NAME_MERGEABILITY_CHANGED}
	requestSource := &notificationspb.Source{Provider: &notificationspb.Source_Github{Github: &notificationspb.GitHubSource{
		Repository: "test-owner/test-repo",
		Subject: &notificationspb.GitHubSubject{Subject: &notificationspb.GitHubSubject_PullRequest{
			PullRequest: &notificationspb.PullRequestSubject{Number: 7},
		}},
		Events: []*notificationspb.GitHubEventFilter{filter},
	}}}
	parsed, err := sourceBody(requestSource)
	if err != nil {
		t.Fatal(err)
	}
	github := parsed.(store.GitHubSource)
	if err := (store.Subscribe{
		DestinationRef: store.DestinationRef{Namespace: "test-ns", Name: "test-sandbox", UID: "test-uid"},
		SessionID:      "test-session", IdempotencyKey: "mergeability", Source: github, LifetimeDays: 1,
	}).Validate(); err != nil {
		t.Fatalf("selector was refused: %v", err)
	}
	view, err := sourceView(github)
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(view, requestSource) {
		t.Errorf("source view = %v, want %v", view, requestSource)
	}

	identity, err := eventIdentityView(store.GitHubMergeabilityEvent{
		AppID: 11, RepositoryID: 22, PullRequestNumber: 7, Sequence: 3,
	})
	if err != nil {
		t.Fatal(err)
	}
	want := &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubMergeability{
		GithubMergeability: &notificationspb.GitHubMergeabilityEvent{
			AppId: 11, RepositoryId: 22, PullRequestNumber: 7, Sequence: 3,
		},
	}}
	if !proto.Equal(identity, want) {
		t.Errorf("identity view = %v, want %v", identity, want)
	}
}
