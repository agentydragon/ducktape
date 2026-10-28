package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/google/uuid"
)

func testPRReview(id int64, state GitHubReviewState, sha string) GitHubReviewObservation {
	return GitHubReviewObservation{
		ID: id, Reviewer: GitHubReviewer{ID: 41, Login: "reviewer"}, State: state,
		CommitSHA: sha, Body: "review body", HTMLURL: "https://github.com/test-owner/test-repo/pull/7#pullrequestreview-" + fmt.Sprint(id),
	}
}

func (g githubFixture) pollReviews(t *testing.T, subject GitHubSubjectKey, reviews []GitHubReviewObservation) {
	t.Helper()
	key := GitHubReviewRefreshKey{AppID: g.app, InstallationID: g.installation, Subject: subject}
	lease, err := g.state.Acquire(t.Context(), key)
	if err != nil {
		t.Fatal(err)
	}
	if lease == nil {
		g.exec(t, `UPDATE github_review_refresh SET last_success_at = now() - interval '11 minutes',
			next_attempt = NULL, claim = NULL, claim_until = NULL
			WHERE app_id = $1 AND installation_id = $2 AND repository_id = $3 AND subject_key = $4`,
			g.app, g.installation, subject.RepositoryID, subject.Key)
		lease, err = g.state.Acquire(t.Context(), key)
		if err != nil {
			t.Fatal(err)
		}
	}
	if lease == nil {
		t.Fatal("review refresh did not acquire after its durable deadline was advanced")
	}
	if err := g.state.Succeed(t.Context(), *lease, GitHubSuccess{
		Fences: []GitHubAccessFence{g.validate(t, g.access())}, Reviews: reviews,
	}); err != nil {
		t.Fatal(err)
	}
}

func reviewDelivery(g githubFixture, number int64, observation GitHubReviewObservation, action string) GitHubIngest {
	observation.Action = &action
	ingest := g.delivery(string(EventPullRequestReview), withAction(action), onPullRequest(number))
	ingest.Review = &GitHubPRReviewObservation{PullRequestNumber: number, Review: observation}
	return ingest
}

func lifecycleDelivery(g githubFixture, number int64, state GitHubPullRequestState, head, baseRef, action string) GitHubIngest {
	observation := GitHubPRLifecycleObservation{
		PullRequestNumber: number, State: state, HeadSHA: head, BaseRef: baseRef, Action: &action,
	}
	ingest := g.delivery(string(EventPullRequest), withAction(action), onPullRequest(number))
	ingest.Lifecycle = &observation
	return ingest
}

func hasSubscription(entry Entry, id uuid.UUID) bool {
	for _, subscriptionID := range entry.SubscriptionIDs {
		if subscriptionID == id {
			return true
		}
	}
	return false
}

func TestGitHubPRReviewPollAndWebhookDeduplicateInEitherOrderAndSnapshotLateSubscribers(t *testing.T) {
	for _, webhookFirst := range []bool{true, false} {
		name := "poll first"
		if webhookFirst {
			name = "webhook first"
		}
		t.Run(name, func(t *testing.T) {
			g := newGitHubFixture(t)
			source := g.source(PullRequestSubject{Number: 7}, EventFilter{Event: EventPullRequestReview})
			first := g.follow(t, source, "review-first")
			second := g.follow(t, source, "review-second")
			g.observePullRequest(t, first, GitHubPullRequestObservation{
				BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
				State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
			})

			review := testPRReview(7001, ReviewApproved, commitB) // A review may refer to an older head.
			if webhookFirst {
				g.ingest(t, reviewDelivery(g, 7, review, "submitted"))
				g.pollReviews(t, first.Subject, []GitHubReviewObservation{review})
			} else {
				g.pollReviews(t, first.Subject, []GitHubReviewObservation{review})
				g.ingest(t, reviewDelivery(g, 7, review, "submitted"))
			}

			if got := g.count(t, `SELECT count(*) FROM github_pr_state_event WHERE app_id = $1 AND event = 'pull_request_review'`, g.app); got != 1 {
				t.Fatalf("normalized review transitions = %d, want one", got)
			}
			g.reconcileSubscription(t, first)
			g.reconcileSubscription(t, second)
			entries := observationEntries(t, g.read(t, g.inbox, 0).Entries)
			if len(entries) != 1 {
				t.Fatalf("shared inbox review observations = %d, want one", len(entries))
			}
			if !hasSubscription(entries[0], first.ID) || !hasSubscription(entries[0], second.ID) {
				t.Errorf("shared observation subscriptions = %v, want both subscribers", entries[0].SubscriptionIDs)
			}
			var payload GitHubReviewObservation
			if err := json.Unmarshal(entries[0].Payload, &payload); err != nil {
				t.Fatal(err)
			}
			if payload.ID != review.ID || payload.CommitSHA != commitB || payload.State != ReviewApproved {
				t.Errorf("review observation = %+v, want review on its original commit", payload)
			}
			if got := g.count(t, `SELECT count(*) FROM subscription_match m JOIN entry e
				ON e.inbox_id = $1 AND e.cursor = m.cursor WHERE m.subscription_id = $2
				AND e.event->>'provider' = 'github' AND e.event->>'event' = 'pull_request_review'`, g.inbox, first.ID); got != 0 {
				t.Errorf("normalized raw review matches = %d, want zero", got)
			}

			lateDestination := g.destination
			lateDestination.Name = "late-review-observer"
			late := g.followIn(t, lateDestination, source, "review-late")
			g.reconcileSubscription(t, late)
			lateEntries := observationEntries(t, g.read(t, late.InboxID, 0).Entries)
			if len(lateEntries) != 1 {
				t.Fatalf("late current review snapshot = %d, want one", len(lateEntries))
			}
			if got, want := lateEntries[0].Event.(GitHubObservationEvent).Sequence,
				entries[0].Event.(GitHubObservationEvent).Sequence; got != want {
				t.Errorf("late snapshot sequence = %d, want current sequence %d", got, want)
			}
		})
	}
}

func TestGitHubPRReviewDismissalTransitionsAndActionFilters(t *testing.T) {
	g := newGitHubFixture(t)
	submittedSource := g.source(PullRequestSubject{Number: 7}, EventFilter{
		Event: EventPullRequestReview, Actions: []string{"submitted"},
	})
	dismissedSource := g.source(PullRequestSubject{Number: 7}, EventFilter{
		Event: EventPullRequestReview, Actions: []string{"dismissed"},
	})
	submitted := g.follow(t, submittedSource, "submitted-review")
	dismissed := g.follow(t, dismissedSource, "dismissed-review")
	g.observePullRequest(t, submitted, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
	})

	approved := testPRReview(7002, ReviewApproved, commitB)
	g.pollReviews(t, submitted.Subject, []GitHubReviewObservation{approved})
	g.reconcileSubscription(t, submitted)
	g.reconcileSubscription(t, dismissed)
	if got := len(observationEntries(t, g.read(t, g.inbox, 0).Entries)); got != 1 {
		t.Fatalf("submitted-only review observations after approval = %d, want one", got)
	}

	dismissal := testPRReview(7002, ReviewDismissed, commitB)
	g.pollReviews(t, submitted.Subject, []GitHubReviewObservation{dismissal})
	g.reconcileSubscription(t, submitted)
	g.reconcileSubscription(t, dismissed)
	entries := observationEntries(t, g.read(t, g.inbox, 0).Entries)
	if len(entries) != 2 {
		t.Fatalf("review state transitions = %d, want approved and dismissed", len(entries))
	}
	var dismissedCount, submittedCount int
	for _, entry := range entries {
		var review GitHubReviewObservation
		if err := json.Unmarshal(entry.Payload, &review); err != nil {
			t.Fatal(err)
		}
		if review.State == ReviewDismissed {
			dismissedCount++
			if !hasSubscription(entry, dismissed.ID) || hasSubscription(entry, submitted.ID) {
				t.Errorf("dismissed transition subscriptions = %v, want dismissed-only", entry.SubscriptionIDs)
			}
		} else if review.State == ReviewApproved {
			submittedCount++
			if !hasSubscription(entry, submitted.ID) || hasSubscription(entry, dismissed.ID) {
				t.Errorf("submitted transition subscriptions = %v, want submitted-only", entry.SubscriptionIDs)
			}
		}
		if review.CommitSHA != commitB {
			t.Errorf("review commit SHA = %q, want original commit %q", review.CommitSHA, commitB)
		}
	}
	if submittedCount != 1 || dismissedCount != 1 {
		t.Errorf("review transitions submitted=%d dismissed=%d, want one each", submittedCount, dismissedCount)
	}

	lateDestination := g.destination
	lateDestination.Name = "dismissed-review-current"
	late := g.followIn(t, lateDestination, g.source(PullRequestSubject{Number: 7},
		EventFilter{Event: EventPullRequestReview}), "dismissed-current")
	g.reconcileSubscription(t, late)
	lateEntries := observationEntries(t, g.read(t, late.InboxID, 0).Entries)
	if len(lateEntries) != 1 {
		t.Fatalf("late review current snapshot = %d, want one current fact", len(lateEntries))
	}
	var current GitHubReviewObservation
	if err := json.Unmarshal(lateEntries[0].Payload, &current); err != nil {
		t.Fatal(err)
	}
	if current.State != ReviewDismissed || current.CommitSHA != commitB {
		t.Errorf("late current review = %+v, want dismissed review on original commit", current)
	}
}

func TestGitHubPRLifecyclePollRecoveryPreservesRealActions(t *testing.T) {
	g := newGitHubFixture(t)
	closedSource := g.source(PullRequestSubject{Number: 7}, EventFilter{
		Event: EventPullRequest, Actions: []string{"closed"},
	})
	closedDestination := g.destination
	closedDestination.Name = "closed-pr"
	closed := g.followIn(t, closedDestination, closedSource, "closed-pr")
	g.observePullRequest(t, closed, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
	})
	if got := g.count(t, `SELECT count(*) FROM github_pr_state_event WHERE repository_id = $1 AND pull_request_number = 7`, g.repository); got != 0 {
		t.Fatalf("initial API OPEN state emitted %d lifecycle events, want quiet baseline", got)
	}

	// The poll catches a close whose webhook was missed and infers the action from the prior OPEN state.
	g.observePullRequest(t, closed, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestClosed, Mergeability: GitHubMergeabilityUnknown,
	})
	g.ingest(t, lifecycleDelivery(g, 7, GitHubPullRequestClosed, commitA, "main", "closed"))
	g.reconcileSubscription(t, closed)
	closedEntries := observationEntries(t, g.read(t, closed.InboxID, 0).Entries)
	if len(closedEntries) != 1 {
		t.Fatalf("recovered closed lifecycle entries = %d, want one", len(closedEntries))
	}
	var closedPayload GitHubPRLifecycleObservation
	if err := json.Unmarshal(closedEntries[0].Payload, &closedPayload); err != nil {
		t.Fatal(err)
	}
	if closedPayload.State != GitHubPullRequestClosed || closedPayload.Action == nil || *closedPayload.Action != "closed" {
		t.Errorf("recovered lifecycle payload = %+v, want closed action", closedPayload)
	}
	if got := g.count(t, `SELECT count(*) FROM subscription_match m JOIN entry e
		ON e.inbox_id = $1 AND e.cursor = m.cursor WHERE m.subscription_id = $2
		AND e.event->>'provider' = 'github' AND e.event->>'event' = 'pull_request'`, closed.InboxID, closed.ID); got != 0 {
		t.Errorf("covered closed raw webhook matches = %d, want zero", got)
	}

	// A same-state reopened webhook after an initial OPEN baseline is not represented by a transition,
	// so its real action remains available to an action-filtered subscriber.
	reopenedSource := g.source(PullRequestSubject{Number: 8}, EventFilter{
		Event: EventPullRequest, Actions: []string{"reopened"},
	})
	reopenedDestination := g.destination
	reopenedDestination.Name = "reopened-pr"
	reopened := g.followIn(t, reopenedDestination, reopenedSource, "reopened-pr")
	g.observePullRequest(t, reopened, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitC, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
	})
	g.ingest(t, lifecycleDelivery(g, 8, GitHubPullRequestOpen, commitC, "main", "reopened"))
	g.reconcileSubscription(t, reopened)
	if got := len(observationEntries(t, g.read(t, reopened.InboxID, 0).Entries)); got != 0 {
		t.Errorf("same-state reopened generated observations = %d, want zero", got)
	}
	if got := g.count(t, `SELECT count(*) FROM subscription_match m JOIN entry e
		ON e.inbox_id = $1 AND e.cursor = m.cursor WHERE m.subscription_id = $2
		AND e.event->>'provider' = 'github' AND e.event->>'event' = 'pull_request'`, reopened.InboxID, reopened.ID); got != 1 {
		t.Errorf("uncovered reopened raw actions = %d, want one", got)
	}
}

func TestGitHubReviewLeaseIsFencedByPRChangeAndInstallationRevocation(t *testing.T) {
	for _, test := range []struct {
		name string
		act  func(*testing.T, githubFixture, githubSubscription)
	}{
		{
			name: "pull request lifecycle change",
			act: func(t *testing.T, g githubFixture, sub githubSubscription) {
				g.observePullRequest(t, sub, GitHubPullRequestObservation{
					BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
					State: GitHubPullRequestClosed, Mergeability: GitHubMergeabilityUnknown,
				})
			},
		},
		{
			name: "installation access revoked",
			act: func(t *testing.T, g githubFixture, _ githubSubscription) {
				g.deliver(t, "installation", withAction("removed"))
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(PullRequestSubject{Number: 7}, EventFilter{Event: EventPullRequestReview}), "fenced-reviews")
			g.observePullRequest(t, sub, GitHubPullRequestObservation{
				BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
				State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
			})
			key := GitHubReviewRefreshKey{AppID: g.app, InstallationID: g.installation, Subject: sub.Subject}
			lease, err := g.state.Acquire(t.Context(), key)
			if err != nil || lease == nil {
				t.Fatalf("review lease = %v, %v", lease, err)
			}
			test.act(t, g, sub)
			err = g.state.Succeed(t.Context(), *lease, GitHubSuccess{
				Fences:  []GitHubAccessFence{g.validate(t, g.access())},
				Reviews: []GitHubReviewObservation{testPRReview(7003, ReviewApproved, commitB)},
			})
			var deferred *RefreshDeferredError
			if !errors.As(err, &deferred) {
				t.Fatalf("stale review success error = %v, want deferred write fence", err)
			}
			if got := g.count(t, `SELECT count(*) FROM github_pr_state_event
				WHERE app_id = $1 AND repository_id = $2 AND pull_request_number = $3 AND event = 'pull_request_review'`,
				g.app, g.repository, 7); got != 0 {
				t.Errorf("stale review observation events = %d, want none", got)
			}
		})
	}
}

func TestGitHubPRStateRetentionKeepsCurrentAndInboxCopies(t *testing.T) {
	g := newGitHubFixture(t)
	source := g.source(PullRequestSubject{Number: 7}, EventFilter{Event: EventPullRequestReview})
	sub := g.follow(t, source, "retained-review")
	g.observePullRequest(t, sub, GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: GitHubPullRequestOpen, Mergeability: GitHubMergeabilityUnknown,
	})
	g.pollReviews(t, sub.Subject, []GitHubReviewObservation{testPRReview(7004, ReviewCommented, commitB)})
	g.reconcileSubscription(t, sub)
	before := observationEntries(t, g.read(t, sub.InboxID, 0).Entries)
	if len(before) != 1 {
		t.Fatalf("pre-retention inbox observations = %d, want one", len(before))
	}
	sequence := before[0].Event.(GitHubObservationEvent).Sequence
	if err := g.CleanupGitHubDeliveries(t.Context(), now().Add(24*time.Hour)); err != nil {
		t.Fatal(err)
	}
	if got := g.count(t, `SELECT count(*) FROM github_pr_state_event
		WHERE app_id = $1 AND repository_id = $2 AND pull_request_number = $3 AND sequence = $4`,
		g.app, g.repository, 7, sequence); got != 0 {
		t.Errorf("expired PR state log rows = %d, want zero", got)
	}
	if got := g.count(t, `SELECT count(*) FROM github_pr_state_current
		WHERE app_id = $1 AND repository_id = $2 AND pull_request_number = $3 AND event = 'pull_request_review'`,
		g.app, g.repository, 7); got != 1 {
		t.Errorf("retained review current rows = %d, want one", got)
	}
	if got := len(observationEntries(t, g.read(t, sub.InboxID, 0).Entries)); got != 1 {
		t.Errorf("retained inbox observation copies = %d, want one", got)
	}

	lateDestination := g.destination
	lateDestination.Name = "after-review-retention"
	late := g.followIn(t, lateDestination, source, "late-after-retention")
	g.reconcileSubscription(t, late)
	lateEntries := observationEntries(t, g.read(t, late.InboxID, 0).Entries)
	if len(lateEntries) != 1 || lateEntries[0].Event.(GitHubObservationEvent).Sequence != sequence {
		t.Errorf("late post-retention snapshots = %+v, want current identity sequence %d", lateEntries, sequence)
	}
}
