// Package wakeups provides test setup for PostgreSQL notification wakeups.
package wakeups

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
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
