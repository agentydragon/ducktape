package store

import (
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
)

// awaitBound only fails a test whose wakeup never comes.
const awaitBound = 30 * time.Second

// observeWakeups returns a check of whether a step wakes workers.
func observeWakeups(t *testing.T, f fixture) func(name string, want bool, step func()) {
	t.Helper()
	const sentinel = "notification_store_test_sentinel"
	hub, err := wakeups.Listen(t.Context(), integrationDatabase.URL, WorkChannel, sentinel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	work, stopWork, err := hub.Subscribe(WorkChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(stopWork)
	settled, stopSettled, err := hub.Subscribe(sentinel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(stopSettled)

	// The hub applies notifications in the order PostgreSQL delivers them, so once the sentinel's has arrived,
	// every notification the step committed before it has too.
	wakes := func(step func()) bool {
		t.Helper()
		step()
		f.exec(t, "SELECT pg_notify($1, '')", sentinel)
		select {
		case <-settled:
		case <-time.After(awaitBound):
			t.Fatal("no wakeup from the sentinel")
		}
		select {
		case <-work:
			return true
		default:
			return false
		}
	}
	return func(name string, want bool, step func()) {
		t.Helper()
		if got := wakes(step); got != want {
			t.Errorf("%s woke workers = %v, want %v", name, got, want)
		}
	}
}

func TestWritesThatChangeWhatWorkersFollowWakeThemAndOthersDoNot(t *testing.T) {
	f := newFixture(t, defaultQuotas())
	check := observeWakeups(t, f)

	creation := f.body("first")
	var subscription Subscription
	check("creating a subscription", true, func() { subscription = f.subscribe(t, creation) })
	check("replaying a creation", false, func() { f.subscribe(t, creation) })
	reused := creation
	reused.LifetimeDays = 30
	check("a refused creation", false, func() {
		if _, err := f.Subscribe(t.Context(), f.owner, reused, nil, "test-account-uid"); !errors.Is(err, ErrIdempotencyKeyReused) {
			t.Error(err)
		}
	})
	check("reading", false, func() { f.read(t, subscription.InboxID, 0) })
	check("appending an event", false, func() {
		event := model.ActionsEvent{RequestID: creation.Source.(model.ActionsSource).RequestID, Sequence: 1}
		f.mustAppendEvent(t, subscription.InboxID, subscription.ID, event, payloadOf(event))
	})
	check("acknowledging", true, func() { f.acknowledge(t, subscription.InboxID, 1) })
	check("renewing", true, func() { f.renew(t, subscription.ID, 1, 7) })
	check("cancelling", true, func() { f.cancel(t, subscription.ID) })
	check("cancelling a cancelled subscription", false, func() { f.cancel(t, subscription.ID) })
	check("retiring", true, func() {
		if err := f.Retire(t.Context(), f.incarnation(), subscription.InboxID); err != nil {
			t.Error(err)
		}
	})
	check("retiring a retired inbox", false, func() {
		if err := f.Retire(t.Context(), f.incarnation(), subscription.InboxID); err != nil {
			t.Error(err)
		}
	})
}

func TestSharedGitHubChangesWakeOnlyAffectedSessionWatches(t *testing.T) {
	first, other := newGitHubFixture(t), newGitHubFixture(t)
	sub := first.follow(t, first.source(model.PullRequestSubject{Number: 7}), "first")
	other.follow(t, other.source(model.PullRequestSubject{Number: 7}), "other")
	const barrier = "notification_session_watch_barrier"
	hub, err := wakeups.Listen(t.Context(), integrationDatabase.URL, InboxChannel, barrier)
	if err != nil {
		t.Fatal(err)
	}
	defer hub.Close()
	firstSignal, stopFirst, err := hub.SubscribeKey(InboxChannel, first.sessionID)
	if err != nil {
		t.Fatal(err)
	}
	defer stopFirst()
	otherSignal, stopOther, err := hub.SubscribeKey(InboxChannel, other.sessionID)
	if err != nil {
		t.Fatal(err)
	}
	defer stopOther()
	settled, stopSettled, err := hub.Subscribe(barrier)
	if err != nil {
		t.Fatal(err)
	}
	defer stopSettled()
	for name, update := range map[string]func(){
		"repository access": func() {
			first.exec(t, `UPDATE github_repository_access SET checked_at=now() WHERE app_id=$1 AND installation_id=$2 AND repository_id=$3`, first.app, first.installation, first.repository)
		},
		"subject": func() {
			first.exec(t, `UPDATE github_subject SET last_success_at=now() WHERE repository_id=$1 AND kind=$2 AND subject_key=$3`, sub.Subject.RepositoryID, sub.Subject.Kind, sub.Subject.Key)
		},
	} {
		update()
		first.exec(t, `SELECT pg_notify($1, '')`, barrier)
		select {
		case <-settled:
		case <-time.After(awaitBound):
			t.Fatal("no barrier")
		}
		select {
		case <-firstSignal:
		default:
			t.Errorf("%s did not wake the affected Session", name)
		}
		select {
		case <-otherSignal:
			t.Errorf("%s woke an unrelated Session", name)
		default:
		}
	}
}
