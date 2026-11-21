// Package eventually provides a shared polling assertion for Agentplane tests.
package eventually

import (
	"context"
	"os"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/util/wait"
)

// Eventually polls condition immediately and then every 20 milliseconds for up to one minute.
// On timeout, it reports each optional lastSeen description before failing the test.
func Eventually(t testing.TB, what string, condition func() bool, lastSeen ...func() string) {
	t.Helper()
	if err := wait.PollUntilContextTimeout(context.Background(), 20*time.Millisecond, time.Minute, true,
		func(context.Context) (bool, error) { return condition(), nil }); err != nil {
		for _, seen := range lastSeen {
			t.Error(seen())
		}
		t.Fatalf("gave up waiting for %s", what)
	}
}

// Within polls condition, for state that announces no change, until it holds or bound elapses. The string describes
// the state when it does not hold.
func Within(t testing.TB, bound time.Duration, condition func() (bool, string)) {
	t.Helper()
	deadline := time.After(bound)
	for {
		held, state := condition()
		if held {
			return
		}
		select {
		case <-deadline:
			t.Fatal(state)
		case <-time.After(5 * time.Millisecond):
		}
	}
}

// PathExists polls until stat succeeds and reports the last stat error if the wait fails.
func PathExists(t testing.TB, path string, interval, timeout time.Duration) {
	t.Helper()
	var statErr error
	err := wait.PollUntilContextTimeout(t.Context(), interval, timeout, true, func(context.Context) (bool, error) {
		_, statErr = os.Stat(path)
		return statErr == nil, nil
	})
	if err != nil {
		t.Fatalf("waiting for %s: %v (last stat: %v)", path, err, statErr)
	}
}
