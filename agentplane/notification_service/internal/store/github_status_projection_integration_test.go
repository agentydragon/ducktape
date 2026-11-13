package store

import (
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

func TestSharedFailuresAreProjectedWithoutBeingCopiedIntoSubscriptions(t *testing.T) {
	g := newGitHubFixture(t)
	source := g.source(model.PullRequestSubject{Number: 7})
	first, second := g.follow(t, source, "first"), g.follow(t, source, "second")
	lease, err := g.state.Acquire(t.Context(), g.access())
	if err != nil || lease == nil {
		t.Fatalf("lease = %v, %v", lease, err)
	}
	if _, err := g.state.Fail(t.Context(), *lease, model.FailureRateLimited, "limited", time.Minute); err != nil {
		t.Fatal(err)
	}

	listed, err := g.ListSubscriptions(t.Context(), g.incarnation(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(listed) != 2 || listed[0].GitHub == nil || listed[1].GitHub == nil {
		t.Fatalf("listed %+v, want both subscriptions with a GitHub status", listed)
	}
	requireEqual(t, listed[0].GitHub, listed[1].GitHub)
	if kind := listed[0].GitHub.Access[0].Failure.Kind; kind != model.FailureRateLimited {
		t.Errorf("failure kind = %q, want %q", kind, model.FailureRateLimited)
	}
	for _, subscription := range listed {
		if subscription.Failure != nil || subscription.LastSuccessAt != nil || subscription.RetryAt != nil {
			t.Errorf("subscription %s took on the shared failure: %+v", subscription.ID, subscription)
		}
	}
	for _, sub := range []githubSubscription{first, second} {
		if state := g.subscriptionState(t, sub.ID); state.errorKind.Valid {
			t.Errorf("subscription row %s records error kind %q", sub.ID, state.errorKind.String)
		}
	}
	if got := g.count(t, `SELECT count(*) FROM github_repository_access WHERE app_id = $1`, g.app); got != 1 {
		t.Errorf("%d access rows, want the one both share", got)
	}
	if page := g.read(t, g.inbox, 0); page.Inbox.LastCursor != 0 || len(page.Entries) != 0 || page.Notice != nil {
		t.Errorf("page %+v, want an untouched inbox", page)
	}
}

func TestRepositoryRenamePreservesSubscriptionSource(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
	lease, err := g.state.Acquire(t.Context(), g.access())
	if err != nil || lease == nil {
		t.Fatalf("lease = %v, %v", lease, err)
	}
	renamed := "test-owner/renamed"
	if err := g.state.CompleteAccess(t.Context(), *lease, &renamed); err != nil {
		t.Fatal(err)
	}
	// The binding is numeric; the subscription retains the name requested by its caller.
	if source := g.get(t, sub.ID).Source.(model.GitHubSource); source.Repository != testRepositoryName {
		t.Errorf("subscription follows %q, want the name it was created with", source.Repository)
	}
}

func TestSharedRefreshDoesNotChangeSubscriptionOrInbox(t *testing.T) {
	for _, success := range []bool{false, true} {
		t.Run(map[bool]string{false: "failure", true: "success"}[success], func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
			lease, err := g.state.Acquire(t.Context(), g.access())
			if err != nil || lease == nil {
				t.Fatalf("lease = %v, %v", lease, err)
			}
			if success {
				err = g.state.CompleteAccess(t.Context(), *lease, nil)
			} else {
				_, err = g.state.Fail(t.Context(), *lease, model.FailureRateLimited, "limited", time.Minute)
			}
			if err != nil {
				t.Fatal(err)
			}
			if subscription := g.get(t, sub.ID); subscription.Failure != nil || subscription.LastSuccessAt != nil || subscription.RetryAt != nil {
				t.Errorf("subscription %+v took on the shared grant's state", subscription)
			}
			if page := g.read(t, g.inbox, 0); page.Inbox.LastCursor != 0 || len(page.Entries) != 0 || page.Notice != nil {
				t.Errorf("page %+v, want an untouched inbox", page)
			}
		})
	}
}
