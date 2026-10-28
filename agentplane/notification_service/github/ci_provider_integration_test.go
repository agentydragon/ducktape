package github

import (
	"fmt"
	"net/http"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
)

const ciChecksPath = "/repos/owner/repo/commits/" + testSHA + "/check-runs"

func completedCICheck(id int64) string {
	return fmt.Sprintf(`{"total_count":1,"check_runs":[{"id":%d,"name":"build","head_sha":%q,"status":"completed","conclusion":"success","details_url":"https://github.example.test/checks/%d","started_at":"2026-10-27T00:00:00Z","completed_at":"2026-10-27T00:01:00Z"}]}`, id, testSHA, id)
}

func TestCIRepairsAMissedWebhookAfterRestartAndSharesTheRead(t *testing.T) {
	h := newHarness(t)
	first := h.follow(pullRequestSource(7), "first")
	second := h.follow(pullRequestSource(7), "second")
	claim := h.claim()
	h.reconcileAll(claim)
	if entries := h.page(first.InboxID).Entries; len(entries) != 0 {
		t.Fatalf("initial entries = %d, want none", len(entries))
	}

	// GitHub changes; no webhook arrives. The durable CI deadline survives a new provider.
	h.fake.respond(ciChecksPath, http.StatusOK, nil, completedCICheck(31))
	h.exec(`UPDATE github_ci_refresh SET last_success_at = now() - interval '20 minutes'`)
	h.exec(`UPDATE subscription SET next_attempt = now()`)
	h.restart()
	h.reconcileAll(claim)
	entries := h.page(first.InboxID).Entries
	if len(entries) != 1 {
		t.Fatalf("entries = %d, want the recovered check", len(entries))
	}
	event, ok := entries[0].Event.(store.GitHubObservationEvent)
	if !ok || event.Event != store.EventCheckRun || event.RepositoryID != baseRepository || event.Sequence <= 0 {
		t.Fatalf("event = %#v, want a generated check observation", entries[0].Event)
	}
	requireEqual(t, entries[0].SubscriptionIDs, sortedIDs(first.ID, second.ID))
	if got := h.fake.count(ciChecksPath); got != 2 {
		t.Fatalf("check reads = %d, want initial + one shared repair", got)
	}

	// An ordinary wakeup neither polls again nor duplicates the recovered result.
	h.ingest("issue_comment", comment(7))
	h.reconcileAll(claim)
	if got := h.fake.count(ciChecksPath); got != 2 {
		t.Fatalf("check reads after wake = %d, want 2", got)
	}
	if got := len(h.page(first.InboxID).Entries); got != 2 {
		t.Fatalf("entries after comment = %d, want check + comment", got)
	}
}

func TestCIPermissionFailureDoesNotBlockOrdinaryWebhookDelivery(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7), "follow")
	h.fake.respond(ciChecksPath, http.StatusForbidden, nil, `{"message":"Resource not accessible by integration"}`)
	h.ingest("issue_comment", comment(7))
	claim := h.claim()
	h.reconcileAll(claim)
	if got := len(h.page(subscription.InboxID).Entries); got != 1 {
		t.Fatalf("entries = %d, want the ordinary comment", got)
	}
	view := h.subscription(subscription.ID)
	if view.GitHub == nil || view.GitHub.CI == nil || view.GitHub.CI.Failure == nil || view.GitHub.CI.Failure.Kind != store.FailureAccessDenied {
		t.Fatalf("GitHub status = %+v, want independent CI access failure", view.GitHub)
	}
	if view.GitHub.Subject.Failure != nil || view.Failure != nil {
		t.Fatalf("CI denial escaped its coverage status: %+v", view)
	}
}

func TestCommentOnlySubscriptionDoesNotPollCI(t *testing.T) {
	h := newHarness(t)
	subscription := h.follow(pullRequestSource(7, store.EventFilter{Event: store.EventIssueComment}), "comments")
	h.ingest("issue_comment", comment(7))
	h.reconcileAll(h.claim())
	if got := h.fake.count(ciChecksPath); got != 0 {
		t.Fatalf("CI reads = %d for a comment-only subscription", got)
	}
	if got := len(h.page(subscription.InboxID).Entries); got != 1 {
		t.Fatalf("entries = %d, want comment", got)
	}
}

func TestPendingMergeabilityDoesNotAccelerateCIReads(t *testing.T) {
	h := newHarness(t)
	h.fake.respond("/repos/owner/repo/pulls/7", http.StatusOK, nil,
		`{"number":7,"state":"open","merged":false,"base":{"ref":"main","sha":"`+testBaseSHA+`"},"mergeable":null,"head":{"sha":"`+testSHA+`","repo":{"id":100,"full_name":"owner/repo"}}}`)
	h.follow(pullRequestSource(7), "follow")
	claim := h.claim()
	h.reconcileAll(claim)
	h.exec(`UPDATE github_subject SET next_attempt = now()`)
	h.exec(`UPDATE subscription SET next_attempt = now()`)
	h.reconcileAll(claim)
	if got := h.fake.count(ciChecksPath); got != 1 {
		t.Fatalf("CI reads = %d, want one across pending mergeability rechecks", got)
	}
}

func TestPollingAndACompleteWebhookProduceOneCheckObservation(t *testing.T) {
	for _, webhookFirst := range []bool{true, false} {
		t.Run(fmt.Sprintf("webhook_first_%t", webhookFirst), func(t *testing.T) {
			h := newHarness(t)
			subscription := h.follow(pullRequestSource(7), "follow")
			claim := h.claim()
			h.reconcileAll(claim)
			payload := check(testSHA)
			payload["check_run"] = map[string]any{
				"id": 31, "name": "build", "head_sha": testSHA,
				"status": "completed", "conclusion": "success", "pull_requests": []any{},
			}
			poll := func() {
				h.fake.respond(ciChecksPath, http.StatusOK, nil, completedCICheck(31))
				h.exec(`UPDATE github_ci_refresh SET last_success_at = now() - interval '20 minutes'`)
				h.exec(`UPDATE subscription SET next_attempt = now()`)
				h.reconcileAll(claim)
			}
			webhook := func() {
				h.ingest("check_run", payload)
				h.reconcileAll(claim)
			}
			if webhookFirst {
				webhook()
				poll()
			} else {
				poll()
				webhook()
			}
			if got := len(h.page(subscription.InboxID).Entries); got != 1 {
				t.Fatalf("entries = %d, want one check observation across both paths", got)
			}
		})
	}
}
