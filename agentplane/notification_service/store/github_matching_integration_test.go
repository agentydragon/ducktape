package store

import (
	"bytes"
	"fmt"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// reconcileAll matches and records a page for each subscription under one claim of the fixture's inbox.
func (g githubFixture) reconcileAll(t *testing.T, subs ...githubSubscription) {
	t.Helper()
	claim := g.claimInbox(t, g.inbox)
	for _, sub := range subs {
		if err := g.reconcile(t, claim, sub); err != nil {
			t.Fatal(err)
		}
	}
}

func (g githubFixture) requireDelivered(t *testing.T, want ...string) {
	t.Helper()
	if got := g.delivered(t); !slices.Equal(got, want) {
		t.Fatalf("delivered %q, want %q", got, want)
	}
}

func sortedIDs(ids ...uuid.UUID) []uuid.UUID {
	slices.SortFunc(ids, func(a, b uuid.UUID) int { return bytes.Compare(a[:], b[:]) })
	return ids
}

func TestOnlyDeliveriesAfterTheSubscriptionStartAreOwed(t *testing.T) {
	g := newGitHubFixture(t)
	source := g.source(model.PullRequestSubject{Number: 7})
	g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("before the subscription"))
	first := g.follow(t, source, "first")
	second := g.follow(t, source, "overlap")
	g.deliver(t, "issue_comment", withAction("created"), onPullRequest(8), labelled("another pull request"))
	g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("owed"))

	g.reconcileAll(t, first, second)

	page := g.read(t, g.inbox, 0)
	g.requireDelivered(t, "owed")
	// Two subscriptions that follow the same subject share the one entry.
	requireEqual(t, page.Entries[0].SubscriptionIDs, sortedIDs(first.ID, second.ID))
	if page.Inbox.Acknowledged != 0 {
		t.Errorf("acknowledged = %d, want delivery to leave it alone", page.Inbox.Acknowledged)
	}
}

func TestRedeliveryIsNotDeliveredAgain(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
	once := g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("once"))
	g.reconcileAll(t, sub)

	if g.ingest(t, once) {
		t.Fatal("the redelivery was stored")
	}
	if step := g.prepare(t, sub); len(step.Matched) != 0 {
		t.Errorf("matched %d deliveries after the one delivered, want none", len(step.Matched))
	}
	g.reconcileAll(t, sub)
	g.requireDelivered(t, "once")
	if got := g.deliveries(t); got != 1 {
		t.Errorf("%d deliveries stored, want 1", got)
	}
}

func TestIssueAndPullRequestSubjectsAreKeptApart(t *testing.T) {
	g := newGitHubFixture(t)
	issue := g.source(model.IssueSubject{Number: 7})
	closed := g.source(model.IssueSubject{Number: 7}, model.EventFilter{Event: model.EventIssues, Actions: []string{"closed"}})
	pullRequest := g.source(model.PullRequestSubject{Number: 7})
	subs := []githubSubscription{g.follow(t, issue, "issue"), g.follow(t, closed, "closed"), g.follow(t, pullRequest, "pull-request")}

	g.deliver(t, "issues", withAction("opened"), onIssue(7), labelled("opened"))
	g.deliver(t, "issues", withAction("closed"), onIssue(7), labelled("closed"))
	g.deliver(t, "issue_comment", withAction("created"), onIssue(7), labelled("issue comment"))
	// GitHub tells a pull request's comment from an issue's by the marker the issue carries, which the stored
	// delivery shows as a link to the pull request.
	g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("pull request comment"))
	g.deliver(t, "issues", withAction("opened"), onIssue(8), labelled("another issue"))

	g.reconcileAll(t, subs...)

	page := g.read(t, g.inbox, 0)
	g.requireDelivered(t, "opened", "closed", "issue comment", "pull request comment")
	var matched [][]uuid.UUID
	for _, entry := range page.Entries {
		matched = append(matched, entry.SubscriptionIDs)
	}
	requireEqual(t, matched, [][]uuid.UUID{
		{subs[0].ID},
		sortedIDs(subs[0].ID, subs[1].ID),
		{subs[0].ID},
		{subs[2].ID},
	})
}

func TestDefaultFiltersMatchBySubjectLinkOrByCommit(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
	g.addRevisions(t, sub.Subject, model.GitHubHeadRevision{Repository: model.GitHubRepository{ID: g.repository, Name: testRepositoryName}, SHA: commitA})

	g.deliver(t, "check_run", withAction("created"), atCommit(commitA), labelled("check run not completed"))
	g.deliver(t, "check_run", withAction("completed"), atCommit(commitA), labelled("check run"))
	g.deliver(t, "status", atCommit(commitA), labelled("status"))
	g.deliver(t, "check_suite", withAction("completed"), atCommit(commitA), labelled("check suite"))
	g.deliver(t, "workflow_run", withAction("completed"), atCommit(commitA), labelled("workflow run"))
	g.deliver(t, "pull_request", withAction("opened"), onPullRequest(7), atCommit(commitA), labelled("pull request"))
	g.deliver(t, "pull_request_review", withAction("submitted"), onPullRequest(7), labelled("review"))
	g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("comment"))
	g.deliver(t, "push", onBranch("devel"), atCommit(commitA), labelled("push"))
	g.deliver(t, "issues", withAction("opened"), onIssue(7), labelled("issue"))
	g.deliver(t, "pull_request", withAction("opened"), onPullRequest(8), labelled("another pull request"))
	g.deliver(t, "check_run", withAction("completed"), atCommit(commitC), labelled("check run of another commit"))

	g.reconcileAll(t, sub)

	g.requireDelivered(t, "check run", "status", "pull request", "review", "comment")
}

func TestWorkflowJobsMatchByCommitAndAction(t *testing.T) {
	subjects := map[string]model.GitHubSubject{
		"pull request": model.PullRequestSubject{Number: 7},
		"branch":       model.BranchSubject{Name: "devel"},
		"commit":       model.CommitSubject{SHA: commitA},
	}
	for name, subject := range subjects {
		t.Run(name, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(subject, model.EventFilter{Event: model.EventWorkflowJob, Actions: []string{"completed"}}), "follow")
			g.addRevisions(t, sub.Subject, model.GitHubHeadRevision{Repository: model.GitHubRepository{ID: g.repository, Name: testRepositoryName}, SHA: commitA})

			g.deliver(t, "workflow_job", withAction("in_progress"), atCommit(commitA), labelled("in progress"))
			g.deliver(t, "workflow_job", withAction("completed"), atCommit(commitB), labelled("another commit"))
			g.deliver(t, "workflow_job", withAction("completed"), atCommit(commitA), labelled("completed"))

			g.reconcileAll(t, sub)

			g.requireDelivered(t, "completed")
		})
	}
}

func TestBranchFollowsItsActivityAndCommitFollowsOnlyItsCI(t *testing.T) {
	for name, subject := range map[string]model.GitHubSubject{"branch": model.BranchSubject{Name: "devel"}, "commit": model.CommitSubject{SHA: commitA}} {
		t.Run(name, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(subject), "follow")
			if name == "commit" {
				// A fixed commit is the revision its refresh records.
				g.addRevisions(t, sub.Subject, model.GitHubHeadRevision{Repository: model.GitHubRepository{ID: g.repository, Name: testRepositoryName}, SHA: commitA})
			}

			g.deliver(t, "push", onBranch("devel"), atCommit(commitA), labelled("push"))
			g.deliver(t, "check_run", withAction("completed"), atCommit(commitA), labelled("check run"))
			g.deliver(t, "delete", onBranch("devel"), labelled("delete"))
			g.deliver(t, "check_run", withAction("completed"), atCommit(commitB), labelled("check run of another commit"))

			g.reconcileAll(t, sub)

			if name == "branch" {
				// The check run of the other commit is not the branch's: nothing associated it.
				g.requireDelivered(t, "push", "check run", "delete")
			} else {
				g.requireDelivered(t, "check run")
			}
		})
	}
}

func TestACommitSeenLaterBringsInTheDeliveriesThatWaitedForIt(t *testing.T) {
	for _, kind := range []string{"pull request", "branch"} {
		for _, ciFirst := range []bool{true, false} {
			t.Run(fmt.Sprintf("%s/ci first=%v", kind, ciFirst), func(t *testing.T) {
				g := newGitHubFixture(t)
				var subject model.GitHubSubject = model.PullRequestSubject{Number: 7}
				association := func() {
					g.deliver(t, "pull_request", withAction("synchronize"), onPullRequest(7), atCommit(commitB), labelled("association"))
				}
				if kind == "branch" {
					subject = model.BranchSubject{Name: "devel"}
					association = func() { g.deliver(t, "push", onBranch("devel"), atCommit(commitB), labelled("association")) }
				}
				// The check run before the subscription is no replay when the commit turns out to be the subject's.
				g.deliver(t, "check_run", withAction("completed"), atCommit(commitB), labelled("before the subscription"))
				sub := g.follow(t, g.source(subject), "follow")
				g.addRevisions(t, sub.Subject, model.GitHubHeadRevision{Repository: model.GitHubRepository{ID: g.repository, Name: testRepositoryName}, SHA: commitA})
				if ciFirst {
					g.deliver(t, "check_run", withAction("completed"), atCommit(commitB), labelled("check run of the new commit"))
				} else {
					association()
				}
				// More unrelated deliveries than a page cannot hide a match that comes after them.
				for range GitHubDeliveryPageSize + 2 {
					g.deliver(t, "check_run", withAction("completed"), atCommit(commitC), labelled("unrelated"))
				}
				g.deliver(t, "check_run", withAction("completed"), atCommit(commitA), labelled("check run of the known commit"))

				g.reconcileAll(t, sub)
				if ciFirst {
					g.requireDelivered(t, "check run of the known commit")
				} else {
					g.requireDelivered(t, "association", "check run of the known commit")
				}

				if ciFirst {
					association()
				} else {
					g.deliver(t, "check_run", withAction("completed"), atCommit(commitB), labelled("check run of the new commit"))
				}
				g.reconcileAll(t, sub)
				g.reconcileAll(t, sub)

				if ciFirst {
					g.requireDelivered(t, "check run of the known commit", "check run of the new commit", "association")
				} else {
					g.requireDelivered(t, "association", "check run of the known commit", "check run of the new commit")
				}
			})
		}
	}
}

func TestCIDeliveriesWithTheirOwnSubjectReferencesAssociateCommits(t *testing.T) {
	for _, event := range []string{"check_run", "check_suite", "status", "workflow_run", "workflow_job"} {
		t.Run(event, func(t *testing.T) {
			g := newGitHubFixture(t)
			var subject model.GitHubSubject = model.BranchSubject{Name: "devel"}
			link := onBranch("devel")
			if event == "check_run" || event == "check_suite" {
				subject, link = model.PullRequestSubject{Number: 7}, onPullRequest(7)
			}
			byDefault := g.follow(t, g.source(subject), "default")
			explicit := g.follow(t, g.source(subject, model.EventFilter{Event: model.EventName(event)}), "explicit")

			// A check that names no subject comes first, and only the later delivery says which commit it is for.
			g.deliver(t, "check_run", withAction("completed"), atCommit(commitB), labelled("check without a subject"))
			var options []deliveryOption
			if event != "status" {
				options = append(options, withAction("completed"))
			}
			g.deliver(t, event, append(options, atCommit(commitB), link, labelled("association"))...)

			g.reconcileAll(t, byDefault, explicit)

			// Both subscriptions share their inbox, so an entry is told apart by who matched it.
			matchedBy := map[string][]string{}
			for _, entry := range g.read(t, g.inbox, 0).Entries {
				for _, id := range entry.SubscriptionIDs {
					name := map[uuid.UUID]string{byDefault.ID: "default", explicit.ID: "explicit"}[id]
					matchedBy[name] = append(matchedBy[name], labels(t, []Entry{entry})[0])
				}
			}
			wantDefault := []string{"check without a subject"}
			if event == "check_run" || event == "status" {
				wantDefault = append(wantDefault, "association")
			}
			wantExplicit := []string{"association"}
			if event == "check_run" {
				wantExplicit = []string{"check without a subject", "association"}
			}
			requireEqual(t, matchedBy, map[string][]string{"default": wantDefault, "explicit": wantExplicit})
		})
	}
}

func TestMatchingPagesAndLongRevisionHistoryAreNotCapped(t *testing.T) {
	g := newGitHubFixture(t)
	source := g.source(model.PullRequestSubject{Number: 7}, model.EventFilter{Event: model.EventCheckRun, Actions: []string{"completed"}})
	// The subject's history of commits is longer than a page, and predates the subscription.
	for i := range GitHubDeliveryPageSize + 2 {
		g.deliver(t, "pull_request", withAction("synchronize"), onPullRequest(7), atCommit(fmt.Sprintf("%040x", i)))
	}
	sub := g.follow(t, source, "follow")
	g.deliver(t, "check_run", withAction("created"), atCommit(fmt.Sprintf("%040x", 1)), labelled("not completed"))
	for i := range GitHubDeliveryPageSize + 2 {
		g.deliver(t, "check_run", withAction("completed"), atCommit(fmt.Sprintf("%040x", 1)), labelled(fmt.Sprint("check run ", i)))
	}

	claim := g.claimInbox(t, g.inbox)
	if err := g.reconcile(t, claim, sub); err != nil {
		t.Fatal(err)
	}
	if got := g.read(t, g.inbox, 0).Inbox.LastCursor; got != GitHubDeliveryPageSize {
		t.Fatalf("first page of %d, want %d", got, GitHubDeliveryPageSize)
	}
	// A full page leaves the subscription due at once.
	if next := g.subscriptionState(t, sub.ID).nextAttempt; !next.Valid || next.Time.After(now()) {
		t.Errorf("next attempt = %v, want it due for the rest of the matches", next)
	}
	if err := g.reconcile(t, claim, sub); err != nil {
		t.Fatal(err)
	}
	// A read is a page too: the rest of the matches are the entries after the first page's.
	if got := len(g.read(t, g.inbox, GitHubDeliveryPageSize).Entries); got != 2 {
		t.Errorf("%d entries after the first page, want 2", got)
	}
	// Nothing left to match: the next look is the scheduled repair, not another immediate one.
	if next := g.subscriptionState(t, sub.ID).nextAttempt; !next.Valid || !next.Time.After(now()) {
		t.Errorf("next attempt = %v, want the later repair", next)
	}
}

func TestADeliveryOfAForkNeedsTheForksGrant(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
	forkRepository := g.fork(1000)
	const forkInstallation, strangerInstallation = 22, 33
	fork := g.grant(t, g.installation+forkInstallation, forkRepository)
	g.deliver(t, "pull_request", withAction("synchronize"), onPullRequest(7), atCommit(commitB), inFork(forkRepository.ID, forkRepository.Name), labelled("fork head"))
	forkCheck := func(installation int64, label string) {
		g.deliver(t, "check_run", withAction("completed"), inRepository(forkRepository.ID, forkRepository.Name), atCommit(commitB),
			inInstallation(installation), labelled(label))
	}
	forkCheck(g.installation+forkInstallation, "fork check")
	// The same commit in an unrelated installation is never admitted.
	forkCheck(g.installation+strangerInstallation, "stranger check")

	// The installation has no access to the fork yet: its deliveries are not owed.
	lease, err := g.state.Acquire(t.Context(), fork)
	if err != nil || lease == nil {
		t.Fatalf("lease = %v, %v", lease, err)
	}
	if _, err := g.state.Fail(t.Context(), *lease, FailureAccessDenied, "not installed", 10*time.Minute); err != nil {
		t.Fatal(err)
	}
	g.reconcileAll(t, sub)
	g.requireDelivered(t, "fork head")

	// Once it has, they are, and a webhook wakes the source to take them.
	g.exec(t, `
		UPDATE github_repository_access SET error = NULL, error_kind = NULL, error_since = NULL, error_observed_at = NULL,
			next_attempt = NULL WHERE app_id = $1 AND repository_id = $2`, g.app, forkRepository.ID)
	g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("comment"))
	g.reconcileAll(t, sub)

	g.requireDelivered(t, "fork head", "fork check", "comment")
}
