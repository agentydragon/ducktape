package github

import (
	"encoding/json"
	"fmt"
	"net/http"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
)

const reviewsPath = "/repos/owner/repo/pulls/7/reviews"

func reviewList(state string) string {
	return fmt.Sprintf(`[{"id":31,"user":{"id":101,"login":"reviewer"},"state":%q,"commit_id":%q,"submitted_at":"2026-10-27T01:02:03Z","body":"review","html_url":"https://github.example.test/reviews/31"}]`, state, testBaseSHA)
}

func TestReviewsRepairAMissedWebhookAfterRestartAndShareTheRead(t *testing.T) {
	h := newHarness(t)
	source := pullRequestSource(7, store.EventFilter{Event: store.EventPullRequestReview})
	first := h.follow(source, "first")
	second := h.follow(source, "second")
	claim := h.claim()
	h.reconcileAll(claim)
	if entries := h.page(first.InboxID).Entries; len(entries) != 0 {
		t.Fatalf("initial entries = %d, want no reviews", len(entries))
	}
	h.fake.respond(reviewsPath, http.StatusOK, nil, reviewList("APPROVED"))
	h.exec(`UPDATE github_review_refresh SET last_success_at = now() - interval '20 minutes'`)
	h.exec(`UPDATE subscription SET next_attempt = now()`)
	h.restart()
	h.reconcileAll(claim)
	entries := h.page(first.InboxID).Entries
	if len(entries) != 1 {
		t.Fatalf("entries = %d, want one recovered review", len(entries))
	}
	event, ok := entries[0].Event.(store.GitHubObservationEvent)
	if !ok || event.Event != store.EventPullRequestReview || event.Sequence <= 0 {
		t.Fatalf("identity = %#v, want review observation", entries[0].Event)
	}
	requireEqual(t, entries[0].SubscriptionIDs, sortedIDs(first.ID, second.ID))
	var review store.GitHubReviewObservation
	if err := json.Unmarshal(entries[0].Payload, &review); err != nil {
		t.Fatal(err)
	}
	if review.ID != 31 || review.Reviewer.ID != 101 || review.State != store.ReviewApproved || review.CommitSHA != testBaseSHA {
		t.Fatalf("review = %+v, want original review identity and evaluated older commit", review)
	}
	if got := h.fake.count(reviewsPath); got != 2 {
		t.Fatalf("review reads = %d, want initial plus one shared refresh", got)
	}
	h.fake.respond(reviewsPath, http.StatusOK, nil, reviewList("DISMISSED"))
	h.exec(`UPDATE github_review_refresh SET last_success_at = now() - interval '20 minutes'`)
	h.exec(`UPDATE subscription SET next_attempt = now()`)
	h.reconcileAll(claim)
	if entries := h.page(first.InboxID).Entries; len(entries) != 2 {
		t.Fatalf("entries = %d, want review plus dismissal", len(entries))
	}
}

func TestReviewPermissionFailureDoesNotBlockCommentsOrCommitResources(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7), "follow")
	h.fake.respond(reviewsPath, http.StatusForbidden, nil, `{"message":"Resource not accessible by integration"}`)
	h.fake.respond(checkRunsPath, http.StatusOK, nil, completedCheckRun(31))
	h.ingest("issue_comment", comment(7))
	h.reconcileAll(h.claim())
	if got := len(h.page(subscription.InboxID).Entries); got != 2 {
		t.Fatalf("entries = %d, want comment and CI despite review failure", got)
	}
	view := h.subscription(subscription.ID)
	if view.GitHub.Reviews == nil || view.GitHub.Reviews.Failure == nil || view.GitHub.Reviews.Failure.Kind != store.FailureAccessDenied {
		t.Fatalf("review refresh = %+v, want independent denial", view.GitHub.Reviews)
	}
	if view.Failure != nil || view.GitHub.Subject.Failure != nil || view.GitHub.CheckRuns.Failure != nil || view.GitHub.CommitStatuses.Failure != nil {
		t.Fatalf("review failure escaped its category: %+v", view)
	}
}

func TestLifecycleRepairsMissedCloseAndReopenWithActionFilters(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7, store.EventFilter{Event: store.EventPullRequest, Actions: []string{"closed", "reopened"}}), "lifecycle")
	claim := h.claim()
	h.reconcileAll(claim)
	if got := len(h.page(subscription.InboxID).Entries); got != 0 {
		t.Fatalf("initial OPEN emitted %d entries, want quiet baseline", got)
	}
	for i, state := range []string{"closed", "open"} {
		h.fake.respond("/repos/owner/repo/pulls/7", http.StatusOK, nil, fmt.Sprintf(`{"number":7,"state":%q,"merged":false,"base":{"ref":"main","sha":%q},"mergeable":null,"head":{"sha":%q,"repo":{"id":100,"full_name":"owner/repo"}}}`, state, testBaseSHA, testSHA))
		h.exec(`UPDATE github_subject SET last_success_at = now() - interval '20 minutes', next_attempt = NULL`)
		h.exec(`UPDATE subscription SET next_attempt = now()`)
		h.restart()
		h.reconcileAll(claim)
		entries := h.page(subscription.InboxID).Entries
		if len(entries) != i+1 {
			t.Fatalf("after %s: entries = %d, want %d", state, len(entries), i+1)
		}
		if event, ok := entries[i].Event.(store.GitHubObservationEvent); !ok || event.Event != store.EventPullRequest {
			t.Fatalf("lifecycle identity = %#v", entries[i].Event)
		}
	}
}

func TestMergedLifecycleRepairDeduplicatesALateWebhook(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7, store.EventFilter{Event: store.EventPullRequest}), "lifecycle")
	claim := h.claim()
	h.reconcileAll(claim)
	h.fake.respond("/repos/owner/repo/pulls/7", http.StatusOK, nil, fmt.Sprintf(`{"number":7,"state":"closed","merged":true,"base":{"ref":"main","sha":%q},"mergeable":null,"head":{"sha":%q,"repo":{"id":100,"full_name":"owner/repo"}}}`, testBaseSHA, testSHA))
	h.exec(`UPDATE github_subject SET last_success_at = now() - interval '20 minutes', next_attempt = NULL`)
	h.exec(`UPDATE subscription SET next_attempt = now()`)
	h.restart()
	h.reconcileAll(claim)
	entries := h.page(subscription.InboxID).Entries
	if len(entries) != 1 {
		t.Fatalf("entries = %d, want recovered merge", len(entries))
	}
	var lifecycle store.GitHubPRLifecycleObservation
	if err := json.Unmarshal(entries[0].Payload, &lifecycle); err != nil {
		t.Fatal(err)
	}
	if lifecycle.State != store.GitHubPullRequestMerged || lifecycle.Action == nil || *lifecycle.Action != "closed" {
		t.Fatalf("lifecycle = %+v, want MERGED with closed action", lifecycle)
	}
	h.ingest("pull_request", map[string]any{
		"action": "closed", "installation": map[string]any{"id": baseInstallation},
		"repository": repositoryPayload(baseRepository, baseRepositoryName),
		"pull_request": map[string]any{
			"number": 7, "state": "closed", "merged": true,
			"head": map[string]any{"sha": testSHA}, "base": map[string]any{"ref": "main"},
		},
	})
	h.reconcileAll(claim)
	if got := len(h.page(subscription.InboxID).Entries); got != 1 {
		t.Fatalf("entries = %d after late webhook, want the same recovered merge only", got)
	}
}
