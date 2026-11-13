// Package eventually provides a shared polling assertion for Agentplane tests.
package eventually

import (
	"context"
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
