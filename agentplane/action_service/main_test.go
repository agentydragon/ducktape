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

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/probes"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/authtest"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

const (
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second
	callerToken  = "test-token-caller"
)

// caller is the ServiceAccount callerToken proves, which carries the caller label.
var caller = auth.Account{Namespace: "test-agentplane", Name: "test-caller"}

// labeled is the caller label, which only its one account carries.
type labeled struct{ account auth.Account }

func (l labeled) Admits(account auth.Account) bool { return account == l.account }

// running is a served Action Service over a database of its own.
type running struct {
	health  healthpb.HealthClient
	actions action_servicepb.ActionServiceClient
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
	c.TokenAudience = "test-actions"
	c.CallerServiceAccountNamespaces = []string{caller.Namespace}
	cluster := deps{
		Reviews: authtest.Reviews{Accounts: map[string]authtest.Account{
			callerToken: {Namespace: caller.Namespace, Name: caller.Name, UID: "test-caller-uid"},
		}},
		Callers: labeled{account: caller},
	}
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
	go func() { served <- serve(ctx, c, cluster, listener) }()
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
		health: healthpb.NewHealthClient(connection), actions: action_servicepb.NewActionServiceClient(connection), url: "http://" + listener.Addr().String(), store: store.New(db), updates: updates,
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
	pending, err := service.store.Submit(t.Context(), store.CallerPrincipal{Account: store.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name}},
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

func TestTheServiceAnswersTheCallersReadsOverGRPCAndHTTP(t *testing.T) {
	service := start(t)
	submitted, err := service.store.Submit(t.Context(), store.CallerPrincipal{Account: store.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name}},
		store.ActionRequestInput{
			IdempotencyKey: "test-read", Action: store.ActionIdentity{Group: "test-group", Name: "test-action"},
			Arguments: json.RawMessage(`{}`), Title: "test title",
		}, store.Admission{RequestID: uuid.New()})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	request := &action_servicepb.GetActionRequestRequest{RequestId: submitted.ID.String()}
	if _, err := service.actions.GetActionRequest(ctx, request); status.Code(err) != codes.Unauthenticated {
		t.Errorf("a call without a bearer: %v", err)
	}
	got, err := service.actions.GetActionRequest(metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+callerToken), request)
	if err != nil || got.GetActionRequest().GetId() != submitted.ID.String() {
		t.Errorf("gRPC receipt: %v, %v", got, err)
	}

	call, err := http.NewRequestWithContext(ctx, http.MethodGet, service.url+"/v1/action-requests/"+submitted.ID.String(), nil)
	if err != nil {
		t.Fatal(err)
	}
	call.Header.Set("Authorization", "Bearer "+callerToken)
	response, err := http.DefaultClient.Do(call)
	if err != nil {
		t.Fatal(err)
	}
	body, err := io.ReadAll(response.Body)
	response.Body.Close()
	if err != nil {
		t.Fatal(err)
	}
	receipt := &action_servicepb.GetActionRequestResponse{}
	if response.StatusCode != http.StatusOK || protojson.Unmarshal(body, receipt) != nil || !proto.Equal(receipt, got) {
		t.Errorf("HTTP receipt: %d %s", response.StatusCode, body)
	}
}
