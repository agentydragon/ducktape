package store

import (
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
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

	creation := body("first")
	var subscription Subscription
	check("creating a subscription", true, func() { subscription = f.subscribe(t, creation) })
	check("replaying a creation", false, func() { f.subscribe(t, creation) })
	reused := creation
	reused.LifetimeDays = 30
	check("a refused creation", false, func() {
		if _, err := f.Subscribe(t.Context(), f.owner, reused, nil); !errors.Is(err, ErrIdempotencyKeyReused) {
			t.Error(err)
		}
	})
	check("reading", false, func() { f.read(t, subscription.InboxID, 0) })
	check("appending an event", false, func() {
		event := ActionsEvent{RequestID: creation.Source.(ActionsSource).RequestID, Sequence: 1}
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
