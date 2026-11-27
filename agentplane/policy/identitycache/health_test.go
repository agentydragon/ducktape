package identitycache

import (
	"context"
	"testing"

	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

func overallStatus(t *testing.T, status *health.Server) healthpb.HealthCheckResponse_ServingStatus {
	t.Helper()
	response, err := status.Check(t.Context(), &healthpb.HealthCheckRequest{})
	if err != nil {
		t.Fatal(err)
	}
	return response.GetStatus()
}

// opensWhenClosed is a WaitCurrent that returns once opened closes, or with its context's error.
func opensWhenClosed(opened <-chan struct{}) func(context.Context) error {
	return func(ctx context.Context) error {
		select {
		case <-opened:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func TestGateHealthReportsNotServingUntilTheViewIsCurrent(t *testing.T) {
	status := health.NewServer()
	opened := make(chan struct{})
	stop := GateHealth(t.Context(), status, opensWhenClosed(opened))
	defer stop()
	if got := overallStatus(t, status); got != healthpb.HealthCheckResponse_NOT_SERVING {
		t.Fatalf("before the view is current: %v, want NOT_SERVING", got)
	}
	close(opened)
	waitFor(t, "SERVING", func() bool { return overallStatus(t, status) == healthpb.HealthCheckResponse_SERVING })
}

func TestAStoppedGateNeverReportsServing(t *testing.T) {
	status := health.NewServer()
	opened := make(chan struct{})
	GateHealth(t.Context(), status, opensWhenClosed(opened))()
	close(opened)
	// Stop returned only after the gate's goroutine ended, so nothing is left to flip the status.
	if got := overallStatus(t, status); got != healthpb.HealthCheckResponse_NOT_SERVING {
		t.Fatalf("after stop: %v, want NOT_SERVING", got)
	}
}
