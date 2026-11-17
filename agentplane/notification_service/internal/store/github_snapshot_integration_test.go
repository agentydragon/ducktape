package store

import (
	"errors"
	"fmt"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

func publishSnapshotObservations(t *testing.T, g githubFixture, sub githubSubscription, event model.EventName, start, count int) {
	t.Helper()
	if event == model.EventCheckRun {
		runs := make([]model.GitHubCheckRun, count)
		for i := range runs {
			runs[i] = checkRunObservation(g, commitA, model.CheckRunCompleted, "success")
			runs[i].ID = int64(start + i + 1)
			runs[i].Name = fmt.Sprintf("check-%d", start+i)
		}
		g.pollCheckRuns(t, sub.Subject, runs)
	} else {
		reviews := make([]model.GitHubReviewObservation, count)
		for i := range reviews {
			reviews[i] = testPRReview(int64(start+i+1), model.ReviewApproved, commitA)
		}
		g.pollReviews(t, sub.Subject, reviews)
	}
}

func snapshotSubscription(t *testing.T, event model.EventName, count int) (githubFixture, githubSubscription, string) {
	t.Helper()
	g := newGitHubFixture(t)
	source := g.source(model.PullRequestSubject{Number: 7}, model.EventFilter{Event: event})
	seed := g.follow(t, source, "seed")
	g.observePullRequest(t, seed, model.GitHubPullRequestObservation{
		BaseRef: "main", HeadSHA: commitA, BaseSHA: commitB,
		State: model.GitHubPullRequestOpen, Mergeability: model.GitHubMergeable,
		MergeCommit: testMergeCommit(commitB, commitA),
	})
	publishSnapshotObservations(t, g, seed, event, 0, count)
	destination := g.destination
	destination.Name = "snapshot-subscriber"
	sub := g.followIn(t, destination, source, "snapshot")
	table := "github_commit_subscription_snapshot"
	if event == model.EventPullRequestReview {
		table = "github_pr_state_subscription_snapshot"
	}
	if got := g.count(t, `SELECT count(*) FROM `+table+` WHERE subscription_id = $1`, sub.ID); got != count {
		t.Fatalf("creation snapshots = %d, want %d", got, count)
	}
	return g, sub, table
}

func TestGitHubSnapshotPagesResumeInSequenceBeforeTransitions(t *testing.T) {
	for _, event := range []model.EventName{model.EventCheckRun, model.EventPullRequestReview} {
		for _, snapshots := range []int{GitHubDeliveryPageSize - 1, GitHubDeliveryPageSize, GitHubDeliveryPageSize + 1} {
			t.Run(fmt.Sprintf("%s/%d", event, snapshots), func(t *testing.T) {
				g, sub, table := snapshotSubscription(t, event, snapshots)
				// New observations fall after the subscription's immutable creation boundary.
				publishSnapshotObservations(t, g, sub, event, snapshots, 2)
				claim := g.claimInbox(t, sub.InboxID)
				var after, sequence int64
				total := snapshots + 2
				for delivered := 0; delivered < total; {
					// No in-memory continuation is retained between worker attempts.
					g.Store = New(g.db, defaultQuotas())
					if err := g.reconcile(t, claim, sub); err != nil {
						t.Fatal(err)
					}
					page := g.read(t, sub.InboxID, after)
					want := min(GitHubDeliveryPageSize, total-delivered)
					if len(page.Entries) != want {
						t.Fatalf("page after %d entries = %d, want %d", delivered, len(page.Entries), want)
					}
					for _, entry := range page.Entries {
						identity, ok := entry.Event.(model.GitHubObservationEvent)
						if !ok || identity.AppID != g.app || identity.RepositoryID != g.repository || identity.Event != event || identity.Sequence <= sequence {
							t.Fatalf("observation identity = %+v after sequence %d", entry.Event, sequence)
						}
						sequence, after = identity.Sequence, entry.Cursor
					}
					delivered += len(page.Entries)
					remaining := max(0, snapshots-delivered)
					if got := g.count(t, `SELECT count(*) FROM `+table+` WHERE subscription_id = $1`, sub.ID); got != remaining {
						t.Fatalf("remaining snapshots = %d, want %d", got, remaining)
					}
					if delivered < total && !g.subscriptionState(t, sub.ID).nextAttempt.Valid {
						t.Fatal("unfinished page left subscription idle")
					}
				}
				g.reconcileSubscription(t, sub)
				if got := len(g.read(t, sub.InboxID, after).Entries); got != 0 {
					t.Fatalf("replayed entries = %d, want none", got)
				}
				if g.subscriptionState(t, sub.ID).nextAttempt.Valid {
					t.Fatal("drained snapshots and transitions left subscription due")
				}
			})
		}
	}
}

func TestGitHubSnapshotAppendFailureRetainsEntirePageForRestart(t *testing.T) {
	for _, event := range []model.EventName{model.EventCheckRun, model.EventPullRequestReview} {
		t.Run(string(event), func(t *testing.T) {
			g, sub, table := snapshotSubscription(t, event, 2)
			quotas := defaultQuotas()
			quotas.EntriesPerInbox = 1
			g.Store = New(g.db, quotas)
			claim := g.claimInbox(t, sub.InboxID)
			var quota *QuotaError
			if err := g.reconcile(t, claim, sub); !errors.As(err, &quota) || quota.Quota != EntriesPerInbox {
				t.Fatalf("recording error = %v, want inbox entry quota", err)
			}
			if page := g.read(t, sub.InboxID, 0); len(page.Entries) != 0 || page.Inbox.LastCursor != 0 {
				t.Fatalf("failed page retained entries: %+v", page)
			}
			if got := g.count(t, `SELECT count(*) FROM `+table+` WHERE subscription_id = $1`, sub.ID); got != 2 {
				t.Fatalf("snapshots after rollback = %d, want both", got)
			}
			if got := g.count(t, `SELECT count(*) FROM subscription_match WHERE subscription_id = $1`, sub.ID); got != 0 {
				t.Fatalf("matches after rollback = %d, want none", got)
			}
			g.Store = New(g.db, defaultQuotas())
			g.reconcileSubscription(t, sub)
			if got := len(g.read(t, sub.InboxID, 0).Entries); got != 2 {
				t.Fatalf("entries after restart = %d, want both snapshots", got)
			}
			if got := g.count(t, `SELECT count(*) FROM `+table+` WHERE subscription_id = $1`, sub.ID); got != 0 {
				t.Fatalf("snapshots after successful retry = %d, want none", got)
			}
		})
	}
}

func TestGitHubSnapshotsRequireEveryAccessFenceDimension(t *testing.T) {
	for _, event := range []model.EventName{model.EventCheckRun, model.EventPullRequestReview} {
		t.Run(string(event), func(t *testing.T) {
			g, sub, table := snapshotSubscription(t, event, 1)
			extra := ""
			if event == model.EventPullRequestReview {
				extra = ", pull_request_number"
			}
			for i, column := range []string{"app_id", "installation_id", "repository_id"} {
				// Retain a snapshot with one component outside the current fence. A valid neighboring
				// snapshot ensures access filtering cannot pass by excluding the entire subscription.
				g.exec(t, `INSERT INTO `+table+` (subscription_id, app_id, installation_id, repository_id, event, sequence, payload`+extra+`)
					SELECT subscription_id, app_id, installation_id, repository_id, event, sequence + $2, payload`+extra+`
					FROM `+table+` WHERE subscription_id = $1 ORDER BY sequence LIMIT 1`, sub.ID, (i+1)*1000000)
				g.exec(t, `UPDATE `+table+` SET `+column+` = `+column+` + 1000000 WHERE subscription_id = $1 AND
					sequence = (SELECT max(sequence) FROM `+table+` WHERE subscription_id = $1)`, sub.ID)
			}
			g.reconcileSubscription(t, sub)
			entries := g.read(t, sub.InboxID, 0).Entries
			if len(entries) != 1 {
				t.Fatalf("fenced snapshot entries = %d, want only the accessible snapshot", len(entries))
			}
			identity, ok := entries[0].Event.(model.GitHubObservationEvent)
			if !ok || identity.AppID != g.app || identity.RepositoryID != g.repository || identity.Event != event {
				t.Fatalf("accessible snapshot identity = %+v", entries[0].Event)
			}
			if got := g.count(t, `SELECT count(*) FROM `+table+` WHERE subscription_id = $1`, sub.ID); got != 3 {
				t.Fatalf("inaccessible snapshots remaining = %d, want three", got)
			}
		})
	}
}
