package api

import (
	"context"
	"database/sql"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/inboxaccess"
	operatorpb "github.com/agentydragon/ducktape/agentplane/notification_service/notifications_operatorpb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const (
	testAudience  = "test-notification-service"
	testNamespace = "test-namespace"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second

	sandboxName      = "test-sandbox"
	sandboxUID       = "test-sandbox-uid"
	successorUID     = "test-successor-sandbox-uid" // the test Sandbox's incarnation after recreateSandbox
	otherSandboxName = "other-sandbox"
	otherSandboxUID  = "other-sandbox-uid"
	operatorName     = "test-operator"
	testSession      = "test-session"
)

var (
	kube *kubeapiserver.Server
	// A Sandbox runs as the ServiceAccount of its own name: sandboxToken and otherSandboxToken prove
	// those of the two Sandboxes, operatorToken the operator's, and outsiderToken one named like the
	// first Sandbox's in another namespace.
	sandboxToken, otherSandboxToken, operatorToken, outsiderToken string
)

func useAPIServer(server *kubeapiserver.Server) (err error) {
	kube = server
	for token, account := range map[*string]auth.Account{
		&sandboxToken:      {Namespace: testNamespace, Name: sandboxName},
		&otherSandboxToken: {Namespace: testNamespace, Name: otherSandboxName},
		&operatorToken:     {Namespace: testNamespace, Name: operatorName},
		&outsiderToken:     {Namespace: "test-elsewhere", Name: sandboxName},
	} {
		if *token, err = server.WorkloadToken(context.Background(), account, testAudience); err != nil {
			return err
		}
	}
	return nil
}

// fakeRunner holds the Sessions each Sandbox's runner reports.
type fakeRunner struct {
	mu       sync.Mutex
	sessions map[string][]string
	err      error
	hangs    bool
	asked    []sandboxreader.Identity
}

func (r *fakeRunner) ListSessions(ctx context.Context, sandbox sandboxreader.Identity) ([]string, error) {
	r.mu.Lock()
	r.asked = append(r.asked, sandbox)
	sessions, err, hangs := r.sessions[sandbox.Name], r.err, r.hangs
	r.mu.Unlock()
	if hangs {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	return sessions, err
}

func (r *fakeRunner) failWith(err error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.err = err
}

func (r *fakeRunner) hang() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.hangs = true
}

func (r *fakeRunner) questions() []sandboxreader.Identity {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]sandboxreader.Identity(nil), r.asked...)
}

type actionsCall struct {
	owner         store.ServiceAccount
	requestID     uuid.UUID
	afterSequence int64
}

// fakeActions answers whether the account may follow an Action request.
type fakeActions struct {
	mu    sync.Mutex
	err   error
	calls []actionsCall
}

func (a *fakeActions) Events(_ context.Context, owner store.ServiceAccount, requestID uuid.UUID, afterSequence int64) ([]store.ActionEvent, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.calls = append(a.calls, actionsCall{owner: owner, requestID: requestID, afterSequence: afterSequence})
	return nil, a.err
}

func (a *fakeActions) failWith(err error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.err = err
}

func (a *fakeActions) asked() []actionsCall {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]actionsCall(nil), a.calls...)
}

// fakeGitHub authorizes every source under one access unless it fails.
type fakeGitHub struct {
	mu      sync.Mutex
	access  store.GitHubAccessKey
	err     error
	sources []store.GitHubSource
}

func (g *fakeGitHub) Authorize(_ context.Context, source store.GitHubSource) (store.GitHubAccessKey, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.sources = append(g.sources, source)
	return g.access, g.err
}

func (g *fakeGitHub) failWith(err error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.err = err
}

func (g *fakeGitHub) authorized() []store.GitHubSource {
	g.mu.Lock()
	defer g.mu.Unlock()
	return append([]store.GitHubSource(nil), g.sources...)
}

// fakeHistory holds the Sandbox that wrote each Session.
type fakeHistory struct {
	mu        sync.Mutex
	sandboxes map[string]*historypb.SandboxRef
	err       error
	asked     []string
}

func (h *fakeHistory) GetSessionSummary(_ context.Context, request *historypb.GetSessionSummaryRequest, _ ...grpc.CallOption) (*historypb.SessionSummary, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.asked = append(h.asked, request.GetSessionId())
	if h.err != nil {
		return nil, h.err
	}
	sandbox, ok := h.sandboxes[request.GetSessionId()]
	if !ok {
		return nil, status.Error(codes.NotFound, "session not found")
	}
	return &historypb.SessionSummary{SessionId: request.GetSessionId(), Sandbox: sandbox}, nil
}

func (h *fakeHistory) failWith(err error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.err = err
}

func (h *fakeHistory) questions() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]string(nil), h.asked...)
}

type serverConfig struct {
	lease, requestTimeout time.Duration
	debounce              store.Debounce
	githubDisabled        bool
	// operatorReader is the account that may call NotificationsOperator, none when empty.
	operatorReader string
	// messageBodyLimit is in UTF-8 bytes.
	messageBodyLimit int
}

type testServer struct {
	notifications   *Notifications
	operatorService *NotificationsOperator
	client          notificationspb.NotificationsClient
	operator        operatorpb.NotificationsOperatorClient
	store           *store.Store
	hub             *wakeups.Hub
	db              *sql.DB
	runner          *fakeRunner
	actions         *fakeActions
	github          *fakeGitHub
	// policy is current and grants nothing until a test grants.
	policy  *inboxaccess.Policy
	history *fakeHistory
	// kube holds the Sandboxes the service reads.
	kube   client.Client
	config serverConfig
}

// newServer serves both APIs over the shared database, emptied of earlier tests' inboxes, with a hub listening to
// the channel the watches follow. Sandboxes are read through the real reader over a fake Kubernetes client.
func newServer(t *testing.T, configure ...func(*serverConfig)) testServer {
	t.Helper()
	config := serverConfig{
		lease: time.Hour, requestTimeout: callDeadline,
		debounce:       store.Debounce{Quiet: time.Minute, MaxWait: 2 * time.Minute},
		operatorReader: operatorName, messageBodyLimit: 64,
	}
	for _, configure := range configure {
		configure(&config)
	}
	db, err := sql.Open("postgres", integrationDatabase.URL)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.ExecContext(t.Context(), `TRUNCATE inbox CASCADE`); err != nil {
		t.Fatal(err)
	}
	hub, err := wakeups.Listen(t.Context(), integrationDatabase.URL, store.WorkChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	resolver, err := auth.NewResolver(kube.TokenReviews(), testAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kube := clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(
		&sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: sandboxName, UID: sandboxUID}},
		&sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: otherSandboxName, UID: otherSandboxUID}},
	).Build()
	sandboxes, err := sandboxreader.New(testNamespace, kube)
	if err != nil {
		t.Fatal(err)
	}
	server := testServer{
		store: store.New(db, store.Quotas{
			InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000,
			UnacknowledgedMessagesPerSender: 128,
		}),
		hub: hub, db: db, config: config,
		runner: &fakeRunner{sessions: map[string][]string{
			sandboxName: {testSession, "another-session"}, otherSandboxName: {testSession},
		}},
		actions: &fakeActions{},
		github:  &fakeGitHub{access: store.GitHubAccessKey{AppID: 11, InstallationID: 22, RepositoryID: 33}},
		policy:  inboxaccess.NewPolicy(time.Hour),
		history: &fakeHistory{sandboxes: map[string]*historypb.SandboxRef{
			testSession: {Namespace: testNamespace, Name: sandboxName},
		}},
		kube: kube,
	}
	server.policy.Freshness.MarkSynced()
	server.policy.Freshness.RecordCycle(reader.PolicySets, time.Now())
	server.policy.Freshness.RecordCycle(reader.PolicyBindings, time.Now())
	options := Options{
		Store: server.store, Wakeups: hub, Resolver: resolver, Namespace: testNamespace, Sandboxes: sandboxes,
		Runner: server.runner, Actions: server.actions, OperatorReader: config.operatorReader,
		SendPolicy: server.policy, History: server.history, MessageBodyLimit: config.messageBodyLimit,
		Debounce: config.debounce, RequestTimeout: config.requestTimeout, WatchLease: config.lease,
	}
	if !config.githubDisabled {
		options.GitHub = server.github
	}
	server.notifications = NewNotifications(options)
	server.operatorService = NewNotificationsOperator(options)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	grpcServer := grpc.NewServer()
	notificationspb.RegisterNotificationsServer(grpcServer, server.notifications)
	operatorpb.RegisterNotificationsOperatorServer(grpcServer, server.operatorService)
	go func() { _ = grpcServer.Serve(listener) }()
	t.Cleanup(grpcServer.Stop)
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	server.client = notificationspb.NewNotificationsClient(connection)
	server.operator = operatorpb.NewNotificationsOperatorClient(connection)
	return server
}

// bearing is a call context carrying the token, or none for "".
func bearing(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	if token == "" {
		return ctx
	}
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
}

func requireCode(t *testing.T, err error, want codes.Code) {
	t.Helper()
	if got := status.Code(err); got != want {
		t.Fatalf("code = %v (%v), want %v", got, err, want)
	}
}

func unary[Request, Response any](call func(context.Context, *Request, ...grpc.CallOption) (*Response, error)) func(context.Context) error {
	return func(ctx context.Context) error {
		_, err := call(ctx, new(Request))
		return err
	}
}

func sandboxRef() *notificationspb.DestinationRef {
	return &notificationspb.DestinationRef{Namespace: testNamespace, Name: sandboxName, Uid: sandboxUID}
}

func actionsSource(requestID uuid.UUID, afterSequence int32) *notificationspb.Source {
	return &notificationspb.Source{Provider: &notificationspb.Source_Actions{
		Actions: &notificationspb.ActionsSource{RequestId: requestID.String(), AfterSequence: afterSequence},
	}}
}

// createRequest follows a new Action request for the test Session of the test Sandbox.
func createRequest(key string) *notificationspb.CreateSubscriptionRequest {
	return &notificationspb.CreateSubscriptionRequest{
		DestinationRef: sandboxRef(), SessionId: testSession, IdempotencyKey: key, Source: actionsSource(uuid.New(), 0),
	}
}

// subscribe makes the Sandbox's subscription to a new Action request, for a Session of its own when it names one.
func (s testServer) subscribe(t *testing.T, key string, sessionID ...string) *notificationspb.Subscription {
	t.Helper()
	request := createRequest(key)
	if len(sessionID) > 0 {
		request.SessionId = sessionID[0]
		s.runner.mu.Lock()
		s.runner.sessions[sandboxName] = append(s.runner.sessions[sandboxName], sessionID[0])
		s.runner.mu.Unlock()
	}
	response, err := s.client.CreateSubscription(bearing(t, sandboxToken), request)
	if err != nil {
		t.Fatal(err)
	}
	return response.GetSubscription()
}

// recreateSandbox deletes the test Sandbox and creates another under its name, whose UID is successorUID.
func (s testServer) recreateSandbox(t *testing.T) {
	t.Helper()
	if err := s.kube.Delete(t.Context(), &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: sandboxName}}); err != nil {
		t.Fatal(err)
	}
	if err := s.kube.Create(t.Context(), &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: sandboxName, UID: successorUID}}); err != nil {
		t.Fatal(err)
	}
}

// grantSend binds a set of one rule, allowing send_message to the targets, to the account of the given name.
// The binding is named after the account.
func (s testServer) grantSend(account string, targets policyapi.SandboxTargets) {
	s.policy.Rules.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: account + "-send"},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{
			{Type: "session_inbox_access", Operations: []string{"send_message"}, Targets: &targets},
		}},
	})
	s.policy.Rules.PutBinding(&policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: account},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    policyapi.ServiceAccountRef{Namespace: testNamespace, Name: account},
			PolicySets: []string{account + "-send"},
		},
	})
}

func (s testServer) exec(t *testing.T, query string, args ...any) {
	t.Helper()
	if _, err := s.db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
}

// record has the source worker commit the events of the subscription's Action request through the given
// sequence, as a worker holding the inbox's claim does, and returns the inbox to the queue.
func (s testServer) record(t *testing.T, subscription *notificationspb.Subscription, through int64) {
	t.Helper()
	id, err := uuid.Parse(subscription.GetInboxId())
	if err != nil {
		t.Fatal(err)
	}
	// Only this inbox and subscription are due, whatever else the test made.
	s.exec(t, `UPDATE inbox SET next_attempt = now() + interval '1 hour' WHERE id <> $1`, id)
	s.exec(t, `UPDATE inbox SET next_attempt = now(), claim_until = now() WHERE id = $1`, id)
	s.exec(t, `UPDATE subscription SET next_attempt = now() WHERE inbox_id = $1`, id)
	claim, err := s.store.Claim(t.Context())
	if err != nil || claim == nil {
		t.Fatalf("claim = %v, %v", claim, err)
	}
	due, err := s.store.Source(t.Context(), *claim)
	if err != nil || due == nil {
		t.Fatalf("due = %v, %v", due, err)
	}
	var events []store.ActionEvent
	for sequence := int64(1); sequence <= through; sequence++ {
		events = append(events, store.ActionEvent{
			Sequence: sequence, State: action_servicepb.ActionState_ACTION_STATE_DECISION_PENDING, At: time.Date(2026, 10, 20, 3, 56, 3, 0, time.UTC),
		})
	}
	if err := s.store.RecordActionEvents(t.Context(), *claim, *due, events); err != nil {
		t.Fatal(err)
	}
	if err := s.store.Release(t.Context(), *claim, nil, nil); err != nil {
		t.Fatal(err)
	}
}
