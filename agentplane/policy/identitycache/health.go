package identitycache

import (
	"context"

	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

// GateHealth makes a service report NOT_SERVING until its Identity view first becomes current, then SERVING. A
// workload authenticated before that would be refused as UNAVAILABLE; that includes the first calls of a pod that
// a rolling deploy has just made Ready. Only the first snapshot gates it: once current, a later PolicyService
// outage is ridden out by the view's grace and then refused per request.
//
// current is the view's WaitCurrent. Call GateHealth before the status is served, and the returned stop before
// reporting NOT_SERVING at shutdown: it ends the wait and returns once the status can no longer change, so a late
// SERVING cannot overwrite the shutdown status.
func GateHealth(ctx context.Context, status *health.Server, current func(context.Context) error) (stop func()) {
	status.SetServingStatus("", healthpb.HealthCheckResponse_NOT_SERVING)
	waiting, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() {
		defer close(done)
		if current(waiting) == nil {
			status.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
		}
	}()
	return func() { cancel(); <-done }
}
