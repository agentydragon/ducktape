package api

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/informer"
	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/policies"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/wait"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	testAudience  = "test-actions"
	testNamespace = "test-agentplane"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second
	// callerLabel admits a ServiceAccount's workloads: sandbox_controller's ActionCallerLabel sets it, and Python's
	// CALLER_LABEL selects it.
	callerLabel = "agentplane.allegedly.works/use-action-service"
)

var (
	callerAccount    = auth.Account{Namespace: testNamespace, Name: "test-caller"}
	otherAccount     = auth.Account{Namespace: testNamespace, Name: "test-other-caller"}
	readerAccount    = auth.Account{Namespace: testNamespace, Name: "test-reader"}
	unlabeledAccount = auth.Account{Namespace: testNamespace, Name: "test-unlabeled"}
	// labeledReaderAccount is a service reader whose ServiceAccount also carries the caller label.
	labeledReaderAccount = auth.Account{Namespace: testNamespace, Name: "test-labeled-reader"}
	// outsiderAccount is labeled, outside the namespaces whose workloads may call.
	outsiderAccount = auth.Account{Namespace: "test-elsewhere", Name: "test-caller"}

	caller      = principal(callerAccount)
	otherCaller = principal(otherAccount)

	kube *kubeapiserver.Server
	// policyObjects writes PolicySets and PolicyBindings.
	policyObjects client.Client
	// Each proves its account, from a Pod running as it.
	callerToken, otherToken, readerToken, labeledReaderToken, unlabeledToken, outsiderToken string
	// policyNamespaces numbers the policy namespaces.
	policyNamespaces atomic.Int32
)

func principal(account auth.Account) store.CallerPrincipal {
	return store.CallerPrincipal{Account: action.ServiceAccount{Namespace: account.Namespace, Name: account.Name}}
}

func useAPIServer(server *kubeapiserver.Server) error {
	kube = server
	ctx := context.Background()
	scheme := runtime.NewScheme()
	if err := policyapi.AddToScheme(scheme); err != nil {
		return err
	}
	var err error
	if policyObjects, err = client.New(server.Config, client.Options{Scheme: scheme}); err != nil {
		return err
	}
	for _, account := range []auth.Account{callerAccount, otherAccount, labeledReaderAccount, outsiderAccount} {
		if err := setCallerLabel(ctx, account, true); err != nil {
			return err
		}
	}
	for token, account := range map[*string]auth.Account{
		&callerToken: callerAccount, &otherToken: otherAccount, &readerToken: readerAccount,
		&labeledReaderToken: labeledReaderAccount, &unlabeledToken: unlabeledAccount, &outsiderToken: outsiderAccount,
	} {
		var err error
		if *token, err = server.WorkloadToken(ctx, account, testAudience); err != nil {
			return err
		}
	}
	return nil
}

// setCallerLabel sets or removes the caller label on the account's ServiceAccount, which it creates when absent.
func setCallerLabel(ctx context.Context, account auth.Account, labeled bool) error {
	if _, err := kube.ServiceAccount(ctx, account); err != nil {
		return err
	}
	value := "null"
	if labeled {
		value = `"true"`
	}
	_, err := kube.Clientset.CoreV1().ServiceAccounts(account.Namespace).Patch(ctx, account.Name, types.MergePatchType,
		fmt.Appendf(nil, `{"metadata":{"labels":{%q:%s}}}`, callerLabel, value), metav1.PatchOptions{})
	return err
}

// labeledAccounts reads the caller label from each ServiceAccount on the API server, where the policy informer's
// index will watch it.
type labeledAccounts struct{}

func (labeledAccounts) Admits(account auth.Account) bool {
	found, err := kube.Clientset.CoreV1().ServiceAccounts(account.Namespace).Get(context.Background(), account.Name, metav1.GetOptions{})
	return err == nil && found.Labels[callerLabel] == "true"
}

// served is the API over a database of its own, on a gRPC server with its HTTP facade, as main serves it.
type served struct {
	store  *store.Store
	db     *sql.DB
	gate   *Gate
	issuer *issuer
	// policy is the namespace whose PolicySets and PolicyBindings the policy index reads, this API's alone.
	policy string
	index  *informer.Index
	// routes are the Executors submissions are admitted against, and the ones runWorker runs.
	routes routes
	// databaseURL reaches the database.
	databaseURL string
	client      action_servicepb.ActionServiceClient
	// operators is the OperatorActionService client.
	operators operator_action_servicepb.OperatorActionServiceClient
	// connections is the MCP edge ConnectionService used to exercise this shared Gate's operator admission.
	connections connection_servicepb.ConnectionServiceClient
	// url is the base URL of the HTTP facade.
	url string
	// operatorToken is a token the issuer signed for the operator.
	operatorToken string
	// linkages are the OAuth linkages of the MCP servers the API serves.
	linkages *linkage.Authority
}

// updatesConnection is the application name of the LISTEN connection that wakes the watches.
const updatesConnection = "test-action-updates"

func serve(t *testing.T) *served {
	t.Helper()
	return serveWith(t, kube.TokenReviews(), newCatalog(t, submissionCatalog))
}

// serveWith serves the API over the groups and the linkages of the MCP servers, with workload tokens reviewed by
// reviews, and a policy index of a namespace of its own.
func serveWith(t *testing.T, reviews auth.TokenReviews, groups *catalog.Catalog, servers ...linkage.Server) *served {
	t.Helper()
	namespace := newPolicyNamespace(t)
	index, _ := namespace.watch(t)
	return serveWithPolicy(t, reviews, groups, namespace, index, servers...)
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

// watch runs a policy informer of the namespace and returns its index once the index is available, with what stops
// the informer. The informer runs until stopped or the test ends.
func (n policyNamespace) watch(t *testing.T) (*informer.Index, func()) {
	t.Helper()
	index := informer.NewIndex(reader.StaleAfterCycles*reader.MinWatchPeriod, []string{testNamespace})
	policyInformer, err := informer.New(kube.Config, index, string(n), reader.MinWatchPeriod)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		policyInformer.Run(ctx)
	}()
	var stopped sync.Once
	stop := func() {
		stopped.Do(func() {
			cancel()
			<-done
		})
	}
	t.Cleanup(stop)
	eventually(t, "the policy index to be available", func() bool { return index.Freshness.Available(time.Now()) })
	return index, stop
}

// eventually waits for the condition, and fails the test once callDeadline passes without it.
func eventually(t *testing.T, what string, condition func() bool) {
	t.Helper()
	err := wait.PollUntilContextTimeout(t.Context(), 20*time.Millisecond, callDeadline, true,
		func(context.Context) (bool, error) { return condition(), nil })
	if err != nil {
		t.Fatalf("gave up waiting for %s: %v", what, err)
	}
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

// serveWithPolicy serves the API over the groups and the linkages of the MCP servers with workload tokens reviewed by
// reviews, reading Action policy from index, which holds the namespace's policy objects.
func serveWithPolicy(
	t *testing.T, reviews auth.TokenReviews, groups *catalog.Catalog, namespace policyNamespace, index *informer.Index,
	servers ...linkage.Server,
) *served {
	t.Helper()
	dsn := integrationDatabase.Clone(t)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	// The LISTEN connection is named so a test can drop it.
	listening, err := url.Parse(dsn)
	if err != nil {
		t.Fatal(err)
	}
	query := listening.Query()
	query.Set("application_name", updatesConnection)
	listening.RawQuery = query.Encode()
	updates, err := wakeups.Listen(t.Context(), listening.String(), store.UpdatesChannel, linkage.UpdatesChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { updates.Close() })
	s := &served{
		store: store.New(db), db: db, issuer: newIssuer(t), policy: string(namespace), index: index, databaseURL: dsn,
	}
	s.operatorToken = s.issuer.token(t, nil)
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
	// Every call asks the TokenReview API again, so a revoked token is refused once the API server refuses it.
	workloads, err := auth.NewResolver(reviews, testAudience, []string{testNamespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	operators, err := NewOperatorVerifier(s.issuer.settings(ProfileAuthentik))
	if err != nil {
		t.Fatal(err)
	}
	s.gate = NewGate(GateOptions{
		Workloads: workloads, Callers: labeledAccounts{}, ReaderAccounts: []auth.Account{readerAccount, labeledReaderAccount},
		Operators: operators,
	})
	grpcServer := grpc.NewServer(grpc.ChainUnaryInterceptor(s.gate.UnaryInterceptor), grpc.ChainStreamInterceptor(s.gate.StreamInterceptor))
	action_servicepb.RegisterActionServiceServer(grpcServer, NewService(s.store, updates, groups, s.gate, index, s.submissions(t)))
	operator_action_servicepb.RegisterOperatorActionServiceServer(grpcServer, NewOperatorService(s.store, updates, s.gate, s.linkages))
	// The standalone MCP edge reuses this Gate; a stub keeps its method behind the auth interceptor here.
	connection_servicepb.RegisterConnectionServiceServer(grpcServer, connection_servicepb.UnimplementedConnectionServiceServer{})
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	httpFacade, err := facade.NewHandler(
		t.Context(), listener.Addr().String(), 0,
		action_servicepb.RegisterActionServiceHandlerFromEndpoint,
		operator_action_servicepb.RegisterOperatorActionServiceHandlerFromEndpoint,
	)
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: httpFacade, ReadHeaderTimeout: callDeadline}
	facade.MountGRPC(server, grpcServer)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { server.Close() })
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	s.client = action_servicepb.NewActionServiceClient(connection)
	s.operators = operator_action_servicepb.NewOperatorActionServiceClient(connection)
	s.connections = connection_servicepb.NewConnectionServiceClient(connection)
	s.url = "http://" + listener.Addr().String()
	return s
}

// as is a call context that presents the bearer, bounded by callDeadline.
func as(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
}

// get is the HTTP facade's answer to a GET of path with the bearer, or with none when token is empty.
func (s *served) get(t *testing.T, path, token string) (int, []byte) {
	t.Helper()
	call, err := http.NewRequestWithContext(t.Context(), http.MethodGet, s.url+path, nil)
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		call.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := http.DefaultClient.Do(call)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, body
}

// submit stores a pending request of the owner, as admission would.
func (s *served) submit(t *testing.T, owner store.CallerPrincipal, key string) store.ActionRequest {
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

// decide records the operator's Decision on a pending request.
func (s *served) decide(t *testing.T, request store.ActionRequest, verdict store.Verdict) store.ActionRequest {
	t.Helper()
	decided, _, err := s.store.Decide(t.Context(), request.ID, store.DecisionInput{
		Verdict: verdict, ExpectedVersion: request.Version, IdempotencyKey: "decision-" + request.IdempotencyKey,
	}, s.issuer.operator())
	if err != nil {
		t.Fatal(err)
	}
	return decided
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
	hub, err := wakeups.Listen(context.Background(), s.databaseURL, store.DispatchChannel)
	if err != nil {
		t.Fatal(err)
	}
	dispatches, unsubscribe, err := hub.Subscribe(store.DispatchChannel)
	if err != nil {
		t.Fatal(err)
	}
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
		unsubscribe()
		hub.Close()
	})
}
