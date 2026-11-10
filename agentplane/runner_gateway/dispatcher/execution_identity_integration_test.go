package dispatcher

import (
	"context"
	"crypto/x509"
	"net"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/workload_auth/testing"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"k8s.io/apimachinery/pkg/util/sets"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const identitySandboxUID = "00000000-0000-4000-8000-000000009021"

// identityCatalog is a live Sandbox view whose execution account and named Identity a test sets.
type identityCatalog struct {
	testCatalog
	account  *ServiceAccountIdentity
	identity *identitypb.Identity
}

// GetExecutionAccount finds no account when account is nil: the Sandbox's managed ServiceAccount is absent.
func (c *identityCatalog) GetExecutionAccount(context.Context, SandboxSnapshot) (*ServiceAccountIdentity, bool, error) {
	return c.account, c.account != nil, nil
}

func (c *identityCatalog) GetExecutionIdentity(context.Context, SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return c.identity, true, nil
}

// placingRunner records who each placed Session executes as.
type placingRunner struct {
	acceptingRunner
	placed chan SessionExecution
}

func (r *placingRunner) CreateSession(_ context.Context, _ SandboxSnapshot, _ string, _ *sessionpb.SessionSpec, _ *string, execution SessionExecution) error {
	r.placed <- execution
	return nil
}

// serveIdentitySVIDs serves a Dispatcher over inventory that admits workload SVIDs, resolving
// Identity subjects in policyNamespace, and returns its store and a client presenting a certificate
// for a workload URI.
func serveIdentitySVIDs(t *testing.T, objects client.Reader, policyNamespace string, inventory *authorityInventory) (*Store, func(uri string) dispatcherpb.SessionDispatcherClient) {
	t.Helper()
	svids, err := auth.NewSVIDResolver(objects, []string{caller.Namespace}, &resolver.Identities{PolicyNamespace: policyNamespace})
	if err != nil {
		t.Fatal(err)
	}
	tokens, err := auth.NewResolver(tokenReviewStub{}, authorityTestAudience, []string{caller.Namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	ca := transporttesting.NewCA(t)
	serverCredential := ca.IssueWithUsage(t, environment_runtime_transport.SessionDispatcherURI, x509.ExtKeyUsageServerAuth)
	listenerConfig := listener.Config{
		ListenAddress: ":8080",
		TLS:           &listener.TLS{CertFile: serverCredential.CertFile, KeyFile: serverCredential.KeyFile},
		Auth:          []listener.Authentication{&listener.SPIFFEAuth{CAFile: ca.RootFile}},
	}
	serverTLS, err := listenerConfig.TLSConfig(environment_runtime_transport.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS.NextProtos = []string{"h2"}
	workloadAuth, err := tokens.WithListenerAuthentication(listenerConfig, svids)
	if err != nil {
		t.Fatal(err)
	}
	store := testStore(t, 10)
	hub := &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true}
	service := &Service{
		Store: store, DispatcherCallers: sets.New[auth.Account](),
		Sandboxes: inventory, Hub: hub, WatchLease: 10 * time.Second,
	}
	server := grpc.NewServer(append(Methods(auth.ReviewedServiceAccountOrIdentity(workloadAuth)).ServerOptions(time.Second),
		grpc.Creds(credentials.NewTLS(serverTLS)))...)
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	netListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(netListener) }()
	t.Cleanup(server.Stop)
	dial := func(uri string) dispatcherpb.SessionDispatcherClient {
		clientTLS, err := ca.Issue(t, uri).ClientTLSConfig(environment_runtime_transport.SessionDispatcherURI)
		if err != nil {
			t.Fatal(err)
		}
		connection, err := grpc.NewClient(netListener.Addr().String(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)), grpc.WithNoProxy())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = connection.Close() })
		return dispatcherpb.NewSessionDispatcherClient(connection)
	}
	return store, dial
}

func TestSessionExecutesAsTheIdentityItsSandboxNamedAtCreation(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 10)
	owner := serviceAccountIdentityFor(Account(caller), "owner-uid")
	named := &identitypb.Identity{Name: "named-identity", Uid: "00000000-0000-4000-8000-0000000090a1"}
	snapshot := SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: identitySandboxUID}
	inventory := &authorityInventory{sandbox: snapshot, account: owner, accountFound: true, identity: named}
	hub := &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true}
	client := authorityClient(t, store, inventory, tokenReviewStub{"owner-account": {account: caller, uid: owner.GetUid()}}, hub)
	open := func() string {
		sessionID := uuid.NewString()
		if _, err := client.CreateSession(authorityContext(t, "owner-account"), &dispatcherpb.CreateSessionRequest{
			SessionId: sessionID, Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: snapshot.Name, Uid: snapshot.UID}, Harness: sessionpb.Harness_HARNESS_CODEX}}}},
		}); err != nil {
			t.Fatalf("CreateSession: %v", err)
		}
		return sessionID
	}
	placed, stranded := open(), open()
	stored, err := store.GetSession(ctx, placed)
	if err != nil || !proto.Equal(stored.ExecutionIdentity, named) || !proto.Equal(stored.ExecutionPrincipal, owner) {
		t.Fatalf("stored Session executes as %v / %v (%v), want %v beside %v", stored.ExecutionIdentity, stored.ExecutionPrincipal, err, named, owner)
	}
	read, err := client.GetSession(authorityContext(t, "owner-account"), &dispatcherpb.GetSessionRequest{SessionId: placed})
	if err != nil || !proto.Equal(read.GetSession().GetIdentity(), named) {
		t.Fatalf("GetSession reads the Session as executing as %v (%v), want %v", read.GetSession().GetIdentity(), err, named)
	}

	catalog := &identityCatalog{testCatalog: testCatalog{sandbox: snapshot}, account: owner, identity: named}
	runner := &placingRunner{placed: make(chan SessionExecution, 2)}
	worker := &DeliveryWorker{Store: store, Catalog: catalog, Leases: testLease{store: store}, Runner: runner}
	worker.init()
	defer worker.queue.ShutDown()
	lease, err := worker.Leases.Acquire(ctx, snapshot.Name, snapshot.UID)
	if err != nil || lease == nil {
		t.Fatalf("acquire delivery owner: %#v %v", lease, err)
	}
	worker.owners[snapshot.UID] = *lease
	if err := worker.processSession(ctx, placed); err != nil {
		t.Fatalf("place Session: %v", err)
	}
	select {
	case execution := <-runner.placed:
		if !proto.Equal(execution.Identity, named) || !proto.Equal(execution.Principal, owner) {
			t.Fatalf("placed Session executes as %v / %v, want %v beside %v", execution.Identity, execution.Principal, named, owner)
		}
	default:
		t.Fatal("Session was not placed")
	}

	// The Sandbox now names another Identity: the Session created under the first is not placed.
	catalog.identity = &identitypb.Identity{Name: "named-identity", Uid: "00000000-0000-4000-8000-0000000090a2"}
	if err := worker.processSession(ctx, stranded); err != nil {
		t.Fatalf("process stranded Session: %v", err)
	}
	refused, err := store.GetSession(ctx, stranded)
	if err != nil || refused.State != SessionFailedState || refused.Reason != "execution_identity_changed" {
		t.Fatalf("Session under a replaced Identity: %#v %v, want failed execution_identity_changed", refused, err)
	}
	select {
	case execution := <-runner.placed:
		t.Fatalf("Session under a replaced Identity was placed as %v", execution)
	default:
	}
}

// An Identity SVID caller acts on the Sessions whose execution identity is its exact Identity
// incarnation, and on no other: not another Identity's, nor one its own Sandbox's ServiceAccount owns.
// It creates none, even on a Sandbox acting as it.
func TestIdentitySVIDCallerActsOnlyOnItsIdentitysSessions(t *testing.T) {
	api, err := kubeapiserver.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := api.Stop(); err != nil {
			t.Error(err)
		}
	})
	const policyNamespace = "test-identity-policies"
	owner := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, "identity-owner", policyNamespace)
	other := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, "identity-other", policyNamespace)
	ownerIdentity := &identitypb.Identity{Name: owner.Identity.Name, Uid: string(owner.Identity.UID)}
	otherIdentity := &identitypb.Identity{Name: other.Identity.Name, Uid: string(other.Identity.UID)}
	ownerAccount := serviceAccountIdentityFor(owner.Principal.Account, owner.Principal.ServiceAccountUID)
	ownerSandbox := SandboxSnapshot{Name: owner.Sandbox.Name, Namespace: owner.Sandbox.Namespace, UID: string(owner.Sandbox.UID)}
	inventory := &authorityInventory{sandbox: ownerSandbox, account: ownerAccount, accountFound: true, identity: ownerIdentity}

	store, client := serveIdentitySVIDs(t, owner.Objects, policyNamespace, inventory)
	ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
	defer cancel()
	createRequest := func(sessionID string) *dispatcherpb.CreateSessionRequest {
		return &dispatcherpb.CreateSessionRequest{
			SessionId: sessionID, Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: ownerSandbox.Name, Uid: ownerSandbox.UID}, Harness: sessionpb.Harness_HARNESS_CODEX}}}},
		}
	}

	ownerClient := client(owner.URI)
	if _, err := ownerClient.CreateSession(ctx, createRequest(uuid.NewString())); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("Identity caller CreateSession on a Sandbox acting as it: %v, want PermissionDenied", err)
	}
	owned, ownedSpec := uuid.NewString(), &sessionpb.SessionSpec{}
	if _, err := store.CreateSessionWithPlacementForCaller(ctx, owned, ownerSandbox.Name, ownerSandbox.UID,
		testPlacement(ownedSpec, ownerSandbox.Name, ownerSandbox.UID), ownedSpec, nil, nil,
		SessionExecution{Principal: ownerAccount, Identity: ownerIdentity}, IdentityCaller{Identity: ownerIdentity, Proof: IdentitySVID{}}); err != nil {
		t.Fatalf("seed the Identity's Session: %v", err)
	}
	ownerAttribution := &IdentityAttribution{Identity: ownerIdentity, Pod: &PodIdentity{Name: owner.Principal.PodName, UID: owner.Principal.PodUID}}
	if _, err := ownerClient.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
		CommandId: "identity-owned-input", SessionId: owned, ExpectedSandboxUid: ownerSandbox.UID, Text: "owned",
	}); err != nil {
		t.Fatalf("Identity caller SubmitInput: %v", err)
	}
	queued, err := store.GetDispatch(ctx, "identity-owned-input")
	if recorded, ok := queued.Attribution.(*IdentityAttribution); err != nil || !ok || !proto.Equal(recorded.Identity, ownerIdentity) {
		t.Fatalf("Dispatch admitted for %#v (%v), want %#v", queued.Attribution, err, ownerAttribution)
	}
	if got, err := ownerClient.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: owned}); err != nil || got.GetSession().GetSessionId() != owned {
		t.Fatalf("Identity caller GetSession: %v, %v", got, err)
	}
	if got, err := ownerClient.GetDispatch(ctx, &dispatcherpb.GetDispatchRequest{CommandId: "identity-owned-input"}); err != nil || got.GetDispatch().GetCommandId() != "identity-owned-input" {
		t.Fatalf("Identity caller GetDispatch: %v, %v", got, err)
	}

	// Another Identity's Session, and one its own Sandbox's ServiceAccount owns with no Identity.
	otherSandbox := SandboxSnapshot{Name: other.Sandbox.Name, Namespace: other.Sandbox.Namespace, UID: string(other.Sandbox.UID)}
	otherSession, otherSpec := uuid.NewString(), &sessionpb.SessionSpec{}
	if _, err := store.CreateSessionWithPlacementForCaller(ctx, otherSession, otherSandbox.Name, otherSandbox.UID,
		testPlacement(otherSpec, otherSandbox.Name, otherSandbox.UID), otherSpec, nil, nil,
		SessionExecution{Principal: serviceAccountIdentityFor(other.Principal.Account, other.Principal.ServiceAccountUID), Identity: otherIdentity},
		IdentityCaller{Identity: otherIdentity, Proof: IdentitySVID{}}); err != nil {
		t.Fatalf("seed another Identity's Session: %v", err)
	}
	accountSession := uuid.NewString()
	if _, err := store.CreateSessionForCaller(ctx, accountSession, ownerSandbox.Name, ownerSandbox.UID, &sessionpb.SessionSpec{}, nil, nil, ownerAccount,
		ServiceAccountCaller{ServiceAccountIdentity: ownerAccount, Attribution: &AdmissionAttribution{Actor: ownerAccount, EffectiveCaller: ownerAccount}}); err != nil {
		t.Fatalf("seed the ServiceAccount's Session: %v", err)
	}
	for _, foreign := range []string{otherSession, accountSession} {
		if _, err := ownerClient.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: foreign}); status.Code(err) != codes.NotFound {
			t.Fatalf("Identity caller read a Session it does not own: %v", err)
		}
		if _, err := ownerClient.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
			CommandId: uuid.NewString(), SessionId: foreign, ExpectedSandboxUid: ownerSandbox.UID, Text: "foreign",
		}); status.Code(err) != codes.NotFound {
			t.Fatalf("Identity caller wrote to a Session it does not own: %v", err)
		}
	}
	listed, err := ownerClient.ListSessions(ctx, &dispatcherpb.ListSessionsRequest{})
	if err != nil || len(listed.GetSessions()) != 1 || listed.GetSessions()[0].GetSessionId() != owned {
		t.Fatalf("Identity caller ListSessions = %v, %v; want only its Session", listed, err)
	}

	otherClient := client(other.URI)
	if _, err := otherClient.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: owned}); status.Code(err) != codes.NotFound {
		t.Fatalf("another Identity read the Session: %v", err)
	}
	if _, err := otherClient.CancelDispatch(ctx, &dispatcherpb.CancelDispatchRequest{CommandId: "identity-owned-input"}); status.Code(err) != codes.NotFound {
		t.Fatalf("another Identity cancelled the Session's command: %v", err)
	}
	if _, err := ownerClient.CancelDispatch(ctx, &dispatcherpb.CancelDispatchRequest{CommandId: "identity-owned-input"}); err != nil {
		t.Fatalf("Identity caller CancelDispatch of its own command: %v", err)
	}
}

// A Sandbox naming an Identity whose managed ServiceAccount is absent runs its Sessions as that
// Identity alone: its Identity writes to them, the Dispatcher places and delivers them
// recording no ServiceAccount, and strands them once the Sandbox names another Identity.
func TestAnIdentityRunSandboxNeedsNoManagedServiceAccount(t *testing.T) {
	api, err := kubeapiserver.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := api.Stop(); err != nil {
			t.Error(err)
		}
	})
	const policyNamespace = "test-identity-run-policies"
	owner := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, "identity-run", policyNamespace)
	named := &identitypb.Identity{Name: owner.Identity.Name, Uid: string(owner.Identity.UID)}
	sandbox := SandboxSnapshot{Name: owner.Sandbox.Name, Namespace: owner.Sandbox.Namespace, UID: string(owner.Sandbox.UID)}
	store, client := serveIdentitySVIDs(t, owner.Objects, policyNamespace, &authorityInventory{sandbox: sandbox, identity: named})
	ownerClient := client(owner.URI)
	ctx, cancel := context.WithTimeout(t.Context(), 20*time.Second)
	defer cancel()

	sessionID, spec := uuid.NewString(), &sessionpb.SessionSpec{}
	if _, err := store.CreateSessionWithPlacementForCaller(ctx, sessionID, sandbox.Name, sandbox.UID,
		testPlacement(spec, sandbox.Name, sandbox.UID), spec, nil, nil, SessionExecution{Identity: named}, IdentityCaller{Identity: named, Proof: IdentitySVID{}}); err != nil {
		t.Fatalf("seed an Identity-run Session: %v", err)
	}
	for _, commandID := range []string{"identity-run-input", "identity-run-stranded"} {
		if _, err := ownerClient.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
			CommandId: commandID, SessionId: sessionID, ExpectedSandboxUid: sandbox.UID, Text: "input",
		}); err != nil {
			t.Fatalf("SubmitInput to an Identity-run Session: %v", err)
		}
	}

	catalog := &identityCatalog{testCatalog: testCatalog{sandbox: sandbox}, identity: named}
	runner := &placingRunner{acceptingRunner: acceptingRunner{calls: make(chan string, 2)}, placed: make(chan SessionExecution, 1)}
	worker := &DeliveryWorker{Store: store, Catalog: catalog, Leases: testLease{store: store}, Runner: runner}
	worker.init()
	defer worker.queue.ShutDown()
	lease, err := worker.Leases.Acquire(ctx, sandbox.Name, sandbox.UID)
	if err != nil || lease == nil {
		t.Fatalf("acquire delivery owner: %#v %v", lease, err)
	}
	worker.owners[sandbox.UID] = *lease
	if err := worker.processSession(ctx, sessionID); err != nil {
		t.Fatalf("place Session: %v", err)
	}
	select {
	case execution := <-runner.placed:
		if !proto.Equal(execution.Identity, named) || execution.Principal != nil {
			t.Fatalf("placed Session executes as %v / %v, want %v alone", execution.Identity, execution.Principal, named)
		}
	default:
		t.Fatal("Session was not placed")
	}
	if err := worker.processSession(ctx, sessionID); err != nil {
		t.Fatalf("deliver input: %v", err)
	}
	if delivered, err := store.GetDispatch(ctx, "identity-run-input"); err != nil || delivered.State != AdmittedState {
		t.Fatalf("input to an Identity-run Session: %#v %v, want admitted", delivered, err)
	}

	// The Sandbox now names a same-named successor: queued work is stranded, not delivered as it.
	catalog.identity = &identitypb.Identity{Name: named.Name, Uid: "00000000-0000-4000-8000-0000000090b2"}
	if err := worker.processSession(ctx, sessionID); err != nil {
		t.Fatalf("process stranded input: %v", err)
	}
	if stranded, err := store.GetDispatch(ctx, "identity-run-stranded"); err != nil || stranded.State != FailedState || stranded.Reason != "execution_identity_changed" {
		t.Fatalf("input under a replaced Identity: %#v %v, want failed execution_identity_changed", stranded, err)
	}
}
