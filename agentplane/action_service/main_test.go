package main

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/postgres/go/probes"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
const callDeadline = 30 * time.Second

// running is a served Action Service.
type running struct {
	health healthpb.HealthClient
	// url is the base URL of the service's HTTP routes.
	url string
	// stop ends the service and returns what serve returned.
	stop func() error
}

func start(t *testing.T) running {
	t.Helper()
	c := defaultConfig()
	c.DatabaseURL = integrationDatabase.URL
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	served := make(chan error, 1)
	go func() { served <- serve(ctx, c, listener) }()
	stop := func() error {
		cancel()
		select {
		case err := <-served:
			return err
		case <-time.After(callDeadline):
			return errors.New("the service did not stop")
		}
	}
	t.Cleanup(func() { cancel() })
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	return running{health: healthpb.NewHealthClient(connection), url: "http://" + listener.Addr().String(), stop: stop}
}

func checkHealth(t *testing.T, client healthpb.HealthClient, service string) (healthpb.HealthCheckResponse_ServingStatus, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	response, err := client.Check(ctx, &healthpb.HealthCheckRequest{Service: service})
	return response.GetStatus(), err
}

func TestTheServiceAnswersHealthAndMetricsOnOneListenerAndStopsWhenTold(t *testing.T) {
	service := start(t)
	for _, name := range []string{"", probes.LivenessService} {
		if got, err := checkHealth(t, service.health, name); err != nil || got != healthpb.HealthCheckResponse_SERVING {
			t.Errorf("health of %q = %v, %v; want SERVING", name, got, err)
		}
	}
	if _, err := checkHealth(t, service.health, "test-unknown-service"); status.Code(err) != codes.NotFound {
		t.Errorf("health of an unknown service: %v", err)
	}

	request, err := http.NewRequestWithContext(t.Context(), http.MethodGet, service.url+"/metrics", nil)
	if err != nil {
		t.Fatal(err)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	body, err := io.ReadAll(response.Body)
	response.Body.Close()
	if err != nil {
		t.Fatal(err)
	}
	// Prometheus scrapes the exposition format, in which the Go runtime collector reports its goroutines.
	if response.StatusCode != http.StatusOK || !strings.Contains(string(body), "\ngo_goroutines ") {
		t.Errorf("metrics: %d %q", response.StatusCode, body)
	}

	if err := service.stop(); !errors.Is(err, context.Canceled) {
		t.Errorf("serve returned %v, want the cancellation that stopped it", err)
	}
	if _, err := checkHealth(t, service.health, ""); err == nil {
		t.Error("the service answered after it stopped")
	}
}
