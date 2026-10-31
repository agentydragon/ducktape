package store

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/google/uuid"
)

// requireInboxUntouched fails unless nothing was appended to the fixture's inbox.
func (g githubFixture) requireInboxUntouched(t *testing.T) {
	t.Helper()
	page := g.read(t, g.inbox, 0)
	if len(page.Entries) != 0 || page.Inbox.LastCursor != 0 || page.Notice != nil {
		t.Fatalf("inbox holds %d entries up to cursor %d, notice %v, want nothing", len(page.Entries), page.Inbox.LastCursor, page.Notice)
	}
}

func (g githubFixture) inboxUpdatedAt(t *testing.T) time.Time {
	t.Helper()
	var updated time.Time
	if err := g.db.QueryRowContext(t.Context(), `SELECT updated_at FROM inbox WHERE id = $1`, g.inbox).Scan(&updated); err != nil {
		t.Fatal(err)
	}
	return updated
}

func (g githubFixture) claimHolder(t *testing.T) uuid.NullUUID {
	t.Helper()
	var holder uuid.NullUUID
	if err := g.db.QueryRowContext(t.Context(), `SELECT claim FROM inbox WHERE id = $1`, g.inbox).Scan(&holder); err != nil {
		t.Fatal(err)
	}
	return holder
}

func TestRecordingAPageDeliversItAndSchedulesTheNextLook(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
	g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("comment"))
	// A failure from an earlier attempt ends with the next success.
	g.exec(t, `
		UPDATE subscription SET error = 'rate limited', error_kind = 'rate_limited', error_since = now(), error_observed_at = now()
		WHERE id = $1`, sub.ID)
	g.exec(t, `UPDATE inbox SET updated_at = now() - interval '1 hour' WHERE id = $1`, g.inbox)
	before := g.inboxUpdatedAt(t)
	claim := g.claimInbox(t, g.inbox)

	step := g.prepare(t, sub)
	if err := g.record(t, claim, step); err != nil {
		t.Fatal(err)
	}

	g.requireDelivered(t, "comment")
	state := g.subscriptionState(t, sub.ID)
	if !state.lastSuccess.Valid || state.errorKind.Valid {
		t.Errorf("subscription state = %+v, want a success and no failure", state)
	}
	// The next look is the repair, when the grant or the subject goes stale, not before.
	if !state.nextAttempt.Valid || !state.nextAttempt.Time.Equal(step.RepairAt) {
		t.Errorf("next attempt = %v, want the repair at %v", state.nextAttempt, step.RepairAt)
	}
	if after := g.inboxUpdatedAt(t); !after.After(before) {
		t.Errorf("inbox updated at %v, want it moved on from %v", after, before)
	}

	// A page with nothing in it records the look and leaves the inbox alone.
	g.exec(t, `UPDATE inbox SET updated_at = now() - interval '1 hour' WHERE id = $1`, g.inbox)
	before = g.inboxUpdatedAt(t)
	empty := g.prepare(t, sub)
	if len(empty.Matched) != 0 {
		t.Fatalf("matched %d deliveries again", len(empty.Matched))
	}
	if err := g.record(t, claim, empty); err != nil {
		t.Fatal(err)
	}
	if after := g.inboxUpdatedAt(t); !after.Equal(before) {
		t.Errorf("inbox updated at %v, want it left at %v", after, before)
	}
	g.requireDelivered(t, "comment")
}

func TestRecordingDoesNothingForASubscriptionThatMovedOn(t *testing.T) {
	for name, change := range map[string]func(g githubFixture, t *testing.T, sub githubSubscription){
		"cancelled": func(g githubFixture, t *testing.T, sub githubSubscription) { g.cancel(t, sub.ID) },
		"renewed":   func(g githubFixture, t *testing.T, sub githubSubscription) { g.renew(t, sub.ID, 1, 7) },
		"expired": func(g githubFixture, t *testing.T, sub githubSubscription) {
			g.exec(t, `UPDATE subscription SET expires_at = now() - interval '1 second' WHERE id = $1`, sub.ID)
		},
	} {
		t.Run(name, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
			g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("comment"))
			claim := g.claimInbox(t, g.inbox)
			step := g.prepare(t, sub)
			if len(step.Matched) != 1 {
				t.Fatalf("matched %d deliveries, want 1", len(step.Matched))
			}

			change(g, t, sub)
			changed := g.subscriptionState(t, sub.ID)
			if err := g.record(t, claim, step); err != nil {
				t.Fatal(err)
			}

			g.requireInboxUntouched(t)
			after := g.subscriptionState(t, sub.ID)
			if after.lastSuccess.Valid || !after.nextAttempt.Time.Equal(changed.nextAttempt.Time) {
				t.Errorf("subscription state = %+v, want it left as %+v", after, changed)
			}
		})
	}
}

func TestRecordingNeedsTheClaimOfTheInbox(t *testing.T) {
	for name, lose := range map[string]string{
		"taken over": `UPDATE inbox SET claim = gen_random_uuid() WHERE id = $1`,
		"expired":    `UPDATE inbox SET claim_until = now() - interval '1 second' WHERE id = $1`,
		"retired":    `UPDATE inbox SET retired = true WHERE id = $1`,
	} {
		t.Run(name, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
			g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("comment"))
			claim := g.claimInbox(t, g.inbox)
			step := g.prepare(t, sub)
			g.exec(t, lose, g.inbox)

			if err := g.record(t, claim, step); !errors.Is(err, ErrClaimLost) {
				t.Fatalf("recording = %v, want ErrClaimLost", err)
			}
			g.requireInboxUntouched(t)
			if state := g.subscriptionState(t, sub.ID); state.lastSuccess.Valid {
				t.Errorf("last success = %v, want none recorded", state.lastSuccess)
			}
		})
	}
}

func TestIngressAfterTheSnapshotLeavesTheSubscriptionDueAtOnce(t *testing.T) {
	for name, deliverBefore := range map[string]bool{"a page with a match": true, "an empty page": false} {
		t.Run(name, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
			if deliverBefore {
				g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("first"))
			}
			claim := g.claimInbox(t, g.inbox)
			step := g.prepare(t, sub)
			// Another receipt commits after the worker read the subscription.
			g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("second"))

			if err := g.record(t, claim, step); err != nil {
				t.Fatal(err)
			}

			// The snapshot's idle schedule must not overwrite what the ingress made due.
			if next := g.subscriptionState(t, sub.ID).nextAttempt; !next.Valid || next.Time.After(now()) {
				t.Errorf("next attempt = %v, want the subscription due at once", next)
			}
			if err := g.reconcile(t, claim, sub); err != nil {
				t.Fatal(err)
			}
			want := []string{"second"}
			if deliverBefore {
				want = []string{"first", "second"}
			}
			g.requireDelivered(t, want...)
			if next := g.subscriptionState(t, sub.ID).nextAttempt; !next.Valid || !next.Time.After(now()) {
				t.Errorf("next attempt = %v, want the later repair", next)
			}
		})
	}
}

func TestAPageReadUnderAFenceThatNoLongerHoldsIsNotRecorded(t *testing.T) {
	for name, invalidate := range map[string]func(g githubFixture, t *testing.T, step *githubStep){
		"the installation is suspended": func(g githubFixture, t *testing.T, _ *githubStep) {
			g.deliver(t, "installation", withAction("suspend"))
		},
		"the grant expires": func(g githubFixture, t *testing.T, _ *githubStep) {
			g.exec(t, `UPDATE github_repository_access SET valid_until = now() WHERE app_id = $1`, g.app)
		},
		"the grant fails": func(g githubFixture, t *testing.T, _ *githubStep) {
			g.exec(t, `
				UPDATE github_repository_access SET error = 'not installed', error_kind = 'access_denied', error_since = now(),
					error_observed_at = now(), valid_until = NULL WHERE app_id = $1`, g.app)
		},
		"the subject is refreshed again": func(g githubFixture, t *testing.T, _ *githubStep) {
			g.exec(t, `UPDATE github_subject SET generation = generation + 1 WHERE repository_id = $1`, g.repository)
		},
		"the repair is due": func(_ githubFixture, _ *testing.T, step *githubStep) { step.RepairAt = now().Add(-time.Second) },
		"no grant was read": func(_ githubFixture, _ *testing.T, step *githubStep) { step.Fences = nil },
	} {
		t.Run(name, func(t *testing.T) {
			g := newGitHubFixture(t)
			sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
			g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("comment"))
			claim := g.claimInbox(t, g.inbox)
			step := g.prepare(t, sub)
			g.exec(t, `UPDATE subscription SET next_attempt = now() + interval '1 hour' WHERE id = $1`, sub.ID)

			invalidate(g, t, &step)
			if err := g.record(t, claim, step); err != nil {
				t.Fatal(err)
			}

			// Nothing is appended and no notice is made for the page; the subscription is looked at again at once,
			// and the worker keeps its claim.
			g.requireInboxUntouched(t)
			state := g.subscriptionState(t, sub.ID)
			if state.lastSuccess.Valid || !state.nextAttempt.Valid || state.nextAttempt.Time.After(now()) {
				t.Errorf("subscription state = %+v, want no success and due at once", state)
			}
			if holder := g.claimHolder(t); !holder.Valid || holder.UUID != claim.held.Holder {
				t.Errorf("claim = %v, want the worker's %s", holder, claim.held.Holder)
			}
		})
	}
}

// expireAccessWhenAppended makes every grant of the App expire as soon as an entry is appended to the inbox, as if
// the page took longer than the grant had left.
func (g githubFixture) expireAccessWhenAppended(t *testing.T) {
	t.Helper()
	name := "expire_access_" + strings.ReplaceAll(uuid.NewString(), "-", "_")
	g.exec(t, fmt.Sprintf(`
		CREATE FUNCTION %[1]s() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN
			UPDATE github_repository_access SET valid_until = now() - interval '1 second' WHERE app_id = %[2]d;
			RETURN NEW;
		END $$`, name, g.app))
	g.exec(t, fmt.Sprintf(`CREATE TRIGGER %[1]s AFTER INSERT ON entry FOR EACH ROW WHEN (NEW.inbox_id = '%[2]s') EXECUTE FUNCTION %[1]s()`,
		name, g.inbox))
	t.Cleanup(func() {
		for _, statement := range []string{"DROP TRIGGER " + name + " ON entry", "DROP FUNCTION " + name + "()"} {
			if _, err := g.db.ExecContext(context.Background(), statement); err != nil {
				t.Error(err)
			}
		}
	})
}

func TestAGrantExpiringWhileAPageIsAppendedRollsTheWholePageBack(t *testing.T) {
	g := newGitHubFixture(t)
	sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
	g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("first"))
	g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("second"))
	claim := g.claimInbox(t, g.inbox)
	step := g.prepare(t, sub)
	if len(step.Matched) != 2 {
		t.Fatalf("matched %d deliveries, want 2", len(step.Matched))
	}
	g.expireAccessWhenAppended(t)

	var deferred *RefreshDeferredError
	if err := g.record(t, claim, step); !errors.As(err, &deferred) {
		t.Fatalf("recording = %v, want it deferred", err)
	}

	g.requireInboxUntouched(t)
	if state := g.subscriptionState(t, sub.ID); state.lastSuccess.Valid {
		t.Errorf("last success = %v, want none recorded", state.lastSuccess)
	}
	if got := g.count(t, `SELECT count(*) FROM subscription_match WHERE subscription_id = $1`, sub.ID); got != 0 {
		t.Errorf("%d matches kept, want none", got)
	}
}

func TestRecordingWakesWorkersOnlyWhenItChangesWhatTheyFollow(t *testing.T) {
	g := newGitHubFixture(t)
	check := observeWakeups(t, g.fixture)
	sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7}), "follow")
	g.deliver(t, "issue_comment", withAction("created"), onPullRequest(7), labelled("comment"))
	claim := g.claimInbox(t, g.inbox)
	step := g.prepare(t, sub)
	stale := step
	stale.Fences = nil

	record := func(step githubStep) func() {
		return func() {
			if err := g.record(t, claim, step); err != nil {
				t.Error(err)
			}
		}
	}
	check("a page read under no fence", true, record(stale))
	check("a page", true, record(step))
	check("cancelling", true, func() { g.cancel(t, sub.ID) })
	check("a page for a cancelled subscription", false, record(step))
	check("a page without the claim", false, func() {
		g.exec(t, `UPDATE inbox SET claim = gen_random_uuid() WHERE id = $1`, g.inbox)
		if err := g.record(t, claim, step); !errors.Is(err, ErrClaimLost) {
			t.Error(err)
		}
	})
}

func TestDeliveryAlreadyRecordedIsNotRecordedAgain(t *testing.T) {
	g := newGitHubFixture(t)
	action := "created"
	sub := g.follow(t, g.source(model.PullRequestSubject{Number: 7},
		model.EventFilter{Event: model.EventIssueComment}), "follow")
	delivery := g.deliver(t, string(model.EventIssueComment), onPullRequest(7), withAction(action), labelled("recorded"))
	event := model.GitHubEvent{
		AppID: g.app, DeliveryID: delivery.DeliveryID, RepositoryID: g.repository,
		Event: model.EventIssueComment, Action: &action,
	}
	tx, err := g.db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := insertEntry(t.Context(), tx, g.inbox, 1, event, delivery.Payload, time.Now().UTC()); err != nil {
		_ = tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	g.exec(t, `INSERT INTO subscription_match (subscription_id, cursor) VALUES ($1, 1)`, sub.ID)
	g.exec(t, `UPDATE inbox SET last_cursor = 1 WHERE id = $1`, g.inbox)

	g.reconcileAll(t, sub)
	if page := g.read(t, g.inbox, 0); len(page.Entries) != 1 || page.Inbox.LastCursor != 1 {
		t.Fatalf("inbox holds %d entries up to cursor %d, want the existing entry only", len(page.Entries), page.Inbox.LastCursor)
	}

	g.deliver(t, string(model.EventIssueComment), onPullRequest(7), withAction(action), labelled("control"))
	g.reconcileAll(t, sub)
	g.requireDelivered(t, "recorded", "control")
}
