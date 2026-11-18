package inbox

import (
	"errors"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/wakeup"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	testwakeups "github.com/agentydragon/ducktape/agentplane/testing/wakeups"
)

// observeWakeups returns a check of whether a step wakes workers.
func observeWakeups(t *testing.T, f fixture) func(name string, want bool, step func()) {
	t.Helper()
	observe := testwakeups.Observe(t, f.db, integrationDatabase.URL, wakeup.WorkChannel)
	return func(name string, want bool, step func()) {
		t.Helper()
		if got := observe(step)[wakeup.WorkChannel]; got != want {
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
		if _, err := f.Subscribe(t.Context(), f.incarnation(), reused, sourceadapter.ActionsAuthorization{}); !errors.Is(err, ErrIdempotencyKeyReused) {
			t.Error(err)
		}
	})
	check("reading", false, func() { f.read(t, subscription.InboxID, 0) })
	check("appending an event", false, func() {
		event := sourceadapter.WrapActionsEvent(model.ActionsEvent{RequestID: creation.Source.(sourceadapter.ActionsSource).RequestID, Sequence: 1})
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
