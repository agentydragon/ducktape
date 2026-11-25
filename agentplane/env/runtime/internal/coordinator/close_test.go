package coordinator

import (
	"context"
	"strings"
	"testing"
)

type nonComparableCloseFailure struct{ details []string }

func (e nonComparableCloseFailure) Error() string { return strings.Join(e.details, ", ") }

func TestCloseSessionStopFailureRetainsOwner(t *testing.T) {
	config := lifecycleConfig(t, modelConfigServer(t).URL, t.TempDir(), nil, "/bin/false")
	config.expectCloseError = true
	config.deps.stopSessions = func(context.Context) error {
		return nonComparableCloseFailure{details: []string{"Session stop unproved"}}
	}
	r := newLifecycleRuntime(t, config)
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	if err := r.Close(ctx); err == nil {
		t.Fatal("Close succeeded after Session stop failed")
	} else if _, ok := err.(nonComparableCloseFailure); !ok {
		t.Fatalf("Close error = %T: %v, want original failure type", err, err)
	}
	requireOwnerRetained(t, config.StateDir, "Session stop failure released environment owner")
}
