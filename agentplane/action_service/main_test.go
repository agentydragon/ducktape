package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/probes"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
const callDeadline = 30 * time.Second

// running is a served Action Service over a database of its own.
type running struct {
	health healthpb.HealthClient
	// url is the base URL of the service's HTTP routes.
	url string
	// store is the service's database, and updates is signalled on every Event committed to it.
	store   *store.Store
	updates <-chan struct{}
	// stop ends the service and returns what serve returned.
	stop func() error
}

func start(t *testing.T) running {
	t.Helper()
	c := defaultConfig()
	c.DatabaseURL = integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", c.DatabaseURL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	hub, err := wakeups.Listen(t.Context(), c.DatabaseURL, store.UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	updates, unsubscribe, err := hub.Subscribe(store.UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(unsubscribe)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	served := make(chan error, 1)
	go func() { served <- serve(ctx, c, listener) }()
	var result error
	var stopped sync.Once
	stop := func() error {
		stopped.Do(func() {
			cancel()
			select {
			case result = <-served:
			case <-time.After(callDeadline):
				result = errors.New("the service did not stop")
			}
		})
		return result
	}
	t.Cleanup(func() { _ = stop() })
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	return running{
		health: healthpb.NewHealthClient(connection), url: "http://" + listener.Addr().String(), store: store.New(db), updates: updates,
		stop: stop,
	}
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

func TestTheServiceRunsTheDispatchWorkerWhichHasNoExecutorYet(t *testing.T) {
	service := start(t)
	operator := store.OperatorPrincipal{Issuer: "https://issuer.test.example", Subject: "test-operator"}
	pending, err := service.store.Submit(t.Context(), store.CallerPrincipal{Account: store.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"}},
		store.ActionRequestInput{
			IdempotencyKey: "test-dispatched", Action: store.ActionIdentity{Group: "test-group", Name: "test-action"},
			Arguments: json.RawMessage(`{}`), Title: "test title",
		}, store.Admission{RequestID: uuid.New()})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := service.store.Decide(t.Context(), pending.ID, store.DecisionInput{
		Verdict: store.VerdictAllow, ExpectedVersion: pending.Version, IdempotencyKey: "test-allow",
	}, operator); err != nil {
		t.Fatal(err)
	}
	// The worker claims the allowed Execution and, with no Executor to run it, fails it.
	deadline := time.After(callDeadline)
	for {
		request, err := service.store.Get(t.Context(), pending.ID, operator)
		if err != nil {
			t.Fatal(err)
		}
		if request.State == store.StateFailed {
			var failure struct{ Kind string }
			if err := json.Unmarshal(request.Execution.Error, &failure); err != nil || failure.Kind != "*worker.UnsupportedActionError" {
				t.Errorf("execution error %s, %v", request.Execution.Error, err)
			}
			break
		}
		select {
		case <-service.updates:
		case <-deadline:
			t.Fatalf("the request is %s, never failed", request.State)
		}
	}
	if err := service.stop(); !errors.Is(err, context.Canceled) {
		t.Errorf("serve returned %v, want the cancellation that stopped it", err)
	}
}
