package main

import (
	"context"
	"net/http"

	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

type healthChecker interface {
	Check(context.Context, *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error)
}

func healthHTTPHandler(checker healthChecker, service string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		response, err := checker.Check(r.Context(), &healthpb.HealthCheckRequest{Service: service})
		if err != nil || response.GetStatus() != healthpb.HealthCheckResponse_SERVING {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
}
