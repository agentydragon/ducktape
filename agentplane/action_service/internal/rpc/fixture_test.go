package rpc

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/linkage_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/grpcgate"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/informer"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/policies"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/worker"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	operatoroidc "github.com/agentydragon/ducktape/agentplane/identity/operator" // The API package has an operator projection helper.
	"github.com/agentydragon/ducktape/agentplane/identity/operator/testissuer"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/eventually"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	testwakeups "github.com/agentydragon/ducktape/agentplane/testing/wakeups"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	testAudience  = "test-actions"
	testNamespace = "test-agentplane"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline      = 30 * time.Second
	operatorClockSkew = 30 * time.Second
)

var (
	callerAccount = auth.Account{Namespace: testNamespace, Name: "test-caller"}
	otherAccount  = auth.Account{Namespace: testNamespace, Name: "test-other-caller"}

	kube *kubeapiserver.Server
	// policyObjects writes PolicySets and PolicyBindings.
	policyObjects client.Client
	// validKubernetesToken proves callerAccount, from a Pod running as it. The service refuses every Kubernetes bearer.
	validKubernetesToken string
	// policyNamespaces numbers the policy namespaces.
	policyNamespaces atomic.Int32
)

func useAPIServer(server *kubeapiserver.Server) error {
	kube = server
	scheme := runtime.NewScheme()
	if err := policyapi.AddToScheme(scheme); err != nil {
		return err
	}
	var err error
	if policyObjects, err = client.New(server.Config, client.Options{Scheme: scheme}); err != nil {
		return err
	}
	validKubernetesToken, err = server.WorkloadToken(context.Background(), callerAccount, testAudience)
	return err
}

// served is the API over a database of its own, on a gRPC server with its HTTP facade, as main serves it.
type served struct {
	store  *store.Store
	db     *sql.DB
	gate   *grpcgate.Gate
	issuer *testissuer.Issuer
	// policy is the namespace whose PolicySets and PolicyBindings the policy index reads, this API's alone.
	policy string
	index  *informer.Index
	// routes are the Executors submissions are admitted against, and the ones runWorker runs.
	routes routes
	// databaseURL reaches the database.
	databaseURL string
	client      action_servicepb.ActionServiceClient
	// operators is the OperatorActionService client.
	operators  operator_action_servicepb.OperatorActionServiceClient
	linkageRPC linkage_servicepb.LinkageServiceClient
	health     healthpb.HealthClient
	// url is the base URL of the HTTP facade.
	url                 string
	certificateURLs     map[string]string
	connections         *certificateConnections
	caller, otherCaller store.IdentityPrincipal
	// operatorToken is a token the issuer signed for the operator.
	operatorToken string
	// linkages are the OAuth linkages of the MCP servers the API serves.
	linkages *linkage.Authority
}

// updatesConnection is the application name of the LISTEN connection that wakes the watches.
const updatesConnection = "test-action-updates"

func serve(t *testing.T) *served {
	t.Helper()
	return serveWith(t, newCatalog(t, submissionCatalog))
}

// serveWith serves the API with a policy index and live Identity credentials of its own.
func serveWith(t *testing.T, groups *catalog.Catalog, servers ...linkage.Server) *served {
	t.Helper()
	namespace := newPolicyNamespace(t)
	index := namespace.watch(t)
	return serveWithPolicy(t, groups, namespace, index, servers...)
}

// policyNamespace is a namespace of one test's own, for its PolicySets and PolicyBindings.
type policyNamespace string

func newPolicyNamespace(t *testing.T) policyNamespace {
	t.Helper()
	name := fmt.Sprintf("test-policy-%d", policyNamespaces.Add(1))
	_, err := kube.Clientset.CoreV1().Namespaces().Create(t.Context(), &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}, metav1.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	return policyNamespace(name)
}

// watch runs a policy informer of the namespace until the test ends, and returns its index once the index is available.
func (n policyNamespace) watch(t *testing.T) *informer.Index {
	t.Helper()
	index := informer.NewIndex()
	policyInformer, err := informer.New(kube.Config, index, string(n), reader.KubernetesIdentities{})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		policyInformer.Run(ctx)
	}()
	t.Cleanup(func() {
		cancel()
		<-done
	})
	eventually.Eventually(t, "the policy index to be available", index.Health.Available)
	return index
}

// submissionCatalog are the ActionGroups submissions name. test-group's and test-github's Actions run and
// test-offline's would, while no Executor runs test-unbound's; test-broken's input schema is no JSON Schema.
const submissionCatalog = `{
	"test-group": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"}, "actions": {
		"test-action": {"description": "d", "input_schema": {
			"type": "object", "properties": {"target": {"type": "string", "maxLength": 20}}, "additionalProperties": false}},
		"test-other-action": {"description": "d"}}},
	"test-github": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"}, "actions": {
		"get_file_contents": {"description": "d", "input_schema": {
			"type": "object", "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}},
			"required": ["owner", "repo"]}}}},
	"test-offline": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"},
		"available": false, "actions": {"test-action": {"description": "d"}}},
	"test-unbound": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"},
		"actions": {"test-action": {"description": "d"}}},
	"test-broken": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"},
		"actions": {"test-action": {"description": "d", "input_schema": {"type": 5}}}}
}`

// routes runs every Action of the groups it names on executor, which succeeds, and no other.
type routes struct {
	groups   map[string]bool
	executor worker.Executor
}

func (r routes) Route(action store.ActionIdentity) (worker.Executor, bool) {
	if !r.groups[action.Group] {
		return nil, false
	}
	return r.executor, true
}

type succeeding struct{}

func (succeeding) Execute(context.Context, store.ExecutionRequest, *worker.Lease) (store.ExecutionOutcome, error) {
	return store.ExecutionOutcome{State: store.ExecutionSucceeded, Result: json.RawMessage(`{"test": "result"}`)}, nil
}

// GitHub repositories as the stand-in for GitHub's unauthenticated repository endpoint answers them: public, then
// one whose lookup fails; it answers 404 for any other, as GitHub does for a private one.
const (
	publicOwner, publicRepository = "test-public-owner", "test-public-repo"
	failingOwner                  = "test-failing-owner"
)

func fakeGitHub(t *testing.T) string {
	t.Helper()
	github := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/repos/"+publicOwner+"/"+publicRepository:
			_, _ = w.Write([]byte(`{"private": false}`))
		case strings.HasPrefix(r.URL.Path, "/repos/"+failingOwner+"/"):
			w.WriteHeader(http.StatusInternalServerError)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(github.Close)
	return github.URL
}

// submissions runs the Actions of submissionCatalog's groups but test-unbound's.
func (s *served) submissions(t *testing.T) Submissions {
	t.Helper()
	s.routes = routes{
		groups:   map[string]bool{"test-group": true, "test-github": true, "test-offline": true, "test-broken": true},
		executor: succeeding{},
	}
	visibility, err := policies.NewVisibility(fakeGitHub(t), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	return Submissions{Executors: s.routes, Provider: policies.NewProvider(visibility)}
}

// serveWithPolicy serves the API over real TLS, reading policy from the supplied index.
func serveWithPolicy(
	t *testing.T, groups *catalog.Catalog, namespace policyNamespace, index *informer.Index,
	servers ...linkage.Server,
) *served {
	t.Helper()
	return serveWithPolicyOptions(t, groups, namespace, index, nil, IntegrationPreviews{}, servers...)
}

func serveWithGateOptions(t *testing.T, configure func(*grpcgate.Options, policyNamespace)) *served {
	t.Helper()
	namespace := newPolicyNamespace(t)
	return serveWithPolicyOptions(t, newCatalog(t, submissionCatalog), namespace, namespace.watch(t), configure, IntegrationPreviews{})
}

func serveWithPolicyOptions(t *testing.T, groups *catalog.Catalog, namespace policyNamespace, index *informer.Index,
	configure func(*grpcgate.Options, policyNamespace), previews IntegrationPreviews, servers ...linkage.Server,
) *served {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db := postgres.Open(t, dsn)
	// The LISTEN connection is named so a test can drop it.
	listening := postgres.URLWithApplicationName(t, dsn, updatesConnection)
	updates, err := wakeups.Listen(t.Context(), listening.String(), store.UpdatesChannel, linkage.UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { updates.Close() })
	s := &served{
		store: store.New(db), db: db, issuer: testissuer.New(t), policy: string(namespace), index: index, databaseURL: dsn,
	}
	s.operatorToken = s.issuer.Token(t, nil)
	configured := map[string]linkage.Server{}
	for _, server := range servers {
		configured[server.ServerID] = server
	}
	if err := linkage.Validate(configured, linkage.ClientMetadata{}); err != nil {
		t.Fatal(err)
	}
	if s.linkages, err = linkage.New(db, configured, linkage.ClientMetadata{}); err != nil {
		t.Fatal(err)
	}
	operators, err := operatoroidc.NewVerifier(s.issuer.Settings(operatoroidc.ProfileAuthentik))
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, svid := s.configureCertificates(t)
	options := grpcgate.Options{
		ErrorMapper: Failure,
		SVID:        svid, Callers: index, ServiceReaders: []string{peertls.NotificationsURI, peertls.AppURI},
		Operators: operators,
	}
	if configure != nil {
		configure(&options, namespace)
	}
	s.gate = grpcgate.New(options)
	grpcServer := grpc.NewServer(grpc.Creds(serverCredentials), grpc.ChainUnaryInterceptor(s.gate.UnaryInterceptor), grpc.ChainStreamInterceptor(s.gate.StreamInterceptor))
	action_servicepb.RegisterActionServiceServer(grpcServer, NewService(s.store, updates, groups, s.gate, index, s.submissions(t)))
	operator_action_servicepb.RegisterOperatorActionServiceServer(grpcServer, NewOperatorService(s.store, updates, s.gate, s.linkages, previews))
	linkage_servicepb.RegisterLinkageServiceServer(grpcServer, linkage.NewService(s.linkages))
	healthServer := health.NewServer()
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(grpcServer, healthServer)
	serverAddr := grpcserver.Serve(t, grpcServer)
	s.connectCertificates(t, serverAddr)
	s.client = action_servicepb.NewActionServiceClient(s.connections)
	s.operators = operator_action_servicepb.NewOperatorActionServiceClient(s.connections)
	s.linkageRPC = linkage_servicepb.NewLinkageServiceClient(s.connections)
	s.health = healthpb.NewHealthClient(s.connections)
	return s
}

// as selects a named certificate connection locally, or presents an unchanged bearer on
// the certificate-less TLS connection. Selectors are never sent as authorization metadata.
func as(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	if strings.HasPrefix(token, certificatePrefix) {
		return context.WithValue(ctx, certificateConnectionKey{}, token)
	}
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
}

// callFacade is the HTTP facade's answer to a call of method on path with the body and the bearer, or with none when
// token is empty.
func (s *served) callFacade(t *testing.T, method, path, token, body string) (int, []byte) {
	t.Helper()
	call, err := http.NewRequestWithContext(t.Context(), method, s.facadeURL(token)+path, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if token != "" && !strings.HasPrefix(token, certificatePrefix) {
		call.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := http.DefaultClient.Do(call)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	answer, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, answer
}

func (s *served) get(t *testing.T, path, token string) (int, []byte) {
	t.Helper()
	return s.callFacade(t, http.MethodGet, path, token, "")
}

func (s *served) post(t *testing.T, path, token, body string) (int, []byte) {
	t.Helper()
	return s.callFacade(t, http.MethodPost, path, token, body)
}

// submit stores a pending request of the owner, as admission would.
func (s *served) submit(t *testing.T, owner store.Owner, key string) store.ActionRequest {
	t.Helper()
	request, err := s.store.Submit(t.Context(), owner, store.ActionRequestInput{
		IdempotencyKey: key, Action: store.ActionIdentity{Group: "test-group", Name: "test-action"},
		Arguments: json.RawMessage(`{"test": "argument"}`), Title: "test title for " + key,
	}, store.Admission{RequestID: uuid.New()})
	if err != nil {
		t.Fatal(err)
	}
	return request
}

// submitAction stores a pending request of the caller for the Action with the JSON arguments, as admission would.
func (s *served) submitAction(t *testing.T, group, name, arguments string) store.ActionRequest {
	t.Helper()
	id := uuid.New()
	request, err := s.store.Submit(t.Context(), s.caller, store.ActionRequestInput{
		IdempotencyKey: id.String(), Action: store.ActionIdentity{Group: group, Name: name},
		Arguments: json.RawMessage(arguments), Title: name,
	}, store.Admission{RequestID: id})
	if err != nil {
		t.Fatal(err)
	}
	return request
}

// decide records the operator's Decision on a pending request.
func (s *served) decide(t *testing.T, request store.ActionRequest, verdict store.Verdict) store.ActionRequest {
	t.Helper()
	decided, _, err := s.store.Decide(t.Context(), request.ID, store.DecisionInput{
		Verdict: verdict, ExpectedVersion: request.Version, IdempotencyKey: "decision-" + request.IdempotencyKey,
	}, operatorPrincipal(s.issuer))
	if err != nil {
		t.Fatal(err)
	}
	return decided
}

func operatorPrincipal(i *testissuer.Issuer) store.OperatorPrincipal {
	return store.OperatorPrincipal{Issuer: i.URL, Subject: testissuer.Subject}
}

func wantCode(t *testing.T, what string, err error, code codes.Code) {
	t.Helper()
	if status.Code(err) != code {
		t.Errorf("%s: %v, want %v", what, err, code)
	}
}

// runWorker dispatches the allowed requests to the routes' executor until the test ends.
func (s *served) runWorker(t *testing.T) {
	t.Helper()
	dispatches := testwakeups.ListenWakeups(t, s.databaseURL, store.DispatchChannel)[store.DispatchChannel]
	dispatcher, err := worker.New(s.store, s.routes, worker.DefaultTiming())
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	worked := make(chan struct{})
	go func() {
		defer close(worked)
		dispatcher.Run(ctx, dispatches)
	}()
	t.Cleanup(func() {
		cancel()
		<-worked
	})
}
