package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

type testHealthChecker struct {
	service string
	status  healthpb.HealthCheckResponse_ServingStatus
	err     error
}

func (c *testHealthChecker) Check(_ context.Context, request *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	c.service = request.GetService()
	if c.err != nil {
		return nil, c.err
	}
	return &healthpb.HealthCheckResponse{Status: c.status}, nil
}

func TestHealthHTTPHandlerMapsReadinessAndLiveness(t *testing.T) {
	for _, test := range []struct {
		name       string
		service    string
		status     healthpb.HealthCheckResponse_ServingStatus
		wantStatus int
	}{
		{name: "ready", service: "", status: healthpb.HealthCheckResponse_SERVING, wantStatus: http.StatusNoContent},
		{name: "not ready", service: "", status: healthpb.HealthCheckResponse_NOT_SERVING, wantStatus: http.StatusServiceUnavailable},
		{name: "live", service: "livez", status: healthpb.HealthCheckResponse_SERVING, wantStatus: http.StatusNoContent},
	} {
		t.Run(test.name, func(t *testing.T) {
			checker := &testHealthChecker{status: test.status}
			recorder := httptest.NewRecorder()
			healthHTTPHandler(checker, test.service).ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/", nil))
			if recorder.Code != test.wantStatus {
				t.Fatalf("status = %d, want %d", recorder.Code, test.wantStatus)
			}
			if checker.service != test.service {
				t.Fatalf("service = %q, want %q", checker.service, test.service)
			}
		})
	}
}

func TestHealthHTTPHandlerTreatsProbeErrorsAsUnavailable(t *testing.T) {
	checker := &testHealthChecker{err: status.Error(codes.Unavailable, "probe failed")}
	recorder := httptest.NewRecorder()
	healthHTTPHandler(checker, "").ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/", nil))
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusServiceUnavailable)
	}
}
