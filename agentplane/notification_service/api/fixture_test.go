package api

import (
	"context"
	"database/sql"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	authlistener "github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	workloadresolver "github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/inboxaccess"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"

	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	testAudience  = "test-notification-service"
	testNamespace = "test-namespace"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline  = 30 * time.Second
	identityToken = "test-identity-svid"

	sandboxName      = "test-sandbox"
	otherSandboxName = "other-sandbox"
	operatorName     = "test-operator"
	testSession      = "test-session"
)

var (
	kube *kubeapiserver.Server
	// Account tokens remain for operator and negative ServiceAccount-authentication coverage. Session
	// owner calls use identityToken and are routed over an authenticated workload SVID connection.
	sandboxToken, otherSandboxToken, operatorToken, outsiderToken string
	// sandboxUID and otherSandboxUID are the UIDs of the two Sandboxes newServer creates for the current test,
	// and successorUID that of the test Sandbox recreateSandbox puts in its place. Tests run one at a time.
	sandboxUID, otherSandboxUID, successorUID string
	testPolicyIdentity                        *policyapi.Identity
	testIdentityWorkload                      *workloadauthtesting.IdentityWorkload
)

// routedClientConn keeps the same test client surface for reviewed ServiceAccount calls and
// Identity calls. The synthetic identityToken only selects a separate mTLS connection whose live
// SVID record names the exact Identity; it is removed before making that authenticated call.
type routedClientConn struct {
	mu       sync.RWMutex
	account  *grpc.ClientConn
	identity *grpc.ClientConn
}

func (r *routedClientConn) connection(ctx context.Context) (grpc.ClientConnInterface, context.Context) {
	md, _ := metadata.FromOutgoingContext(ctx)
	for _, authorization := range md.Get("authorization") {
		if authorization == "Bearer "+identityToken {
			md = md.Copy()
			md.Delete("authorization")
			ctx = metadata.NewOutgoingContext(ctx, md)
			r.mu.RLock()
			connection := r.identity
			r.mu.RUnlock()
			return connection, ctx
		}
	}
	return r.account, ctx
}

func (r *routedClientConn) Invoke(ctx context.Context, method string, args, reply any, options ...grpc.CallOption) error {
	connection, ctx := r.connection(ctx)
	return connection.Invoke(ctx, method, args, reply, options...)
}

func (r *routedClientConn) NewStream(ctx context.Context, description *grpc.StreamDesc, method string, options ...grpc.CallOption) (grpc.ClientStream, error) {
	connection, ctx := r.connection(ctx)
	return connection.NewStream(ctx, description, method, options...)
}

func (r *routedClientConn) replaceIdentity(connection *grpc.ClientConn) {
	r.mu.Lock()
	r.identity = connection
	r.mu.Unlock()
}

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
	asked    []*sandboxreader.Identity
}

func (r *fakeRunner) ListSessions(ctx context.Context, sandbox *sandboxreader.Identity) ([]string, error) {
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

func (r *fakeRunner) questions() []*sandboxreader.Identity {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]*sandboxreader.Identity(nil), r.asked...)
}

type actionsCall struct {
	owner         store.Owner
	requestID     uuid.UUID
	afterSequence int64
}

// fakeActions answers whether the account may follow an Action request.
type fakeActions struct {
	mu    sync.Mutex
	err   error
	calls []actionsCall
}

func (a *fakeActions) Events(_ context.Context, owner store.Owner, requestID uuid.UUID, afterSequence int64) ([]storagecodec.ActionEvent, error) {
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
	sources []model.GitHubSource
}

func (g *fakeGitHub) Authorize(_ context.Context, source model.GitHubSource) (store.GitHubAccessKey, error) {
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

func (g *fakeGitHub) authorized() []model.GitHubSource {
	g.mu.Lock()
	defer g.mu.Unlock()
	return append([]model.GitHubSource(nil), g.sources...)
}

// fakeHistory holds the Sandbox that wrote each Session.
type fakeHistory struct {
	mu        sync.Mutex
	sandboxes map[string]*historypb.SandboxRef
	err       error
	asked     []string
}

type fakeSessions struct {
	placements map[string]*dispatcherpb.Session
	err        error
}

func nativeSession(sessionID, name, uid string) *dispatcherpb.Session {
	identity := &identitypb.Identity{Name: name}
	if name != "" {
		identity.Uid = name + "-identity-uid"
	}
	return &dispatcherpb.Session{SessionId: sessionID, Identity: identity, Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{
		EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: name, Uid: uid}},
	}}}
}

func sessionWithIdentity(sessionID, name, uid string, identity *identitypb.Identity) *dispatcherpb.Session {
	session := nativeSession(sessionID, name, uid)
	if identity != nil {
		session.Identity = proto.Clone(identity).(*identitypb.Identity)
	}
	return session
}

func (o *fakeSessions) GetSession(_ context.Context, request *dispatcherpb.GetSessionRequest, _ ...grpc.CallOption) (*dispatcherpb.GetSessionResponse, error) {
	if o.err != nil {
		return nil, o.err
	}
	opening := o.placements[request.GetSessionId()]
	if opening == nil {
		return nil, status.Error(codes.NotFound, "opening not found")
	}
	return &dispatcherpb.GetSessionResponse{Session: opening}, nil
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
	requestTimeout time.Duration
	debounce       store.Debounce
	githubDisabled bool
	// operatorReader is the account allowed to inspect namespace Session diagnostics.
	operatorReader string
	// messageBodyLimit is in UTF-8 bytes.
	messageBodyLimit int
}

type testServer struct {
	notifications *Notifications
	client        notificationspb.NotificationsClient
	clientRoutes  *routedClientConn
	store         *store.Store
	hub           *wakeups.Hub
	db            *sql.DB
	runner        *fakeRunner
	actions       *fakeActions
	github        *fakeGitHub
	// policy is current and grants nothing until a test grants.
	policy          *inboxaccess.Policy
	history         *fakeHistory
	openings        *fakeSessions
	identity        *identitypb.Identity
	workload        *workloadauthtesting.IdentityWorkload
	ca              *transporttesting.CA
	identityAddress string
	// kube holds the Sandboxes the service reads.
	kube   client.Client
	config serverConfig
}

func identityWorkloadForTest(t *testing.T) workloadauthtesting.IdentityWorkload {
	t.Helper()
	if testIdentityWorkload != nil {
		testIdentityWorkload.Workload.Delete(t)
		testIdentityWorkload = nil
	}
	if testPolicyIdentity == nil {
		created := workloadauthtesting.NewIdentityWorkload(t, kube, testNamespace, sandboxName, testNamespace)
		testPolicyIdentity = created.Identity
		testIdentityWorkload = &created
	} else {
		created := workloadauthtesting.NewWorkloadForIdentity(t, kube, testNamespace, sandboxName, testPolicyIdentity)
		testIdentityWorkload = &created
	}
	// The authenticated credential and the Sandbox contract must name the same Identity. The
	// notification owner path verifies both the Session placement and the live Sandbox spec.
	current := testIdentityWorkload.Workload.Sandbox.DeepCopy()
	current.Spec.Identity = &sandboxapi.IdentityRef{Name: testIdentityWorkload.Identity.Name}
	if err := testIdentityWorkload.Workload.Objects.Update(t.Context(), current); err != nil {
		t.Fatal(err)
	}
	testIdentityWorkload.Workload.Sandbox = current
	return *testIdentityWorkload
}

func connectIdentityClient(t *testing.T, ca *transporttesting.CA, workload workloadauthtesting.Workload, address string) *grpc.ClientConn {
	t.Helper()
	clientCredentials, err := ca.Issue(t, workload.URI).ClientCredentials(environment_runtime_transport.NotificationsURI)
	if err != nil {
		t.Fatal(err)
	}
	connection, err := grpc.NewClient(address, grpc.WithTransportCredentials(clientCredentials), grpc.WithNoProxy())
	if err != nil {
		t.Fatal(err)
	}
	return connection
}

// newServer serves the API over the shared database, emptied of earlier tests' inboxes, with a hub listening to
// the channel the watches follow. Sandboxes are read through the real reader from the API server, where newServer
// replaces the earlier test's with fresh ones.
func newServer(t *testing.T, configure ...func(*serverConfig)) testServer {
	t.Helper()
	config := serverConfig{
		requestTimeout: callDeadline,
		debounce:       store.Debounce{Quiet: time.Minute, MaxWait: 2 * time.Minute},
		operatorReader: operatorName, messageBodyLimit: 64,
	}
	for _, configure := range configure {
		configure(&config)
	}
	db := postgres.Open(t, integrationDatabase.URL)
	if _, err := db.ExecContext(t.Context(), `TRUNCATE inbox CASCADE`); err != nil {
		t.Fatal(err)
	}
	hub, err := wakeups.Listen(t.Context(), integrationDatabase.URL, store.WorkChannel, store.InboxChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	if testIdentityWorkload != nil {
		testIdentityWorkload.Workload.Delete(t)
		testIdentityWorkload = nil
	}
	resolver, err := auth.NewResolver(kube.TokenReviews(), testAudience, []string{testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{sandboxapi.AddToScheme, policyapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	kubeClient, err := client.New(kube.Config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.DeleteAllOf(t.Context(), &sandboxapi.Sandbox{}, client.InNamespace(testNamespace)); err != nil {
		t.Fatal(err)
	}
	identityWorkload := identityWorkloadForTest(t)
	testIdentity := &identitypb.Identity{Name: identityWorkload.Identity.Name, Uid: string(identityWorkload.Identity.UID)}
	sandboxUID, otherSandboxUID, successorUID = string(identityWorkload.Sandbox.UID), createSandbox(t, kubeClient, otherSandboxName), ""
	sandboxes, err := sandboxreader.New(testNamespace, kubeClient)
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
		policy:  inboxaccess.NewPolicy(),
		history: &fakeHistory{sandboxes: map[string]*historypb.SandboxRef{
			testSession: {Namespace: testNamespace, Name: sandboxName, Uid: proto.String(sandboxUID)},
		}},
		kube:     kubeClient,
		identity: testIdentity,
		workload: &identityWorkload,
		ca:       transporttesting.NewCA(t),
		openings: &fakeSessions{placements: map[string]*dispatcherpb.Session{
			testSession:       sessionWithIdentity(testSession, sandboxName, sandboxUID, testIdentity),
			"missing-session": sessionWithIdentity("missing-session", sandboxName, sandboxUID, testIdentity),
		}},
	}
	server.policy.Health.MarkSynced()
	options := Options{
		Store: server.store, Wakeups: hub, Namespace: testNamespace, Sandboxes: sandboxes,
		Runner: server.runner, Actions: server.actions, OperatorReader: config.operatorReader,
		SendPolicy: server.policy, History: server.history, Sessions: server.openings, MessageBodyLimit: config.messageBodyLimit,
		Debounce: config.debounce, RequestTimeout: config.requestTimeout,
	}
	if !config.githubDisabled {
		options.GitHub = server.github
	}
	server.notifications = NewNotifications(options)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	grpcServer := grpc.NewServer(Methods(options, resolver).ServerOptions(config.requestTimeout)...)
	notificationspb.RegisterNotificationsServer(grpcServer, server.notifications)
	go func() { _ = grpcServer.Serve(listener) }()
	t.Cleanup(grpcServer.Stop)
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	svidResolver, err := auth.NewSVIDResolver(identityWorkload.Objects, []string{testNamespace}, &workloadresolver.Identities{PolicyNamespace: testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	serving := server.ca.Issue(t, environment_runtime_transport.NotificationsURI)
	serverTLS, err := serving.WorkloadServerTLSConfig(environment_runtime_transport.NotificationsURI)
	if err != nil {
		t.Fatal(err)
	}
	identityListenerConfig := authlistener.Config{
		ListenAddress: "127.0.0.1:0",
		TLS:           &authlistener.TLS{CertFile: serving.CertFile, KeyFile: serving.KeyFile},
		Auth:          []authlistener.Authentication{&authlistener.SPIFFEAuth{CAFile: serving.CAFile}},
	}
	identityResolver, err := resolver.WithListenerAuthentication(identityListenerConfig, svidResolver)
	if err != nil {
		t.Fatal(err)
	}
	identityListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	identityGRPCServer := grpc.NewServer(append(Methods(options, identityResolver).ServerOptions(config.requestTimeout), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	notificationspb.RegisterNotificationsServer(identityGRPCServer, server.notifications)
	go func() { _ = identityGRPCServer.Serve(identityListener) }()
	t.Cleanup(identityGRPCServer.Stop)
	identityConn := connectIdentityClient(t, server.ca, identityWorkload.Workload, identityListener.Addr().String())
	server.identityAddress = identityListener.Addr().String()
	server.clientRoutes = &routedClientConn{account: connection, identity: identityConn}
	t.Cleanup(func() {
		_ = connection.Close()
		_ = identityConn.Close()
	})
	server.client = notificationspb.NewNotificationsClient(server.clientRoutes)
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
		SessionId: testSession, IdempotencyKey: key, Source: actionsSource(uuid.New(), 0),
	}
}

// subscribe makes the Sandbox's subscription to a new Action request, for a Session of its own when it names one.
func (s testServer) subscribe(t *testing.T, key string, sessionID ...string) *notificationspb.Subscription {
	t.Helper()
	request := createRequest(key)
	if len(sessionID) > 0 {
		request.SessionId = sessionID[0]
		s.openings.placements[sessionID[0]] = sessionWithIdentity(sessionID[0], sandboxName, sandboxUID, s.identity)
		s.runner.mu.Lock()
		s.runner.sessions[sandboxName] = append(s.runner.sessions[sandboxName], sessionID[0])
		s.runner.mu.Unlock()
	}
	response, err := s.client.CreateSubscription(bearing(t, identityToken), request)
	if err != nil {
		t.Fatal(err)
	}
	return response.GetSubscription()
}

// createSandbox creates the named Sandbox in testNamespace and returns the UID the API server gave it.
func createSandbox(t *testing.T, kube client.Client, name string) string {
	t.Helper()
	sandbox := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: name},
		Spec:       sandboxapi.SandboxSpec{Template: "test-template"},
	}
	if err := kube.Create(t.Context(), sandbox); err != nil {
		t.Fatal(err)
	}
	return string(sandbox.UID)
}

// recreateSandbox deletes the test Sandbox and creates another under its name, whose UID becomes successorUID.
func (s testServer) recreateSandbox(t *testing.T) {
	t.Helper()
	if s.workload == nil || s.clientRoutes == nil || s.ca == nil {
		t.Fatal("Identity workload fixture is unavailable")
	}
	s.workload.Workload.Delete(t)
	successor := workloadauthtesting.NewWorkloadForIdentity(t, kube, testNamespace, sandboxName, s.workload.Identity)
	sandbox := successor.Workload.Sandbox.DeepCopy()
	sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: successor.Identity.Name}
	if err := successor.Workload.Objects.Update(t.Context(), sandbox); err != nil {
		t.Fatal(err)
	}
	successor.Workload.Sandbox = sandbox
	s.workload = &successor
	testIdentityWorkload = s.workload
	successorUID = string(successor.Sandbox.UID)
	sandboxUID = successorUID
	s.openings.placements["another-session"] = sessionWithIdentity("another-session", sandboxName, successorUID, s.identity)
	connection := connectIdentityClient(t, s.ca, successor.Workload, s.identityAddress)
	s.clientRoutes.replaceIdentity(connection)
	t.Cleanup(func() { _ = connection.Close() })
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
			Subject:    policyapi.Subject{Namespace: testNamespace, Name: account},
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
	var events []storagecodec.ActionEvent
	for sequence := int64(1); sequence <= through; sequence++ {
		events = append(events, storagecodec.ActionEvent{
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

func accountUID(t *testing.T, name string) string {
	t.Helper()
	account, err := kube.ServiceAccount(t.Context(), auth.Account{Namespace: testNamespace, Name: name})
	if err != nil {
		t.Fatal(err)
	}
	return string(account.UID)
}
