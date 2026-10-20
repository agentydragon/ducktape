package api

import (
	"context"
	"database/sql"
	"net"
	"sync"
	"testing"
	"time"

	operatorpb "github.com/agentydragon/ducktape/agentplane/notification_service/notifications_operatorpb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/authtest"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const (
	testAudience  = "test-notification-service"
	testNamespace = "test-namespace"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline = 30 * time.Second

	sandboxName       = "test-sandbox"
	sandboxUID        = "test-sandbox-uid"
	otherSandboxName  = "other-sandbox"
	otherSandboxUID   = "other-sandbox-uid"
	operatorName      = "test-operator"
	testSession       = "test-session"
	sandboxToken      = "test-token-sandbox"
	otherSandboxToken = "test-token-other-sandbox"
	operatorToken     = "test-token-operator"
	outsiderToken     = "test-token-outsider"
)

// Bearer -> ServiceAccount. Test literals, not real credentials. A Sandbox runs as the ServiceAccount of its own
// name.
var testAccounts = map[string]authtest.Account{
	sandboxToken:      {Namespace: testNamespace, Name: sandboxName, UID: "test-sandbox-account-uid"},
	otherSandboxToken: {Namespace: testNamespace, Name: otherSandboxName, UID: "test-other-sandbox-account-uid"},
	operatorToken:     {Namespace: testNamespace, Name: operatorName, UID: "test-operator-account-uid"},
	outsiderToken:     {Namespace: "test-elsewhere", Name: sandboxName, UID: "test-outsider-account-uid"},
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

type serverConfig struct {
	lease, requestTimeout time.Duration
	debounce              store.Debounce
	githubDisabled        bool
	// operatorReader is the account that may call NotificationsOperator, none when empty.
	operatorReader string
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
	config          serverConfig
}

// newServer serves both APIs over the shared database, emptied of earlier tests' inboxes, with a hub listening to
// the channel the watches follow. Sandboxes are read through the real reader over a fake Kubernetes client.
func newServer(t *testing.T, configure ...func(*serverConfig)) testServer {
	t.Helper()
	config := serverConfig{
		lease: time.Hour, requestTimeout: callDeadline,
		debounce:       store.Debounce{Quiet: time.Minute, MaxWait: 2 * time.Minute},
		operatorReader: operatorName,
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
	resolver, err := auth.NewResolver(authtest.Reviews{Audience: testAudience, Accounts: testAccounts}, testAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	sandboxes, err := sandboxreader.New(testNamespace, clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(
		&sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: sandboxName, UID: sandboxUID}},
		&sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: otherSandboxName, UID: otherSandboxUID}},
	).Build())
	if err != nil {
		t.Fatal(err)
	}
	server := testServer{
		store: store.New(db, store.Quotas{InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000}),
		hub:   hub, db: db, config: config,
		runner: &fakeRunner{sessions: map[string][]string{
			sandboxName: {testSession, "another-session"}, otherSandboxName: {testSession},
		}},
		actions: &fakeActions{},
		github:  &fakeGitHub{access: store.GitHubAccessKey{AppID: 11, InstallationID: 22, RepositoryID: 33}},
	}
	options := Options{
		Store: server.store, Wakeups: hub, Resolver: resolver, Namespace: testNamespace, Sandboxes: sandboxes,
		Runner: server.runner, Actions: server.actions, OperatorReader: config.operatorReader,
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
			Sequence: sequence, State: store.ActionDecisionPending, At: time.Date(2026, 10, 20, 3, 56, 3, 0, time.UTC),
		})
	}
	if err := s.store.RecordActionEvents(t.Context(), *claim, *due, events); err != nil {
		t.Fatal(err)
	}
	if err := s.store.Release(t.Context(), *claim, nil, nil); err != nil {
		t.Fatal(err)
	}
}
