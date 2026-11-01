// Package probes is the standard gRPC health service of a service that answers from PostgreSQL and follows its
// NOTIFY channels, for Kubernetes' native probes.
package probes

import (
	"context"
	"database/sql"
	"fmt"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

// LivenessService is the health Check service name of the liveness verdict; the empty name is readiness.
const LivenessService = "livez"

// queryBound bounds the readiness query, so a database that hangs fails the probe rather than holds it.
const queryBound = 5 * time.Second

// Probes admits every caller, because the kubelet sends no bearer.
//
// The empty service is readiness: SERVING while the LISTEN connection is up and a query succeeds, until Drain.
// The `livez` service is liveness: SERVING while the process answers, so a database outage is waited out instead
// of restarting the service. Watch is not served.
type Probes struct {
	healthpb.UnimplementedHealthServer
	db       *sql.DB
	hub      *wakeups.Hub
	draining atomic.Bool
}

func New(db *sql.DB, hub *wakeups.Hub) *Probes { return &Probes{db: db, hub: hub} }

// Drain makes readiness NOT_SERVING from now on, so that the service gets no new callers while it stops.
func (p *Probes) Drain() { p.draining.Store(true) }

func (p *Probes) Check(ctx context.Context, request *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	var serving bool
	switch request.GetService() {
	case "":
		serving = p.ready(ctx)
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

func (p *Probes) ready(ctx context.Context) bool {
	if p.draining.Load() {
		return false
	}
	// The error text can carry the connection string; the type names the failure.
	if err := p.hub.Ping(); err != nil {
		slog.WarnContext(ctx, "PostgreSQL wakeups unavailable", "error_type", fmt.Sprintf("%T", err))
		return false
	}
	ctx, cancel := context.WithTimeout(ctx, queryBound)
	defer cancel()
	if _, err := p.db.ExecContext(ctx, "SELECT 1"); err != nil {
		slog.WarnContext(ctx, "PostgreSQL unavailable", "error_type", fmt.Sprintf("%T", err))
		return false
	}
	return true
}
