package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/callers"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/linkage_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/policies"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/testing/nettest"
	"github.com/agentydragon/ducktape/agentplane/action_service/testing/oauthprovider"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/postgres/probes"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	workloadidentitytesting "github.com/agentydragon/ducktape/agentplane/workload_identity/testing"
	"github.com/google/uuid"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second
	testAudience = "test-actions"
	// policyNamespace holds the PolicySets and PolicyBindings the service watches.
	policyNamespace = "test-agentplane"
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
	linkages  linkage_servicepb.LinkageServiceClient
	address   string
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
	return startConfigured(t, kubeConfig, groups, func(*config, *deps) {})
}

// startConfigured is start with the configuration and deps configure changes.
func startConfigured(t *testing.T, kubeConfig *rest.Config, groups *catalog.Catalog, configure func(*config, *deps)) running {
	t.Helper()
	c := defaultConfig()
	c.ActionGroups = groups
	c.DatabaseURL = integrationDatabase.Clone(t)
	c.RPC = listener.Config{
		ListenAddress: "127.0.0.1:0",
		Auth:          []listener.Authentication{&listener.KubernetesBearerAuth{Audiences: []string{testAudience}}},
	}
	c.CallerServiceAccountNamespaces = []string{readerAccount.Namespace}
	c.ReaderAccounts = []auth.Account{readerAccount}
	c.PolicyNamespace = policyNamespace
	cluster := deps{Reviews: kube.TokenReviews(), Kube: kubeConfig}
	configure(&c, &cluster)
	db := postgres.Open(t, c.DatabaseURL)
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
	rpcListener, err := net.Listen("tcp", c.RPC.ListenAddress)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(t.Context())
	served := make(chan error, 1)
	go func() { served <- serve(ctx, c, cluster, rpcListener) }()
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
	clientCredentials := insecure.NewCredentials()
	if c.RPC.TLS != nil {
		if !c.RPC.Has(listener.SPIFFE) {
			t.Fatal("test TLS listener must supply SPIFFE roots for its client")
		}
		for _, method := range c.RPC.Auth {
			if spiffe, ok := method.(*listener.SPIFFEAuth); ok {
				clientCredentials, err = (&environment_runtime_transport.ClientTLS{
					CAFile: spiffe.CAFile, ServerURI: environment_runtime_transport.ActionsURI,
				}).Credentials()
				if err != nil {
					t.Fatal(err)
				}
				break
			}
		}
	}
	connection, err := grpc.NewClient(rpcListener.Addr().String(), grpc.WithTransportCredentials(clientCredentials))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	return running{
		health: healthpb.NewHealthClient(connection), actions: action_servicepb.NewActionServiceClient(connection),
		operators: operator_action_servicepb.NewOperatorActionServiceClient(connection),
		linkages:  linkage_servicepb.NewLinkageServiceClient(connection),
		address:   rpcListener.Addr().String(), store: store.New(db), updates: updates, stop: stop,
	}
}

func checkHealth(t *testing.T, client healthpb.HealthClient, service string) (healthpb.HealthCheckResponse_ServingStatus, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	response, err := client.Check(ctx, &healthpb.HealthCheckRequest{Service: service})
	return response.GetStatus(), err
}

// waitReady returns once the service is ready, which it is once the policy index has listed.
func waitReady(t *testing.T, service running) {
	t.Helper()
	if err := wait.PollUntilContextTimeout(t.Context(), 20*time.Millisecond, callDeadline, true, func(context.Context) (bool, error) {
		got, err := checkHealth(t, service.health, "")
		return got == healthpb.HealthCheckResponse_SERVING, err
	}); err != nil {
		t.Fatalf("the service never turned ready: %v", err)
	}
}

func TestTheServiceAnswersHealthAndStopsWhenTold(t *testing.T) {
	service := start(t, kube.Config, &catalog.Catalog{})
	waitReady(t, service)
	if got, err := checkHealth(t, service.health, probes.LivenessService); err != nil || got != healthpb.HealthCheckResponse_SERVING {
		t.Errorf("liveness = %v, %v; want SERVING", got, err)
	}
	if _, err := checkHealth(t, service.health, "test-unknown-service"); status.Code(err) != codes.NotFound {
		t.Errorf("health of an unknown service: %v", err)
	}
	client := &http.Client{Timeout: 5 * time.Second}
	for _, path := range []string{"/llms.txt", "/openapi.json", "/metrics", "/v1/action-groups", "/oauth/client-metadata.json"} {
		response, err := client.Get("http://" + service.address + path)
		if err != nil {
			var timeout net.Error
			if errors.As(err, &timeout) && timeout.Timeout() {
				t.Errorf("HTTP request to %s timed out: %v", path, err)
			}
			continue // A gRPC listener may reject the HTTP/1 preface outright.
		}
		response.Body.Close()
		if response.StatusCode == http.StatusOK {
			t.Errorf("HTTP request to %s unexpectedly succeeded", path)
		}
	}

	if err := service.stop(); !errors.Is(err, context.Canceled) {
		t.Errorf("serve returned %v, want the cancellation that stopped it", err)
	}
	if _, err := checkHealth(t, service.health, ""); err == nil {
		t.Error("the service answered after it stopped")
	}
}

// refusingAPIServer reaches an API server that refuses every connection, so a policy index of it never lists.
func refusingAPIServer(t *testing.T) *rest.Config {
	t.Helper()
	refusing := nettest.RefusingAddress(t)
	return &rest.Config{Host: "http://" + refusing}
}

func TestTheServiceIsNotReadyWithoutThePolicyIndex(t *testing.T) {
	service := start(t, refusingAPIServer(t), &catalog.Catalog{})
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
	pending, err := r.store.Submit(t.Context(), store.CallerPrincipal{Account: caller, ServiceAccountUID: "test-caller-uid"}, store.ActionRequestInput{
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
		if request.State == action_servicepb.ActionState_ACTION_STATE_SUCCEEDED || request.State == action_servicepb.ActionState_ACTION_STATE_FAILED {
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
	if request.State != action_servicepb.ActionState_ACTION_STATE_FAILED ||
		request.Execution.Error.GetKind() != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_INTERNAL {
		t.Errorf("the request is %s with execution error %v", request.State, request.Execution.Error)
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
	id := service.allow(t, caller, store.ActionIdentity{Group: "test-sandbox", Name: "create"},
		`{"name": "test-box", "template": "test-template"}`)
	// The box waits for its credential, which the Policy Controller and cert-manager would issue.
	issuance := workloadidentitytesting.Issuance{Objects: workloadCredentials(t), CA: transporttesting.NewCA(t), Namespace: namespace}
	var request store.ActionRequest
	if err := wait.PollUntilContextTimeout(t.Context(), 50*time.Millisecond, callDeadline, true, func(ctx context.Context) (bool, error) {
		issuance.Settle(t)
		var err error
		request, err = service.store.Get(ctx, id, testOperator)
		return request.State == action_servicepb.ActionState_ACTION_STATE_SUCCEEDED || request.State == action_servicepb.ActionState_ACTION_STATE_FAILED, err
	}); err != nil {
		t.Fatal(err)
	}
	if request.State != action_servicepb.ActionState_ACTION_STATE_SUCCEEDED {
		t.Fatalf("the request is %s with execution error %s", request.State, request.Execution.Error)
	}
	pods, err := kube.Clientset.CoreV1().Pods(namespace).List(t.Context(), metav1.ListOptions{})
	if err != nil || len(pods.Items) != 1 || pods.Items[0].Spec.ServiceAccountName != caller.Name {
		t.Errorf("the namespace holds %v, %v; want one box running as the caller", pods, err)
	}
}

// workloadCredentials reads and writes WorkloadCredentials and their Secrets on the test API server.
func workloadCredentials(t *testing.T) client.Client {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, workloadapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	objects, err := client.New(kube.Config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	return objects
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

// echoUpstream serves an MCP server whose one tool, test_echo, answers "test text"; the server answers 503 while down
// holds. It returns the server's endpoint.
func echoUpstream(t *testing.T, down *atomic.Bool) string {
	t.Helper()
	server := mcp.NewServer(&mcp.Implementation{Name: "test-upstream", Version: "v0.0.1"}, nil)
	server.AddTool(&mcp.Tool{Name: "test_echo", InputSchema: json.RawMessage(`{"type": "object"}`)},
		func(context.Context, *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: "test text"}}}, nil
		})
	handler := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, nil)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if down.Load() {
			http.Error(w, "the test upstream is down", http.StatusServiceUnavailable)
			return
		}
		handler.ServeHTTP(w, r)
	}))
	t.Cleanup(upstream.Close)
	return upstream.URL + "/mcp"
}

// mcpGroup is the settings of a group bound to the MCP server at endpoint.
func mcpGroup(endpoint string) string {
	return `{"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e",
		"config": {"transport": "streamable-http", "url": "` + endpoint + `", "auth": "none"}}}`
}

// An MCP group whose server is down leaves the service serving, and an allowed request of the group waiting for it:
// once the server is back the group offers its tools, and the request runs.
func TestTheServiceRunsAnMCPGroupsActionOnceItsServerIsBack(t *testing.T) {
	var down atomic.Bool
	down.Store(true)
	groups := newCatalog(t, `{"test-mcp": `+mcpGroup(echoUpstream(t, &down))+`}`)
	service := start(t, kube.Config, groups)
	waitReady(t, service)

	id := service.allow(t, store.ServiceAccount{Namespace: "test-namespace", Name: "test-caller"},
		store.ActionIdentity{Group: "test-mcp", Name: "test_echo"}, `{}`)
	var failures int32
	awaitMCPHealth(t, groups, "test-mcp", func(h *action_servicepb.McpHealth) bool {
		failures = h.GetFailures()
		return failures > 0
	})
	// Another attempt fails after the dispatch was committed: the request still waits.
	awaitMCPHealth(t, groups, "test-mcp", func(h *action_servicepb.McpHealth) bool { return h.GetFailures() > failures })
	if request, err := service.store.Get(t.Context(), id, testOperator); err != nil || request.State != action_servicepb.ActionState_ACTION_STATE_ALLOWED {
		t.Fatalf("while the server is down the request is %s, %v", request.State, err)
	}

	down.Store(false)
	request := service.ended(t, id)
	if request.State != action_servicepb.ActionState_ACTION_STATE_SUCCEEDED {
		t.Fatalf("the request is %s with execution error %s", request.State, request.Execution.Error)
	}
}

// fetch is the answer to a GET of address.
// The service finishes a link at its callback, and a group the linkage authorizes follows it: a change another replica
// commits reaches the group at once, not at its next refresh minutes later. The service also serves the shared Client
// ID Metadata Document.
func TestTheServiceFinishesALinkAndAnOAuthGroupFollowsItsLinkage(t *testing.T) {
	var down atomic.Bool
	groups := newCatalog(t, `{"test-linked": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e",
		"config": {"transport": "streamable-http", "url": "`+echoUpstream(t, &down)+`", "server_id": "test-linked", "auth": "oauth"}}}}`)
	provider := oauthprovider.New(t, oauthprovider.Options{ExpiresIn: 3600})
	shared := provider.Server("test-shared")
	shared.ClientID, shared.UseSharedCIMD = nil, true
	servers := map[string]linkage.Server{"test-linked": provider.Server("test-linked"), "test-shared": shared}
	metadata := linkage.ClientMetadata{URL: "https://test-actions.example" + linkage.ClientMetadataPath, ClientName: "Test application"}
	var databaseURL string
	service := startConfigured(t, kube.Config, groups, func(c *config, _ *deps) {
		c.MCPServers, c.MCPClientMetadata, databaseURL = servers, metadata, c.DatabaseURL
	})
	unlinked := func(h *action_servicepb.McpHealth) bool { return h.GetDetail() == "no account is linked" }
	awaitMCPHealth(t, groups, "test-linked", unlinked)

	// The replica the operator started the flow on.
	db := postgres.Open(t, databaseURL)
	replica, err := linkage.New(db, servers, metadata)
	if err != nil {
		t.Fatal(err)
	}
	started, err := replica.Start(t.Context(), "test-linked", nil, testOperator)
	if err != nil {
		t.Fatal(err)
	}
	request := &linkage_servicepb.CompleteCallbackRequest{
		State: oauthprovider.State(t, started.GetAuthorizationUrl()), Code: "test-code",
	}
	result, err := service.linkages.CompleteCallback(t.Context(), request)
	if err != nil || result.GetLinkage().GetStatus() != operator_action_servicepb.McpLinkageStatus_MCP_LINKAGE_STATUS_LINKED {
		t.Fatalf("the gRPC callback answered %v, %v", result, err)
	}
	if _, err := service.linkages.CompleteCallback(t.Context(), request); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("replaying the callback returned %v", err)
	}
	if _, err := service.linkages.CompleteCallback(t.Context(), &linkage_servicepb.CompleteCallbackRequest{}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("an incomplete callback returned %v", err)
	}
	awaitMCPHealth(t, groups, "test-linked", func(h *action_servicepb.McpHealth) bool {
		return h.GetState() == action_servicepb.McpLifecycle_MCP_LIFECYCLE_AVAILABLE
	})
	if _, err := replica.Disconnect(t.Context(), "test-linked"); err != nil {
		t.Fatal(err)
	}
	awaitMCPHealth(t, groups, "test-linked", unlinked)
	started, err = replica.Start(t.Context(), "test-linked", nil, testOperator)
	if err != nil {
		t.Fatal(err)
	}
	_, err = service.linkages.CompleteCallback(t.Context(), &linkage_servicepb.CompleteCallbackRequest{
		State: oauthprovider.State(t, started.GetAuthorizationUrl()), Code: oauthprovider.RefusedCode,
	})
	failureStatus := status.Convert(err)
	details := status.Convert(err).Details()
	if status.Code(err) != codes.Unavailable || len(details) != 1 {
		t.Fatalf("a refused exchange returned %v", err)
	}
	if got := failureStatus.Message(); got != "MCP authorization code exchange failed" ||
		strings.Contains(got, oauthprovider.RefusedError) || strings.Contains(got, oauthprovider.RefusedErrorDescription) {
		t.Fatalf("a refused exchange exposed provider error text in its status message: %q", got)
	}
	info, ok := details[0].(*errdetails.ErrorInfo)
	if !ok || info.GetReason() != "MCP_TOKEN_EXCHANGE_FAILED" || info.GetDomain() != "agentplane.actions" {
		t.Fatalf("a refused exchange returned details %v", details)
	}

	metadataResponse, err := service.linkages.GetClientMetadata(t.Context(), &linkage_servicepb.GetClientMetadataRequest{})
	var document struct {
		ClientID string `json:"client_id"`
	}
	if err != nil || json.Unmarshal(metadataResponse.GetJsonDocument(), &document) != nil || document.ClientID != metadata.URL {
		t.Fatalf("the gRPC metadata document answered %q, %v", metadataResponse.GetJsonDocument(), err)
	}
}

func TestTheServiceAnswersAReadersReadsOverGRPC(t *testing.T) {
	service := start(t, kube.Config, &catalog.Catalog{})
	submitted, err := service.store.Submit(t.Context(), store.CallerPrincipal{Account: store.ServiceAccount{Namespace: "test-agentplane", Name: "test-caller"}, ServiceAccountUID: "test-caller-uid"},
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
}

// No operator is configured here, so the operator services refuse every bearer; refusing one shows a service is
// served and gated, where a service that is not served would answer UNIMPLEMENTED.
func TestTheServiceGatesTheOperatorServicesOverGRPC(t *testing.T) {
	service := start(t, kube.Config, &catalog.Catalog{})
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	asReader := metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+readerToken)
	decision := &operator_action_servicepb.DecideActionRequestRequest{
		RequestId: uuid.NewString(), Verdict: action_servicepb.Verdict_VERDICT_ALLOW, ExpectedVersion: 1, IdempotencyKey: "test-decision",
	}
	if _, err := service.operators.DecideActionRequest(asReader, decision); status.Code(err) != codes.Unauthenticated {
		t.Errorf("a reader's Decision over gRPC: %v", err)
	}
}

// setCallerLabel sets or removes the caller label on the account's ServiceAccount, which it creates when absent.
func setCallerLabel(t *testing.T, account auth.Account, labeled bool) {
	t.Helper()
	if _, err := kube.ServiceAccount(t.Context(), account); err != nil {
		t.Fatal(err)
	}
	value := "null"
	if labeled {
		value = fmt.Sprintf("%q", callers.ServiceAccountLabelValue)
	}
	if _, err := kube.Clientset.CoreV1().ServiceAccounts(account.Namespace).Patch(t.Context(), account.Name, types.MergePatchType,
		fmt.Appendf(nil, `{"metadata":{"labels":{%q:%s}}}`, callers.ServiceAccountLabelKey, value), metav1.PatchOptions{}); err != nil {
		t.Fatal(err)
	}
}

// listOverGRPC lists the requests the bearer's account may read, until the call answers code.
func listOverGRPC(t *testing.T, service running, bearer string, code codes.Code) *action_servicepb.ListActionRequestsResponse {
	t.Helper()
	var listed *action_servicepb.ListActionRequestsResponse
	var err error
	if waited := wait.PollUntilContextTimeout(t.Context(), 20*time.Millisecond, callDeadline, true, func(ctx context.Context) (bool, error) {
		listed, err = service.actions.ListActionRequests(metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+bearer),
			&action_servicepb.ListActionRequestsRequest{})
		return status.Code(err) == code, nil
	}); waited != nil {
		t.Fatalf("ListActionRequests answered %v, never %v", err, code)
	}
	return listed
}

func TestTheServiceAdmitsAWorkloadWhileItsServiceAccountCarriesTheCallerLabel(t *testing.T) {
	service := start(t, kube.Config, &catalog.Catalog{})
	caller := auth.Account{Namespace: readerAccount.Namespace, Name: "test-labeled-caller"}
	setCallerLabel(t, caller, false)
	token, err := kube.WorkloadToken(t.Context(), caller, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	submitted, err := service.store.Submit(t.Context(), store.CallerPrincipal{Account: store.ServiceAccount{Namespace: caller.Namespace, Name: caller.Name}, ServiceAccountUID: "test-caller-uid"},
		store.ActionRequestInput{
			IdempotencyKey: "test-own", Action: store.ActionIdentity{Group: "test-group", Name: "test-action"},
			Arguments: json.RawMessage(`{}`), Title: "test title",
		}, store.Admission{RequestID: uuid.New()})
	if err != nil {
		t.Fatal(err)
	}
	// The index, once listed, vouches that the account carries no label.
	waitReady(t, service)
	refused := func(when string) {
		t.Helper()
		ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
		defer cancel()
		if _, err := service.actions.ListActionRequests(metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token),
			&action_servicepb.ListActionRequestsRequest{}); status.Code(err) != codes.Unauthenticated {
			t.Errorf("%s, over gRPC: %v, want UNAUTHENTICATED", when, err)
		}
	}
	refused("unlabeled")

	setCallerLabel(t, caller, true)
	ownOnly := func(over string, listed *action_servicepb.ListActionRequestsResponse) {
		t.Helper()
		if got := listed.GetActionRequests(); len(got) != 1 || got[0].GetId() != submitted.ID.String() {
			t.Errorf("over %s the caller lists %v, want its own request %s", over, got, submitted.ID)
		}
	}
	ownOnly("gRPC", listOverGRPC(t, service, token, codes.OK))

	setCallerLabel(t, caller, false)
	listOverGRPC(t, service, token, codes.Unauthenticated)
	refused("unlabeled again")
}

// A submission is admitted against the configured catalog and the groups' Executors, behind the Gate's policy index.
func TestTheServiceAdmitsASubmissionAgainstItsCatalogOnlyWhileThePolicyIndexAdmitsTheCaller(t *testing.T) {
	var down atomic.Bool
	down.Store(true)
	// test-mcp's server is never down; test-offline's always is.
	groups := newCatalog(t, `{"test-mcp": `+mcpGroup(echoUpstream(t, new(atomic.Bool)))+`,
		"test-offline": `+mcpGroup(echoUpstream(t, &down))+`}`)
	caller := auth.Account{Namespace: readerAccount.Namespace, Name: "test-submitter"}
	setCallerLabel(t, caller, true)
	token, err := kube.WorkloadToken(t.Context(), caller, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	submit := func(service running, group, key string) (*action_servicepb.SubmitActionRequestResponse, error) {
		t.Helper()
		ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
		defer cancel()
		return service.actions.SubmitActionRequest(metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token),
			&action_servicepb.SubmitActionRequestRequest{
				IdempotencyKey: key, Action: &action_servicepb.ActionIdentity{Group: group, Name: "test_echo"},
				Arguments: &structpb.Struct{}, Title: "test title",
			})
	}

	service := start(t, kube.Config, groups)
	waitReady(t, service)
	awaitMCPHealth(t, groups, "test-mcp", func(h *action_servicepb.McpHealth) bool {
		return h.GetState() == action_servicepb.McpLifecycle_MCP_LIFECYCLE_AVAILABLE
	})
	submitted, err := submit(service, "test-mcp", "test-grpc")
	if err != nil {
		t.Fatalf("test-mcp over gRPC: %v", err)
	}
	id, err := uuid.Parse(submitted.GetActionRequest().GetId())
	if err != nil {
		t.Fatal(err)
	}
	if stored, err := service.store.Get(t.Context(), id, testOperator); err != nil || stored.State != action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING {
		t.Errorf("the submission is stored as %s, %v; want awaiting a decision", stored.State, err)
	}
	for group, want := range map[string]codes.Code{
		"test-offline": codes.Unavailable,
		"test-unknown": codes.InvalidArgument,
	} {
		if _, err := submit(service, group, "test-refused"); status.Code(err) != want {
			t.Errorf("%s over gRPC: %v, want %v", group, err, want)
		}
	}

	// The policy index never lists, so it admits no caller.
	unindexed := start(t, refusingAPIServer(t), &catalog.Catalog{})
	if _, err := submit(unindexed, "test-mcp", "test-unindexed"); status.Code(err) != codes.Unauthenticated {
		t.Errorf("without the policy index, over gRPC: %v, want UNAUTHENTICATED", err)
	}
}

func TestTheServiceAnswersActionPolicyFromItsPolicyIndexOverGRPC(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	caller := auth.Account{Namespace: policyNamespace, Name: "test-policy-caller"}
	setCallerLabel(t, caller, true)
	token, err := kube.WorkloadToken(ctx, caller, testAudience)
	if err != nil {
		t.Fatal(err)
	}
	scheme := runtime.NewScheme()
	if err := policyapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	policyObjects, err := client.New(kube.Config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	for _, object := range []client.Object{
		&policyapi.PolicySet{
			ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: "test-policy-set"},
			Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{Type: policies.RuleType, Policy: &policyapi.ActionPolicy{
				Type: string(store.PolicyExactActions), Actions: map[string]policyapi.ActionNames{"test-group": {"test-action"}},
			}}}},
		},
		&policyapi.PolicyBinding{
			ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: "test-policy-binding"},
			Spec: policyapi.PolicyBindingSpec{
				Subject:    policyapi.Subject{Namespace: caller.Namespace, Name: caller.Name},
				PolicySets: []string{"test-policy-set"},
			},
		},
	} {
		if err := policyObjects.Create(ctx, object); err != nil {
			t.Fatal(err)
		}
	}
	service := start(t, kube.Config, &catalog.Catalog{})
	// Once the policy index has listed, it admits the labeled caller and resolves its binding.
	waitReady(t, service)

	authorized := metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
	var got *action_servicepb.GetActionPolicyResponse
	err = wait.PollUntilContextTimeout(ctx, 20*time.Millisecond, callDeadline, true, func(context.Context) (bool, error) {
		got, err = service.actions.GetActionPolicy(authorized, &action_servicepb.GetActionPolicyRequest{})
		return got.GetActionPolicy().GetSynced() && len(got.GetActionPolicy().GetAutoApproveIf()) == 1, err
	})
	if err != nil {
		t.Fatalf("the caller's policy: %v, %v", got, err)
	}
}

// A sandbox group's Actions are declared, so its view holds still between the reads, where an MCP group's health moves
// with its server.
func TestTheServiceAnswersItsCatalogOverGRPC(t *testing.T) {
	namespace := sandboxNamespace(t, kube, true)
	service := start(t, kube.Config, newCatalog(t, `{"test-sandbox": {"title": "t", "description": "d",
		"executor": {"kind": "pod", "description": "e", "namespace": "`+namespace+`",
			"templates": {"test-template": "the test box"}}}}`))
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	defer cancel()
	ctx = metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+readerToken)
	listed, err := service.actions.ListActionGroups(ctx, &action_servicepb.ListActionGroupsRequest{})
	if err != nil || len(listed.GetActionGroups()) != 1 || len(listed.GetActionGroups()[0].GetActions()) == 0 {
		t.Fatalf("gRPC catalog: %v, %v", listed, err)
	}
	action := listed.GetActionGroups()[0].GetActions()[0]
	got, err := service.actions.GetAction(ctx, &action_servicepb.GetActionRequest{GroupKey: "test-sandbox", ActionKey: action.GetName()})
	if err != nil || !proto.Equal(got.GetAction(), action) {
		t.Errorf("gRPC Action: %v, %v", got, err)
	}
}
