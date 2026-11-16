package notifications

import (
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	"google.golang.org/protobuf/encoding/protojson"
)

func TestMergeabilityEventFilterHasNoActionsAndInboxSummaryNamesItsPullRequest(t *testing.T) {
	filters, err := parseEvents([]string{"mergeability_changed"})
	if err != nil || len(filters) != 1 || filters[0].GetEvent() != notificationspb.GitHubEventName_GITHUB_EVENT_NAME_MERGEABILITY_CHANGED || len(filters[0].GetActions()) != 0 {
		t.Fatalf("filters = %v, error = %v", filters, err)
	}
	if _, err := parseEvents([]string{"mergeability_changed:completed"}); err == nil {
		t.Fatal("mergeability_changed accepted an action suffix")
	}
	line := inboxEntryLine(&notificationspb.InboxEntry{
		Cursor: 5,
		Event: &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubMergeability{
			GithubMergeability: &notificationspb.GitHubMergeabilityEvent{
				PullRequestNumber: 7, Observation: &notificationspb.GitHubMergeabilityEvent_Sequence{Sequence: 3},
			},
		}},
	})
	if want := "entry 5  github PR #7 mergeability sequence 3"; line != want {
		t.Errorf("summary = %q, want %q", line, want)
	}
	snapshot := inboxEntryLine(&notificationspb.InboxEntry{
		Cursor: 6,
		Event: &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubMergeability{
			GithubMergeability: &notificationspb.GitHubMergeabilityEvent{
				PullRequestNumber: 7, Observation: &notificationspb.GitHubMergeabilityEvent_SnapshotVersion{SnapshotVersion: 4},
			},
		}},
	})
	if want := "entry 6  github PR #7 mergeability snapshot version 4"; snapshot != want {
		t.Errorf("snapshot summary = %q, want %q", snapshot, want)
	}
}

func TestInboxSummaryNamesGitHubObservations(t *testing.T) {
	for _, event := range []struct {
		name notificationspb.GitHubEventName
		want string
	}{
		{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_CHECK_RUN, "entry 7  github check_run observation sequence 12"},
		{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_STATUS, "entry 7  github status observation sequence 12"},
		{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PULL_REQUEST, "entry 7  github pull_request observation sequence 12"},
		{notificationspb.GitHubEventName_GITHUB_EVENT_NAME_PULL_REQUEST_REVIEW, "entry 7  github pull_request_review observation sequence 12"},
	} {
		line := inboxEntryLine(&notificationspb.InboxEntry{
			Cursor: 7,
			Event: &notificationspb.EventIdentity{Provider: &notificationspb.EventIdentity_GithubObservation{
				GithubObservation: &notificationspb.GitHubObservationEvent{
					RepositoryId: 22, Event: event.name, Sequence: 12,
				},
			}},
		})
		if line != event.want {
			t.Errorf("summary = %q, want %q", line, event.want)
		}
	}
}

func TestSubscriptionSummarySerializesGitHubStatus(t *testing.T) {
	raw := []byte(`{
		"id":"subscription",
		"source":{"github":{"repository":"owner/repo","subject":{"pullRequest":{"number":7}}}},
		"version":"1",
		"github":{
			"checkRuns":{"lastSuccessAt":"2026-10-27T10:00:00Z","errorKind":"SOURCE_FAILURE_KIND_RATE_LIMITED","error":"check runs rate limited","retryAt":"2026-10-27T10:02:00Z"},
			"commitStatuses":{"lastSuccessAt":"2026-10-27T11:00:00Z","errorKind":"SOURCE_FAILURE_KIND_ACCESS_DENIED","error":"statuses denied","retryAt":"2026-10-27T11:02:00Z"},
			"reviews":{"lastSuccessAt":"2026-10-27T12:00:00Z","errorKind":"SOURCE_FAILURE_KIND_UNAVAILABLE","error":"reviews unavailable","retryAt":"2026-10-27T12:02:00Z","refreshingUntil":"2026-10-27T12:03:00Z"}
		}
	}`)
	var subscription notificationspb.Subscription
	if err := protojson.Unmarshal(raw, &subscription); err != nil {
		t.Fatal(err)
	}
	line, err := subscriptionLine(&subscription)
	if err != nil {
		t.Fatal(err)
	}
	_, encoded, found := strings.Cut(line, "github status ")
	if !found {
		t.Fatalf("summary %q does not include GitHub status", line)
	}
	var status notificationspb.GitHubStatus
	if err := protojson.Unmarshal([]byte(encoded), &status); err != nil {
		t.Fatal(err)
	}
	if status.GetCheckRuns().GetError() != "check runs rate limited" || status.GetCheckRuns().GetErrorKind() != notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_RATE_LIMITED || status.GetCheckRuns().GetRetryAt().AsTime().UTC().Format(time.RFC3339) != "2026-10-27T10:02:00Z" {
		t.Fatalf("summary did not preserve check-run status: %v", &status)
	}
	if status.GetCommitStatuses().GetError() != "statuses denied" || status.GetCommitStatuses().GetErrorKind() != notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_ACCESS_DENIED || status.GetCommitStatuses().GetRetryAt().AsTime().UTC().Format(time.RFC3339) != "2026-10-27T11:02:00Z" {
		t.Fatalf("summary did not preserve GitHub status: %v", &status)
	}
	if status.GetReviews() == nil || status.GetReviews().GetError() != "reviews unavailable" || status.GetReviews().GetErrorKind() != notificationspb.SourceFailureKind_SOURCE_FAILURE_KIND_UNAVAILABLE ||
		status.GetReviews().GetRetryAt().AsTime().UTC().Format(time.RFC3339) != "2026-10-27T12:02:00Z" ||
		status.GetReviews().GetRefreshingUntil().AsTime().UTC().Format(time.RFC3339) != "2026-10-27T12:03:00Z" {
		t.Fatalf("summary did not preserve review status: %v", &status)
	}
}
