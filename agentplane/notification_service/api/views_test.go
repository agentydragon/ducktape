package api

import (
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"google.golang.org/protobuf/encoding/protojson"
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
			AppId: 11, RepositoryId: 22, PullRequestNumber: 7,
			Observation: &notificationspb.GitHubMergeabilityEvent_Sequence{Sequence: 3},
		},
	}}
	if !proto.Equal(identity, want) {
		t.Errorf("identity view = %v, want %v", identity, want)
	}

	snapshot, err := eventIdentityView(store.GitHubMergeabilitySnapshotEvent{
		AppID: 11, RepositoryID: 22, PullRequestNumber: 7, Version: 4,
	})
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
	for _, event := range []store.EventName{store.EventCheckRun, store.EventStatus, store.EventPullRequestReview, store.EventPullRequest} {
		identity, err := eventIdentityView(store.GitHubObservationEvent{
			AppID: 11, RepositoryID: 22, Event: event, Sequence: 3,
		})
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
	if _, err := eventIdentityView(store.GitHubObservationEvent{
		AppID: 11, RepositoryID: 22, Event: store.EventIssues, Sequence: 4,
	}); err == nil {
		t.Fatal("view accepted an unsupported observation event")
	}
}

func TestGitHubRefreshStatusViewsSeparateCheckRunsAndCommitStatuses(t *testing.T) {
	lastSuccess := time.Date(2026, 10, 27, 10, 0, 0, 0, time.UTC)
	errorSince := lastSuccess.Add(time.Minute)
	errorObserved := errorSince.Add(time.Second)
	retryAt := errorObserved.Add(time.Minute)
	refreshingUntil := retryAt.Add(time.Minute)
	statusesLastSuccess := lastSuccess.Add(time.Hour)
	statusesErrorSince := statusesLastSuccess.Add(time.Minute)
	statusesErrorObserved := statusesErrorSince.Add(time.Second)
	statusesRetryAt := statusesErrorObserved.Add(time.Minute)
	statusesRefreshingUntil := statusesRetryAt.Add(time.Minute)
	status, err := githubStatusView(&store.GitHubStatus{
		Subject: store.GitHubSubjectStatus{Key: store.GitHubSubjectKey{RepositoryID: 22, Kind: "pull_request", Key: "7"}},
		CheckRuns: &store.GitHubRefreshStatus{
			LastSuccessAt: &lastSuccess,
			Failure: &store.SourceFailure{
				Kind: store.FailureRateLimited, Message: "rate limited", Since: errorSince, ObservedAt: errorObserved,
			},
			RetryAt: &retryAt, RefreshingUntil: &refreshingUntil,
		},
		CommitStatuses: &store.GitHubRefreshStatus{
			LastSuccessAt: &statusesLastSuccess,
			Failure: &store.SourceFailure{
				Kind: store.FailureAccessDenied, Message: "statuses denied", Since: statusesErrorSince, ObservedAt: statusesErrorObserved,
			},
			RetryAt: &statusesRetryAt, RefreshingUntil: &statusesRefreshingUntil,
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if status.GetCheckRuns() == nil || status.GetCheckRuns().GetErrorKind() != notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_RATE_LIMITED ||
		status.GetCheckRuns().GetError() != "rate limited" || status.GetCheckRuns().GetLastSuccessAt().AsTime() != lastSuccess ||
		status.GetCheckRuns().GetErrorSince().AsTime() != errorSince || status.GetCheckRuns().GetErrorObservedAt().AsTime() != errorObserved ||
		status.GetCheckRuns().GetRetryAt().AsTime() != retryAt || status.GetCheckRuns().GetRefreshingUntil().AsTime() != refreshingUntil {
		t.Errorf("check-run refresh view = %v", status.GetCheckRuns())
	}
	if status.GetCommitStatuses() == nil || status.GetCommitStatuses().GetErrorKind() != notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_ACCESS_DENIED ||
		status.GetCommitStatuses().GetError() != "statuses denied" || status.GetCommitStatuses().GetLastSuccessAt().AsTime() != statusesLastSuccess ||
		status.GetCommitStatuses().GetErrorSince().AsTime() != statusesErrorSince || status.GetCommitStatuses().GetErrorObservedAt().AsTime() != statusesErrorObserved ||
		status.GetCommitStatuses().GetRetryAt().AsTime() != statusesRetryAt || status.GetCommitStatuses().GetRefreshingUntil().AsTime() != statusesRefreshingUntil {
		t.Errorf("commit-status refresh view = %v", status.GetCommitStatuses())
	}
}
