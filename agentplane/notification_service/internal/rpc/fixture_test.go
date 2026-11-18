package rpc

import (
	"context"
	"database/sql"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	workloadresolver "github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	inboxstore "github.com/agentydragon/ducktape/agentplane/notification_service/internal/inbox"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/inboxaccess"
	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/wakeup"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	testAudience  = "test-notification-service"
	testNamespace = "test-namespace"
	// callDeadline only bounds a call that stopped making progress, so a hang fails the test.
	callDeadline        = 30 * time.Second
	identityCaller      = "test-identity-svid"
	otherIdentityCaller = "test-other-identity-svid"
	operatorCaller      = "test-app-service-svid"

	sandboxName      = "test-sandbox"
	otherSandboxName = "other-sandbox"
	operatorName     = "test-operator"
	testSession      = "test-session"
)

var (
	kube *kubeapiserver.Server
	// Bound account tokens are used only to verify retired credentials are rejected.
	sandboxToken, otherSandboxToken, operatorToken, outsiderToken string
	// sandboxUID and otherSandboxUID are the UIDs of the two Sandboxes newServer creates for the current test,
	// and successorUID that of the test Sandbox recreateSandbox puts in its place. Tests run one at a time.
	sandboxUID, otherSandboxUID, successorUID string
	testPolicyIdentity                        *policyapi.Identity
	testIdentityWorkload                      *workloadauthtesting.IdentityWorkload
	testOtherWorkload                         *workloadauthtesting.IdentityWorkload
)

// Test caller selectors choose authenticated connections without sending synthetic metadata.
type (
	callerContextKey struct{}
	routedClientConn struct {
		mu                                         sync.RWMutex
		unauthenticated, identity, other, operator *grpc.ClientConn
	}
)

func (r *routedClientConn) connection(ctx context.Context) (grpc.ClientConnInterface, context.Context) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	switch ctx.Value(callerContextKey{}) {
	case identityCaller:
		return r.identity, ctx
	case otherIdentityCaller:
		return r.other, ctx
	case operatorCaller:
		return r.operator, ctx
	default:
		return r.unauthenticated, ctx
	}
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

type actionsCall struct {
	owner         inboxstore.Owner
	requestID     uuid.UUID
	afterSequence int64
}

// fakeActions answers whether the account may follow an Action request.
type fakeActions struct {
	mu    sync.Mutex
	err   error
	calls []actionsCall
}

func (a *fakeActions) Events(_ context.Context, owner inboxstore.Owner, requestID uuid.UUID, afterSequence int64) ([]storagecodec.ActionEvent, error) {
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
	access  githubstate.AccessKey
	err     error
	sources []githubmodel.Source
}

func (g *fakeGitHub) Authorize(_ context.Context, source githubmodel.Source) (githubstate.AccessKey, error) {
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

func (g *fakeGitHub) authorized() []githubmodel.Source {
	g.mu.Lock()
	defer g.mu.Unlock()
	return append([]githubmodel.Source(nil), g.sources...)
}

// fakeHistory holds the Sandbox that wrote each Session.
type fakeHistory struct {
	mu        sync.Mutex
	sandboxes map[string]*historypb.SandboxRef
	err       error
	asked     []string
}

type fakeSessions struct {
	sessions map[string]*dispatcherpb.Session
	err      error
	hangs    bool
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

func (o *fakeSessions) GetSession(ctx context.Context, request *dispatcherpb.GetSessionRequest, _ ...grpc.CallOption) (*dispatcherpb.GetSessionResponse, error) {
	if o.hangs {
		<-ctx.Done()
		return nil, ctx.Err()
	}
	if o.err != nil {
		return nil, o.err
	}
	session := o.sessions[request.GetSessionId()]
	if session == nil {
		return nil, status.Error(codes.NotFound, "opening not found")
	}
	return &dispatcherpb.GetSessionResponse{Session: session}, nil
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
	requestTimeout         time.Duration
	debounce               inboxstore.Debounce
	githubDisabled         bool
	operatorReaderServices []string
	// messageBodyLimit is in UTF-8 bytes.
	messageBodyLimit int
}

type testServer struct {
	notifications *Notifications
	client        notificationspb.NotificationsClient
	serviceClient notificationspb.NotificationsClient
	clientRoutes  *routedClientConn
	store         *inboxstore.Store
	hub           *wakeups.Hub
	db            *sql.DB
	actions       *fakeActions
	github        *fakeGitHub
	// policy is current and grants nothing until a test grants.
	policy          *inboxaccess.Policy
	history         *fakeHistory
	openings        *fakeSessions
	identity        *identitypb.Identity
	workload        *workloadauthtesting.IdentityWorkload
	otherWorkload   *workloadauthtesting.IdentityWorkload
	ca              *transporttesting.CA
	identityAddress string
	serviceAddress  string
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
	clientCredentials, err := ca.Issue(t, workload.URI).ClientCredentials(peertls.NotificationsURI)
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
		requestTimeout:         callDeadline,
		debounce:               inboxstore.Debounce{Quiet: time.Minute, MaxWait: 2 * time.Minute},
		operatorReaderServices: []string{peertls.AppURI}, messageBodyLimit: 64,
	}
	for _, configure := range configure {
		configure(&config)
	}
	db := postgres.Open(t, integrationDatabase.URL)
	if _, err := db.ExecContext(t.Context(), `TRUNCATE inbox CASCADE`); err != nil {
		t.Fatal(err)
	}
	hub, err := wakeups.Listen(t.Context(), integrationDatabase.URL, wakeup.WorkChannel, wakeup.InboxChannel)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { hub.Close() })
	if testIdentityWorkload != nil {
		testIdentityWorkload.Workload.Delete(t)
		testIdentityWorkload = nil
	}
	if testOtherWorkload != nil {
		testOtherWorkload.Workload.Delete(t)
		if err := testOtherWorkload.Objects.Delete(t.Context(), testOtherWorkload.Identity); err != nil {
			t.Fatal(err)
		}
		testOtherWorkload = nil
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
	otherWorkload := workloadauthtesting.NewIdentityWorkload(t, kube, testNamespace, otherSandboxName, testNamespace)
	testOtherWorkload = &otherWorkload
	sandboxUID, otherSandboxUID, successorUID = string(identityWorkload.Sandbox.UID), string(otherWorkload.Sandbox.UID), ""
	sandboxes, err := sandboxreader.New(testNamespace, kubeClient)
	if err != nil {
		t.Fatal(err)
	}
	server := testServer{
		store: inboxstore.New(db, transaction.New(db), inboxstore.Quotas{
			InboxesPerAccount: 64, ActiveSubscriptionsPerInbox: 64, EntriesPerInbox: 10_000,
			UnacknowledgedMessagesPerSender: 128,
		}),
		hub: hub, db: db, config: config,
		actions: &fakeActions{},
		github:  &fakeGitHub{access: githubstate.AccessKey{AppID: 11, InstallationID: 22, RepositoryID: 33}},
		policy:  inboxaccess.NewPolicy(),
		history: &fakeHistory{sandboxes: map[string]*historypb.SandboxRef{
			testSession: {Namespace: testNamespace, Name: sandboxName, Uid: proto.String(sandboxUID)},
		}},
		kube:     kubeClient,
		identity: testIdentity,
		workload: &identityWorkload, otherWorkload: &otherWorkload,
		ca: transporttesting.NewCA(t),
		openings: &fakeSessions{sessions: map[string]*dispatcherpb.Session{
			testSession: sessionWithIdentity(testSession, sandboxName, sandboxUID, testIdentity),
		}},
	}
	server.policy.Health.MarkSynced()
	options := Options{
		Store: server.store, Wakeups: hub, Namespace: testNamespace, Sandboxes: sandboxes,
		Actions:                server.actions,
		OperatorReaderServices: config.operatorReaderServices,
		SendPolicy:             server.policy, History: server.history, Sessions: server.openings, MessageBodyLimit: config.messageBodyLimit,
		Debounce: config.debounce, RequestTimeout: config.requestTimeout,
	}
	if !config.githubDisabled {
		options.GitHub = server.github
	}
	server.notifications = NewNotifications(options)
	svidResolver, err := auth.NewSVIDResolver(identityWorkload.Objects, []string{testNamespace}, &workloadresolver.Identities{PolicyNamespace: testNamespace})
	if err != nil {
		t.Fatal(err)
	}
	methods, err := Methods(options, svidResolver)
	if err != nil {
		t.Fatal(err)
	}
	serving := server.ca.Issue(t, peertls.NotificationsURI)
	serverTLS, err := serving.WorkloadServerTLSConfigWithServices(peertls.NotificationsURI, true)
	if err != nil {
		t.Fatal(err)
	}
	grpcServer := grpc.NewServer(append(methods.ServerOptions(config.requestTimeout), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	notificationspb.RegisterNotificationsServer(grpcServer, server.notifications)
	address := grpcserver.Serve(t, grpcServer)
	server.identityAddress, server.serviceAddress = address, address
	identityConn := connectIdentityClient(t, server.ca, identityWorkload.Workload, address)
	otherConn := connectIdentityClient(t, server.ca, otherWorkload.Workload, address)
	serviceCredentials, err := server.ca.Issue(t, peertls.AppURI).ClientCredentials(peertls.NotificationsURI)
	if err != nil {
		t.Fatal(err)
	}
	serviceConn, err := grpc.NewClient(address, grpc.WithTransportCredentials(serviceCredentials))
	if err != nil {
		t.Fatal(err)
	}
	unauthenticatedCredentials, err := (&peertls.ClientTLS{CAFile: serving.CAFile, ServerURI: peertls.NotificationsURI}).Credentials()
	if err != nil {
		t.Fatal(err)
	}
	connection, err := grpc.NewClient(address, grpc.WithTransportCredentials(unauthenticatedCredentials))
	if err != nil {
		t.Fatal(err)
	}
	server.serviceClient = notificationspb.NewNotificationsClient(serviceConn)
	server.clientRoutes = &routedClientConn{unauthenticated: connection, identity: identityConn, other: otherConn, operator: serviceConn}
	t.Cleanup(func() {
		_ = connection.Close()
		_ = identityConn.Close()
		_ = otherConn.Close()
		_ = serviceConn.Close()
	})
	server.client = notificationspb.NewNotificationsClient(server.clientRoutes)
	return server
}

// bearing selects a test SVID caller, or sends an actual bearer for rejection tests.
func bearing(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), callDeadline)
	t.Cleanup(cancel)
	switch token {
	case "":
		return ctx
	case identityCaller, otherIdentityCaller, operatorCaller:
		return context.WithValue(ctx, callerContextKey{}, token)
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
		s.openings.sessions[sessionID[0]] = sessionWithIdentity(sessionID[0], sandboxName, sandboxUID, s.identity)
	}
	response, err := s.client.CreateSubscription(bearing(t, identityCaller), request)
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
	s.openings.sessions["another-session"] = sessionWithIdentity("another-session", sandboxName, successorUID, s.identity)
	connection := connectIdentityClient(t, s.ca, successor.Workload, s.identityAddress)
	s.clientRoutes.replaceIdentity(connection)
	t.Cleanup(func() { _ = connection.Close() })
}

// grantSend binds send_message to the sender Identity, or an unrelated account for denial checks.
// The binding is named after the account.
func (s testServer) grantSend(account string, targets policyapi.SandboxTargets) {
	s.policy.Rules.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: account + "-send"},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{
			{Type: "session_inbox_access", Operations: []string{"send_message"}, Targets: &targets},
		}},
	})
	subject := policyapi.Subject{Namespace: testNamespace, Name: account}
	if account == otherSandboxName {
		s.policy.Rules.PutIdentity(s.otherWorkload.Identity)
		subject = policyapi.Subject{Identity: &policyapi.IdentityRef{Name: s.otherWorkload.Identity.Name}}
	}
	s.policy.Rules.PutBinding(&policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: account},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    subject,
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
