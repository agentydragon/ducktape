package rpc

import (
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
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
	github := parsed.(sourceadapter.GitHubSource)
	if err := (sourceadapter.Subscribe{
		SessionID: "test-session", IdempotencyKey: "mergeability", Source: github, LifetimeDays: 1,
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

	identity, err := eventIdentityView(sourceadapter.WrapGitHubMergeabilityEvent(githubmodel.MergeabilityEvent{
		AppID: 11, RepositoryID: 22, PullRequestNumber: 7, Sequence: 3,
	}))
	if err != nil {
		t.Fatal(err)
	}
	want := &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubMergeability{
		GithubMergeability: &notificationspb.GitHubMergeabilityEvent{
			AppId: 11, RepositoryId: 22, PullRequestNumber: 7,
			Observation: &notificationspb.GitHubMergeabilityEvent_Sequence{Sequence: 3},
		},
	}}
	if !proto.Equal(identity, want) {
		t.Errorf("identity view = %v, want %v", identity, want)
	}

	snapshot, err := eventIdentityView(sourceadapter.WrapGitHubMergeabilitySnapshotEvent(githubmodel.MergeabilitySnapshotEvent{
		AppID: 11, RepositoryID: 22, PullRequestNumber: 7, Version: 4,
	}))
	if err != nil {
		t.Fatal(err)
	}
	wantSnapshot := &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubMergeability{
		GithubMergeability: &notificationspb.GitHubMergeabilityEvent{
			AppId: 11, RepositoryId: 22, PullRequestNumber: 7,
			Observation: &notificationspb.GitHubMergeabilityEvent_SnapshotVersion{SnapshotVersion: 4},
		},
	}}
	if !proto.Equal(snapshot, wantSnapshot) {
		t.Errorf("snapshot identity view = %v, want %v", snapshot, wantSnapshot)
	}

	var conflicting notificationspb.EventIdentity
	if err := protojson.Unmarshal([]byte(`{"githubMergeability":{"appId":"11","repositoryId":"22","pullRequestNumber":"7","sequence":"3","snapshotVersion":"4"}}`), &conflicting); err == nil {
		t.Fatal("decoded a mergeability identity with both observation discriminators")
	}
}

func TestGitHubObservationIdentityViewRoundTrips(t *testing.T) {
	for _, event := range []githubmodel.EventName{githubmodel.EventCheckRun, githubmodel.EventStatus, githubmodel.EventPullRequestReview, githubmodel.EventPullRequest} {
		identity, err := eventIdentityView(sourceadapter.WrapGitHubObservationEvent(githubmodel.ObservationEvent{
			AppID: 11, RepositoryID: 22, Event: event, Sequence: 3,
		}))
		if err != nil {
			t.Fatal(err)
		}
		name, err := eventNameView(event)
		if err != nil {
			t.Fatal(err)
		}
		want := &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubObservation{
			GithubObservation: &notificationspb.GitHubObservationEvent{
				AppId: 11, RepositoryId: 22, Event: name, Sequence: 3,
			},
		}}
		if !proto.Equal(identity, want) {
			t.Fatalf("identity view = %v, want %v", identity, want)
		}
		encoded, err := protojson.Marshal(identity)
		if err != nil {
			t.Fatal(err)
		}
		var decoded notificationspb.EventIdentity
		if err := protojson.Unmarshal(encoded, &decoded); err != nil {
			t.Fatal(err)
		}
		if !proto.Equal(&decoded, identity) {
			t.Errorf("protojson round trip = %v, want %v", &decoded, identity)
		}
	}
	if _, err := eventIdentityView(sourceadapter.WrapGitHubObservationEvent(githubmodel.ObservationEvent{
		AppID: 11, RepositoryID: 22, Event: githubmodel.EventIssues, Sequence: 4,
	})); err == nil {
		t.Fatal("view accepted an unsupported observation event")
	}
}

func TestGitHubRefreshStatusViewsSeparateCheckRunsCommitStatusesAndReviews(t *testing.T) {
	resources := []struct {
		name     string
		kind     model.SourceFailureKind
		wantKind notificationspb.SourceFailureKind
	}{
		{"check runs", model.FailureRateLimited, notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_RATE_LIMITED},
		{"commit statuses", model.FailureAccessDenied, notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_ACCESS_DENIED},
		{"reviews", model.FailureUnavailable, notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_UNAVAILABLE},
	}
	statuses := make([]*githubstate.RefreshStatus, len(resources))
	want := make([]*notificationspb.GitHubRefreshStatus, len(resources))
	for i, resource := range resources {
		// Each resource has times and a failure of its own, so a view built from another resource's status differs.
		lastSuccess := time.Date(2026, 10, 27, 10+i, 0, 0, 0, time.UTC)
		errorSince := lastSuccess.Add(time.Minute)
		errorObserved := errorSince.Add(time.Second)
		retryAt := errorObserved.Add(time.Minute)
		refreshingUntil := retryAt.Add(time.Minute)
		message := resource.name + " failed"
		statuses[i] = &githubstate.RefreshStatus{
			LastSuccessAt:   &lastSuccess,
			Failure:         &model.SourceFailure{Kind: resource.kind, Message: message, Since: errorSince, ObservedAt: errorObserved},
			RetryAt:         &retryAt,
			RefreshingUntil: &refreshingUntil,
		}
		want[i] = &notificationspb.GitHubRefreshStatus{
			LastSuccessAt: timestamppb.New(lastSuccess), ErrorKind: resource.wantKind.Enum(), Error: proto.String(message),
			ErrorSince: timestamppb.New(errorSince), ErrorObservedAt: timestamppb.New(errorObserved),
			RetryAt: timestamppb.New(retryAt), RefreshingUntil: timestamppb.New(refreshingUntil),
		}
	}
	status, err := githubStatusView(&githubstate.Status{
		Subject:   githubstate.SubjectStatus{Key: githubstate.SubjectKey{RepositoryID: 22, Kind: "pull_request", Key: "7"}},
		CheckRuns: statuses[0], CommitStatuses: statuses[1], Reviews: statuses[2],
	})
	if err != nil {
		t.Fatal(err)
	}
	for i, got := range []*notificationspb.GitHubRefreshStatus{status.GetCheckRuns(), status.GetCommitStatuses(), status.GetReviews()} {
		if !proto.Equal(got, want[i]) {
			t.Errorf("%s refresh view = %v, want %v", resources[i].name, got, want[i])
		}
	}
}
