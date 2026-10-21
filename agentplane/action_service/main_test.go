package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/probes"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/google/uuid"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/client-go/rest"
)

const (
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second
	testAudience = "test-actions"
)

var (
	kube *kubeapiserver.Server
	// readerAccount reads every account's requests; readerToken proves it, from a Pod running as it.
	readerAccount = auth.Account{Namespace: "test-agentplane", Name: "test-reader"}
	readerToken   string
)

func useAPIServer(server *kubeapiserver.Server) (err error) {
	kube = server
	readerToken, err = server.WorkloadToken(context.Background(), readerAccount, testAudience)
	return err
}

// running is a served Action Service over a database of its own.
type running struct {
	health    healthpb.HealthClient
	actions   action_servicepb.ActionServiceClient
	operators operator_action_servicepb.OperatorActionServiceClient
	// url is the base URL of the service's HTTP routes.
	url string
	// store is the service's database, and updates is signalled on every Event committed to it.
	store   *store.Store
	updates <-chan struct{}
	// stop ends the service and returns what serve returned.
	stop func() error
}

// start serves groups over a database of its own, reading the policy objects of, and running the sandbox groups on,
// the API server kubeConfig reaches.
func start(t *testing.T, kubeConfig *rest.Config, groups *catalog.Catalog) running {
	t.Helper()
	c := defaultConfig()
	c.ActionGroups = groups
	c.DatabaseURL = integrationDatabase.Clone(t)
	c.TokenAudience = testAudience
	c.CallerServiceAccountNamespaces = []string{readerAccount.Namespace}
	c.ReaderAccounts = []auth.Account{readerAccount}
	c.PolicyNamespace = "test-agentplane"
	c.PolicyResyncSeconds = int(reader.MinWatchPeriod / time.Second)
	// As deployed until the Gate reads the caller label's index: no workload is admitted as a caller.
	cluster := deps{Reviews: kube.TokenReviews(), Callers: noCallers{}, Kube: kubeConfig}
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
		health: healthpb.NewHealthClient(connection), actions: action_servicepb.NewActionServiceClient(connection),
		operators: operator_action_servicepb.NewOperatorActionServiceClient(connection),
		url:       "http://" + listener.Addr().String(), store: store.New(db), updates: updates, stop: stop,
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
	service := start(t, kube.Config, &catalog.Catalog{})
	// Ready once the policy index has listed.
	err := wait.PollUntilContextTimeout(t.Context(), 20*time.Millisecond, callDeadline, true, func(context.Context) (bool, error) {
		got, err := checkHealth(t, service.health, "")
		return got == healthpb.HealthCheckResponse_SERVING, err
	})
	if err != nil {
		t.Fatalf("the service never turned ready: %v", err)
	}
	if got, err := checkHealth(t, service.health, probes.LivenessService); err != nil || got != healthpb.HealthCheckResponse_SERVING {
		t.Errorf("liveness = %v, %v; want SERVING", got, err)
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

func TestTheServiceIsNotReadyWithoutThePolicyIndex(t *testing.T) {
	// An API server that refuses every connection: the policy index never lists.
	refusing, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	refusing.Close()
	service := start(t, &rest.Config{Host: "http://" + refusing.Addr().String()}, &catalog.Catalog{})
	if got, err := checkHealth(t, service.health, ""); err != nil || got != healthpb.HealthCheckResponse_NOT_SERVING {
		t.Errorf("readiness = %v, %v; want NOT_SERVING", got, err)
	}
	// Liveness never follows the API server, so that waiting out its outage restarts nothing.
	if got, err := checkHealth(t, service.health, probes.LivenessService); err != nil || got != healthpb.HealthCheckResponse_SERVING {
		t.Errorf("liveness = %v, %v; want SERVING", got, err)
	}
}

var testOperator = store.OperatorPrincipal{Issuer: "https://issuer.test.example", Subject: "test-operator"}

// allow submits action as caller and allows it.
func (r running) allow(t *testing.T, caller store.ServiceAccount, action store.ActionIdentity, arguments string) uuid.UUID {
	t.Helper()
	pending, err := r.store.Submit(t.Context(), store.CallerPrincipal{Account: caller}, store.ActionRequestInput{
		IdempotencyKey: "test-dispatched", Action: action, Arguments: json.RawMessage(arguments), Title: "test title",
	}, store.Admission{RequestID: uuid.New()})
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := r.store.Decide(t.Context(), pending.ID, store.DecisionInput{
		Verdict: store.VerdictAllow, ExpectedVersion: pending.Version, IdempotencyKey: "test-allow",
	}, testOperator); err != nil {
		t.Fatal(err)
	}
	return pending.ID
}

// run submits action as caller, allows it, and returns the request once its Execution has ended.
func (r running) run(t *testing.T, caller store.ServiceAccount, action store.ActionIdentity, arguments string) store.ActionRequest {
	t.Helper()
	return r.ended(t, r.allow(t, caller, action, arguments))
}

// ended returns the request once its Execution has ended.
func (r running) ended(t *testing.T, id uuid.UUID) store.ActionRequest {
	t.Helper()
	deadline := time.After(callDeadline)
	for {
		request, err := r.store.Get(t.Context(), id, testOperator)
		if err != nil {
			t.Fatal(err)
		}
		if request.State == store.StateSucceeded || request.State == store.StateFailed {
			return request
		}
		select {
		case <-r.updates:
		case <-deadline:
			t.Fatalf("the request is %s, never ended", request.State)
		}
	}
}

func TestTheServiceFailsAnActionNoExecutorRuns(t *testing.T) {
	service := start(t, kube.Config, &catalog.Catalog{})
	request := service.run(t, store.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"},
		store.ActionIdentity{Group: "test-group", Name: "test-action"}, `{}`)
	var failure struct{ Kind string }
	if err := json.Unmarshal(request.Execution.Error, &failure); err != nil || request.State != store.StateFailed ||
		failure.Kind != "*worker.UnsupportedActionError" {
		t.Errorf("the request is %s with execution error %s, %v", request.State, request.Execution.Error, err)
	}
	if err := service.stop(); !errors.Is(err, context.Canceled) {
		t.Errorf("serve returned %v, want the cancellation that stopped it", err)
	}
}

// A sandbox group's Action runs through the worker, holding its lease, to a box on the API server.
func TestTheServiceRunsASandboxGroupsActions(t *testing.T) {
	namespace := sandboxNamespace(t, kube, true)
	service := start(t, kube.Config, sandboxGroups(t, namespace, "null"))
	caller := store.ServiceAccount{Namespace: namespace, Name: "test-caller"}
	request := service.run(t, caller, store.ActionIdentity{Group: "test-sandbox", Name: "create"},
		`{"name": "test-box", "template": "test-template"}`)
	if request.State != store.StateSucceeded {
		t.Fatalf("the request is %s with execution error %s", request.State, request.Execution.Error)
	}
	pods, err := kube.Clientset.CoreV1().Pods(namespace).List(t.Context(), metav1.ListOptions{})
	if err != nil || len(pods.Items) != 1 || pods.Items[0].Spec.ServiceAccountName != caller.Name {
		t.Errorf("the namespace holds %v, %v; want one box running as the caller", pods, err)
	}
}

// awaitMCPHealth waits until the health of the MCP group key satisfies condition.
func awaitMCPHealth(t *testing.T, groups *catalog.Catalog, key string, condition func(*action_servicepb.McpHealth) bool) {
	t.Helper()
	deadline := time.After(callDeadline)
	for {
		changed := groups.Changed()
		for k, g := range groups.Groups() {
			if k == key && condition(g.Health) {
				return
			}
		}
		select {
		case <-changed:
		case <-deadline:
			t.Fatal("the MCP group never reached the health awaited")
		}
	}
}

// An MCP group whose server is down leaves the service serving, and an allowed request of the group waiting for it:
// once the server is back the group offers its tools, and the request runs.
func TestTheServiceRunsAnMCPGroupsActionOnceItsServerIsBack(t *testing.T) {
	server := mcp.NewServer(&mcp.Implementation{Name: "test-upstream", Version: "v0.0.1"}, nil)
	server.AddTool(&mcp.Tool{Name: "test_echo", InputSchema: json.RawMessage(`{"type": "object"}`)},
		func(context.Context, *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "test text"}}}, nil
		})
	handler := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, nil)
	var down atomic.Bool
	down.Store(true)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if down.Load() {
			http.Error(w, "the test upstream is down", http.StatusServiceUnavailable)
			return
		}
		handler.ServeHTTP(w, r)
	}))
	t.Cleanup(upstream.Close)
	groups := newCatalog(t, `{"test-mcp": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e",
		"config": {"transport": "streamable-http", "url": "`+upstream.URL+`/mcp", "auth": "none"}}}}`)
	service := start(t, kube.Config, groups)
	err := wait.PollUntilContextTimeout(t.Context(), 20*time.Millisecond, callDeadline, true, func(context.Context) (bool, error) {
		got, err := checkHealth(t, service.health, "")
		return got == healthpb.HealthCheckResponse_SERVING, err
	})
	if err != nil {
		t.Fatalf("the service never turned ready: %v", err)
	}

	id := service.allow(t, store.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"},
		store.ActionIdentity{Group: "test-mcp", Name: "test_echo"}, `{}`)
	var failures int32
	awaitMCPHealth(t, groups, "test-mcp", func(h *action_servicepb.McpHealth) bool {
		failures = h.GetFailures()
		return failures > 0
	})
	// Another attempt fails after the dispatch was committed: the request still waits.
	awaitMCPHealth(t, groups, "test-mcp", func(h *action_servicepb.McpHealth) bool { return h.GetFailures() > failures })
	if request, err := service.store.Get(t.Context(), id, testOperator); err != nil || request.State != store.StateAllowed {
		t.Fatalf("while the server is down the request is %s, %v", request.State, err)
	}

	down.Store(false)
	request := service.ended(t, id)
	if request.State != store.StateSucceeded {
		t.Fatalf("the request is %s with execution error %s", request.State, request.Execution.Error)
	}
}

func TestTheServiceAnswersAReadersReadsOverGRPCAndHTTP(t *testing.T) {
	service := start(t, kube.Config, &catalog.Catalog{})
	submitted, err := service.store.Submit(t.Context(), store.CallerPrincipal{Account: store.ServiceAccount{Namespace: "test-agentplane", Name: "test-caller"}},
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
	got, err := service.actions.GetActionRequest(metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+readerToken), request)
	if err != nil || got.GetActionRequest().GetId() != submitted.ID.String() {
		t.Errorf("gRPC receipt: %v, %v", got, err)
	}

	call, err := http.NewRequestWithContext(ctx, http.MethodGet, service.url+"/v1/action-requests/"+submitted.ID.String(), nil)
	if err != nil {
		t.Fatal(err)
	}
	call.Header.Set("Authorization", "Bearer "+readerToken)
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

// No operator is configured here, so the operator service refuses every bearer; refusing one shows the service is
// served and gated, where a service that is not served would answer UNIMPLEMENTED, and a route that is not, 404.
func TestTheServiceGatesTheOperatorServiceOverGRPCAndHTTP(t *testing.T) {
	service := start(t, kube.Config, &catalog.Catalog{})
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	decision := &operator_action_servicepb.DecideActionRequestRequest{
		RequestId: uuid.NewString(), Verdict: action_servicepb.Verdict_VERDICT_ALLOW, ExpectedVersion: 1, IdempotencyKey: "test-decision",
	}
	if _, err := service.operators.DecideActionRequest(metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+readerToken), decision); status.Code(err) != codes.Unauthenticated {
		t.Errorf("a reader's Decision over gRPC: %v", err)
	}
	call, err := http.NewRequestWithContext(ctx, http.MethodPost, service.url+"/v1/operator/action-requests/"+decision.GetRequestId()+"/decision",
		strings.NewReader(`{"verdict": "VERDICT_ALLOW", "expected_version": 1, "idempotency_key": "test-decision"}`))
	if err != nil {
		t.Fatal(err)
	}
	call.Header.Set("Authorization", "Bearer "+readerToken)
	response, err := http.DefaultClient.Do(call)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusUnauthorized {
		t.Errorf("a reader's Decision over HTTP: %d", response.StatusCode)
	}
}
