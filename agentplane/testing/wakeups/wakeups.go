// Package wakeups provides test setup for PostgreSQL notification wakeups.
package wakeups

import (
	"context"
	"database/sql"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
)

const (
	// sentinel is notified after each observed step, to learn when the Hub has applied the step's notifications.
	sentinel = "wakeups_observe_sentinel"
	// awaitBound only fails a test whose wakeup never comes.
	awaitBound = 30 * time.Second
)

// ListenWakeups returns a signal for each channel and cleans up subscriptions before closing their shared Hub.
func ListenWakeups(t testing.TB, dsn string, channels ...string) map[string]<-chan struct{} {
	t.Helper()
	hub, err := wakeups.Listen(t.Context(), dsn, channels...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = hub.Close() })

	signals := make(map[string]<-chan struct{}, len(channels))
	for _, channel := range channels {
		signal, unsubscribe, err := hub.Subscribe(channel)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(unsubscribe)
		signals[channel] = signal
	}
	return signals
}

// Observe returns a function that runs a step, then reports whether the step woke each of channels and consumes
// those wakeups. The step must have committed its writes when it returns. It listens on dsn and notifies via db.
func Observe(t testing.TB, db *sql.DB, dsn string, channels ...string) func(step func()) map[string]bool {
	t.Helper()
	signals := ListenWakeups(t, dsn, append([]string{sentinel}, channels...)...)
	return func(step func()) map[string]bool {
		t.Helper()
		step()
		// The hub applies notifications in the order PostgreSQL delivers them, so once the sentinel's has arrived,
		// every notification the step committed before it has too.
		ctx, cancel := context.WithTimeout(t.Context(), awaitBound)
		defer cancel()
		if _, err := db.ExecContext(ctx, `SELECT pg_notify($1, '')`, sentinel); err != nil {
			t.Fatal(err)
		}
		select {
		case <-signals[sentinel]:
		case <-ctx.Done():
			t.Fatal("no wakeup from the sentinel")
		}
		woken := make(map[string]bool, len(channels))
		for _, channel := range channels {
			select {
			case <-signals[channel]:
				woken[channel] = true
			default:
				woken[channel] = false
			}
		}
		return woken
	}
}
