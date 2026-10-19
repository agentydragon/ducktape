package admin

import (
	"context"
	"time"

	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

// LivenessService is the service name of `/livez` in a health Check.
const LivenessService = "livez"

// Probes is the standard gRPC health service, for Kubernetes' native probes and for a caller that
// wants the verdict alone. Like `/healthz` and `/livez` it admits every caller, because the kubelet
// sends no bearer; GetHealth is the authenticated call that carries the facts.
//
// The empty service is `/healthz`: SERVING while the index is complete and still moving. The
// `livez` service is `/livez`: SERVING while the process answers, whatever the index says, so a
// recoverable watch outage never restarts it. Watch is not served.
type Probes struct {
	healthpb.UnimplementedHealthServer
	index Index
	clock func() time.Time
}

func NewProbes(index Index, clock func() time.Time) *Probes {
	return &Probes{index: index, clock: clock}
}

func (p *Probes) Check(_ context.Context, request *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	var serving bool
	switch request.GetService() {
	case "":
		serving = p.index.Status(p.clock()).Available
	case LivenessService:
		serving = true
	default:
		return nil, status.Error(codes.NotFound, "unknown service")
	}
	if serving {
		return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
	}
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_NOT_SERVING}, nil
}
