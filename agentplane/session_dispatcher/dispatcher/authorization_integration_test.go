package dispatcher

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/access"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
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
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"

	"sigs.k8s.io/controller-runtime/pkg/client"
)

const authoritySandboxUID = "00000000-0000-4000-8000-000000009013"

type authorityInventory struct {
	mu              sync.Mutex
	sandbox         SandboxSnapshot
	identity        *identitypb.Identity
	environment     *sandboxapi.Sandbox
	identityMissing bool
	sandboxErr      error
}

func (i *authorityInventory) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if i.sandboxErr != nil {
		return SandboxSnapshot{}, false, i.sandboxErr
	}
	return i.sandbox, name == i.sandbox.Name, nil
}

func (i *authorityInventory) GetExecutionIdentity(_ context.Context, sandbox SandboxSnapshot) (*identitypb.Identity, bool, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if sandbox.Name != i.sandbox.Name || sandbox.UID != i.sandbox.UID {
		return nil, false, nil
	}
	if i.identityMissing {
		return nil, true, nil
	}
	if i.identity == nil {
		return &identitypb.Identity{Name: "authority-test-identity", Uid: "00000000-0000-4000-8000-00000000a001"}, true, nil
	}
	return i.identity, true, nil
}

func (i *authorityInventory) GetEnvironment(_ context.Context, sandbox SandboxSnapshot) (*sandboxapi.Sandbox, bool, error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	if sandbox.Name != i.sandbox.Name || sandbox.UID != i.sandbox.UID {
		return nil, false, nil
	}
	if i.environment != nil {
		return i.environment.DeepCopy(), true, nil
	}
	resource := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
		Namespace: i.sandbox.Namespace, Name: i.sandbox.Name, UID: types.UID(i.sandbox.UID),
	}}
	identity := i.identity
	if identity == nil && !i.identityMissing {
		identity = &identitypb.Identity{Name: "authority-test-identity", Uid: "00000000-0000-4000-8000-00000000a001"}
	}
	if identity != nil {
		resource.Spec.Identity = &sandboxapi.IdentityRef{Name: identity.GetName()}
	}
	resource.Spec.Running = true
	return resource, true, nil
}

func (i *authorityInventory) failSandboxRead(err error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.sandboxErr = err
}

type authorityPeers struct {
	component         dispatcherpb.SessionDispatcherClient
	sameNamedIdentity dispatcherpb.SessionDispatcherClient
	otherIdentity     dispatcherpb.SessionDispatcherClient
}

// authorityClients exercises the live workload Identity rule and the independent component grant.
// Neither workload Identity inherits the component's authority from its ServiceAccount name.
func authorityClients(t *testing.T, queueStore *store.Store, inventory *authorityInventory, hub *wakeups.Hub) authorityPeers {
	t.Helper()
	api := kubeapiserver.StartForTest(t)
	sameNamed := workloadauthtesting.NewWorkload(t, api, caller.Namespace, caller.Name)
	other := workloadauthtesting.NewWorkload(t, api, caller.Namespace, "test-stranger")
	svids, err := auth.NewSVIDResolver(sameNamed.Objects, []string{caller.Namespace}, &resolver.Identities{PolicyNamespace: caller.Namespace})
	if err != nil {
		t.Fatal(err)
	}
	rule, err := auth.AnySubject(svids).AllowServiceCallers([]string{peertls.AppURI})
	if err != nil {
		t.Fatal(err)
	}
	service := &Service{
		Store: queueStore, DispatcherServices: sets.New(peertls.AppURI),
		Sandboxes: inventory, Hub: hub, WatchLease: 10 * time.Second,
		EnvironmentRules: access.NewIndex(), PolicyReady: func() error { return nil },
	}
	ca := transporttesting.NewCA(t)
	serverTLS, err := ca.Issue(t, peertls.SessionDispatcherURI).WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(Methods(rule).ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	serverAddr := grpcserver.Serve(t, server)
	dial := func(uri string) dispatcherpb.SessionDispatcherClient {
		t.Helper()
		return authoritySVIDClient(t, serverAddr, uri, ca, ca.Issue(t, uri))
	}
	return authorityPeers{component: dial(peertls.AppURI), sameNamedIdentity: dial(sameNamed.URI), otherIdentity: dial(other.URI)}
}

func authoritySVIDContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 8*time.Second)
	t.Cleanup(cancel)
	return metadata.NewOutgoingContext(ctx, metadata.Pairs(
		"x-agentplane-subject", "attacker/forged",
	))
}

func authoritySVIDClient(
	t *testing.T,
	address, workloadURI string,
	ca *transporttesting.CA,
	presented peertls.Config,
) dispatcherpb.SessionDispatcherClient {
	t.Helper()
	clientCredential := ca.Issue(t, workloadURI)
	clientTLS, err := clientCredential.ClientTLSConfig(peertls.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	pair, err := tls.LoadX509KeyPair(presented.CertFile, presented.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	clientTLS.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
		return &pair, nil
	}
	connection, err := grpc.NewClient(
		address,
		grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)),
		grpc.WithNoProxy(),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return dispatcherpb.NewSessionDispatcherClient(connection)
}

type countingAuthorityDispatcher struct {
	dispatcherpb.SessionDispatcherServer
	listCalls atomic.Int32
}

func (s *countingAuthorityDispatcher) ListSessions(
	ctx context.Context,
	request *dispatcherpb.ListSessionsRequest,
) (*dispatcherpb.ListSessionsResponse, error) {
	s.listCalls.Add(1)
	return s.SessionDispatcherServer.ListSessions(ctx, request)
}

type authoritySVIDServer struct {
	queueStore *store.Store
	address    string
	ca         *transporttesting.CA
	counted    *countingAuthorityDispatcher
}

func serveAuthoritySVIDServer(
	t *testing.T,
	objects client.Reader,
	inventory *authorityInventory,
	environmentRules *reader.Index[access.Rule],
) authoritySVIDServer {
	t.Helper()
	svids, err := auth.NewSVIDResolver(objects, []string{caller.Namespace}, &resolver.Identities{PolicyNamespace: caller.Namespace})
	if err != nil {
		t.Fatal(err)
	}
	queueStore := testStore(t, 10)
	hub := testWatchHub(t, queueStore)
	service := &Service{
		Store:     queueStore,
		Sandboxes: inventory, EnvironmentRules: environmentRules, PolicyReady: func() error { return nil },
		Hub: hub, WatchLease: 10 * time.Second,
	}
	counted := &countingAuthorityDispatcher{SessionDispatcherServer: service}
	ca := transporttesting.NewCA(t)
	serverCredential := ca.Issue(t, peertls.SessionDispatcherURI)
	serverTLS, err := serverCredential.WorkloadServerTLSConfig(peertls.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS.NextProtos = []string{"h2", "http/1.1"}
	server := grpc.NewServer(append(Methods(auth.AnySubject(svids)).ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	dispatcherpb.RegisterSessionDispatcherServer(server, counted)
	serverAddr := grpcserver.Serve(t, server)
	return authoritySVIDServer{queueStore: queueStore, address: serverAddr, ca: ca, counted: counted}
}

func (s authoritySVIDServer) client(t *testing.T, uri string, leaf peertls.Config) dispatcherpb.SessionDispatcherClient {
	t.Helper()
	return authoritySVIDClient(t, s.address, uri, s.ca, leaf)
}

func requireRejectedLeavesAndMixedAuthentication(t *testing.T, server authoritySVIDServer, workloadURI string) {
	t.Helper()
	validOwner := server.client(t, workloadURI, server.ca.Issue(t, workloadURI))
	invalidLeaves := map[string]peertls.Config{
		"forged":  transporttesting.NewCA(t).Issue(t, workloadURI),
		"expired": server.ca.IssueWithValidity(t, workloadURI, time.Now().Add(-time.Hour), time.Now().Add(-time.Minute), x509.ExtKeyUsageClientAuth),
	}
	for name, leaf := range invalidLeaves {
		t.Run(name+"_leaf_rejected_before_dispatcher_handler", func(t *testing.T) {
			_, err := server.client(t, workloadURI, leaf).ListSessions(authoritySVIDContext(t), &dispatcherpb.ListSessionsRequest{})
			if err == nil {
				t.Fatal("invalid workload leaf reached a successful Dispatcher RPC")
			}
			if calls := server.counted.listCalls.Load(); calls != 0 {
				t.Fatalf("invalid workload leaf reached Dispatcher ListSessions handler %d times", calls)
			}
		})
	}
	mixed := metadata.AppendToOutgoingContext(authoritySVIDContext(t), "authorization", "Bearer foreign-bearer")
	if _, err := validOwner.ListSessions(mixed, &dispatcherpb.ListSessionsRequest{}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("certificate plus bearer = %v, want Unauthenticated", err)
	}
	if calls := server.counted.listCalls.Load(); calls != 0 {
		t.Fatalf("mixed credentials reached Dispatcher ListSessions handler %d times", calls)
	}
}

func TestDispatcherServiceAccountSVIDDoesNotOwnSandboxIdentitySessions(t *testing.T) {
	api := kubeapiserver.StartForTest(t)
	owner := workloadauthtesting.NewWorkload(t, api, caller.Namespace, caller.Name)
	foreign := workloadauthtesting.NewWorkload(t, api, caller.Namespace, "test-foreign-caller")
	ownerSandbox := SandboxSnapshot{Name: owner.Sandbox.Name, Namespace: owner.Sandbox.Namespace, UID: string(owner.Sandbox.UID)}
	ownerIdentity := &identitypb.Identity{Name: owner.Identity.Name, Uid: string(owner.Identity.UID)}
	inventory := &authorityInventory{sandbox: ownerSandbox, identity: ownerIdentity}
	environmentRules := access.NewIndex()
	environmentRules.PutIdentity(owner.Identity)
	authority := serveAuthoritySVIDServer(t, owner.Objects, inventory, environmentRules)
	requireRejectedLeavesAndMixedAuthentication(t, authority, owner.URI)
	ownerClient := authority.client(t, owner.URI, authority.ca.Issue(t, owner.URI))
	sessionID := uuid.NewString()
	request := &dispatcherpb.CreateSessionRequest{
		SessionId: sessionID,
		Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{
			EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
				Environment: &sessionpb.ExactEnvironment{Name: ownerSandbox.Name, Uid: ownerSandbox.UID},
				Harness:     sessionpb.Harness_HARNESS_CODEX,
			},
		}}},
	}
	if _, err := ownerClient.CreateSession(authoritySVIDContext(t), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ServiceAccount SVID created a Session: %v, want PermissionDenied", err)
	}
	identity := &identitypb.Identity{Name: "svid-session-owner", Uid: "00000000-0000-4000-8000-00000000a002"}
	testPlacement(request.GetSpec(), ownerSandbox.Name, ownerSandbox.UID)
	if _, err := authority.queueStore.CreateSessionWithPlacementForCaller(t.Context(), sessionID, ownerSandbox.Name, ownerSandbox.UID,
		request.GetSpec(), nil, nil,
		store.SessionExecution{Identity: identity}, store.IdentityCaller{Identity: identity, Proof: store.IdentitySVID{}}); err != nil {
		t.Fatalf("seed Identity-owned Session: %v", err)
	}
	if _, err := ownerClient.GetSession(authoritySVIDContext(t), &dispatcherpb.GetSessionRequest{SessionId: sessionID}); status.Code(err) != codes.NotFound {
		t.Fatalf("ServiceAccount SVID read an Identity-owned Session: %v, want NotFound", err)
	}

	foreignClient := authority.client(t, foreign.URI, authority.ca.Issue(t, foreign.URI))
	if _, err := foreignClient.GetSession(authoritySVIDContext(t), &dispatcherpb.GetSessionRequest{SessionId: sessionID}); status.Code(err) != codes.NotFound {
		t.Fatalf("foreign workload Identity SVID read an Identity-owned Session: %v, want NotFound", err)
	}
}

func TestDispatcherIdentitySVIDDoesNotInheritSameNamedAccountAuthority(t *testing.T) {
	api := kubeapiserver.StartForTest(t)
	owner := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, caller.Name, caller.Namespace).Workload
	foreign := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, "test-foreign-caller", caller.Namespace).Workload
	ownerIdentity := &identitypb.Identity{Name: owner.Identity.Name, Uid: string(owner.Identity.UID)}
	ownerSandbox := SandboxSnapshot{Name: owner.Sandbox.Name, Namespace: owner.Sandbox.Namespace, UID: string(owner.Sandbox.UID)}
	inventory := &authorityInventory{sandbox: ownerSandbox, identity: ownerIdentity}
	environmentRules := access.NewIndex()
	environmentRules.PutIdentity(owner.Identity)
	authority := serveAuthoritySVIDServer(t, owner.Objects, inventory, environmentRules)
	requireRejectedLeavesAndMixedAuthentication(t, authority, owner.URI)
	ownerClient := authority.client(t, owner.URI, authority.ca.Issue(t, owner.URI))
	sessionID := uuid.NewString()
	_, err := ownerClient.CreateSession(authoritySVIDContext(t), &dispatcherpb.CreateSessionRequest{
		SessionId: sessionID,
		Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{
			EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
				Environment: &sessionpb.ExactEnvironment{Name: ownerSandbox.Name, Uid: ownerSandbox.UID},
				Harness:     sessionpb.Harness_HARNESS_CODEX,
			},
		}}},
	})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("Identity SVID inherited its same-named ServiceAccount's CreateSession authority: %v", err)
	}
	listed, err := ownerClient.ListSessions(authoritySVIDContext(t), &dispatcherpb.ListSessionsRequest{})
	if err != nil || len(listed.GetSessions()) != 0 {
		t.Fatalf("Identity SVID listed a Session through its same-named ServiceAccount: %v, %v", listed, err)
	}
	if calls := authority.counted.listCalls.Load(); calls != 1 {
		t.Fatalf("valid owner ListSessions handler calls = %d, want 1", calls)
	}

	foreignClient := authority.client(t, foreign.URI, authority.ca.Issue(t, foreign.URI))
	if _, err := foreignClient.GetSession(authoritySVIDContext(t), &dispatcherpb.GetSessionRequest{SessionId: sessionID}); status.Code(err) != codes.NotFound {
		t.Fatalf("foreign Sandbox SVID GetSession = %v, want NotFound", err)
	}
}

func TestRPCIdentityOnlyOwnershipKeepsServiceBackendReads(t *testing.T) {
	queueStore := testStore(t, 10)
	ownerIdentity := &identitypb.Identity{Name: "authority-owner", Uid: "00000000-0000-4000-8000-00000000a003"}
	inventory := &authorityInventory{
		sandbox:  SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: authoritySandboxUID},
		identity: ownerIdentity,
	}
	hub := testWatchHub(t, queueStore)
	peers := authorityClients(t, queueStore, inventory, hub)
	client := peers.component

	spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
		Environment: &sessionpb.ExactEnvironment{Name: caller.Name, Uid: authoritySandboxUID}, Harness: sessionpb.Harness_HARNESS_CODEX,
	}}}}
	request := &dispatcherpb.CreateSessionRequest{SessionId: uuid.NewString(), Spec: spec}
	if _, err := peers.sameNamedIdentity.CreateSession(authoritySVIDContext(t), request); status.Code(err) != codes.NotFound {
		t.Fatalf("same-named workload Identity created a Session: %v, want NotFound", err)
	}
	opened, err := client.CreateSession(authoritySVIDContext(t), request)
	if err != nil || !proto.Equal(opened.GetSession().GetIdentity(), ownerIdentity) {
		t.Fatalf("configured service component CreateSession = %v, %v; want the exact Sandbox Identity", opened, err)
	}
	if _, err := peers.sameNamedIdentity.GetSession(authoritySVIDContext(t), &dispatcherpb.GetSessionRequest{SessionId: request.GetSessionId()}); status.Code(err) != codes.NotFound {
		t.Fatalf("same-named workload Identity read Session ownership: %v, want NotFound", err)
	}
	if _, err := peers.otherIdentity.GetSession(authoritySVIDContext(t), &dispatcherpb.GetSessionRequest{SessionId: request.GetSessionId()}); status.Code(err) != codes.NotFound {
		t.Fatalf("foreign workload Identity read Session: %v, want NotFound", err)
	}
	if _, err := client.GetSession(authoritySVIDContext(t), &dispatcherpb.GetSessionRequest{SessionId: request.GetSessionId()}); err != nil {
		t.Fatalf("configured service component could not read Session: %v", err)
	}
	listed, err := client.ListSessions(authoritySVIDContext(t), &dispatcherpb.ListSessionsRequest{})
	if err != nil || len(listed.GetSessions()) != 1 || listed.GetSessions()[0].GetSessionId() != request.GetSessionId() {
		t.Fatalf("configured service component ListSessions = %v, %v", listed, err)
	}
	ordinary, err := peers.sameNamedIdentity.ListSessions(authoritySVIDContext(t), &dispatcherpb.ListSessionsRequest{})
	if err != nil || len(ordinary.GetSessions()) != 0 {
		t.Fatalf("same-named workload Identity ListSessions = %v, %v; want no owned Sessions", ordinary, err)
	}
}

func TestCreateSessionRejectsMissingIdentityEvenWhenSandboxLookupSucceeds(t *testing.T) {
	queueStore := testStore(t, 3)
	inventory := &authorityInventory{
		sandbox:         SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: authoritySandboxUID},
		identityMissing: true,
	}
	peers := authorityClients(t, queueStore, inventory, nil)
	client := peers.component
	request := &dispatcherpb.CreateSessionRequest{
		SessionId: uuid.NewString(),
		Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
			Environment: &sessionpb.ExactEnvironment{Name: caller.Name, Uid: authoritySandboxUID}, Harness: sessionpb.Harness_HARNESS_CODEX,
		}}}},
	}
	if _, err := client.CreateSession(authoritySVIDContext(t), request); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("CreateSession with a successful lookup but no Identity = %v, want FailedPrecondition", err)
	}
	if _, err := queueStore.GetSession(t.Context(), request.GetSessionId()); !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("missing-Identity opening was persisted: %v", err)
	}
}

// admissionAttribution is a row's ServiceAccount attribution, nil when it has none.
func admissionAttribution(attribution store.Attribution) *store.AdmissionAttribution {
	account, _ := attribution.(*store.AdmissionAttribution)
	return account
}

func TestSessionRetryReadsDurableReceiptWithoutOwnerInventory(t *testing.T) {
	queueStore := testStore(t, 3)
	identity := &identitypb.Identity{Name: "durable-retry-identity", Uid: "00000000-0000-4000-8000-00000000a005"}
	snapshot := SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: uuid.NewString()}
	inventory := &authorityInventory{sandbox: snapshot, identity: identity}
	peers := authorityClients(t, queueStore, inventory, nil)
	client := peers.component
	setup := "echo retained"
	request := &dispatcherpb.CreateSessionRequest{
		SessionId: uuid.NewString(),
		Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{
			EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
				Environment: &sessionpb.ExactEnvironment{Name: snapshot.Name, Uid: snapshot.UID},
				Harness:     sessionpb.Harness_HARNESS_CODEX,
			},
		}}},
		SetupScript: &setup,
	}
	if _, err := peers.sameNamedIdentity.CreateSession(authoritySVIDContext(t), request); status.Code(err) != codes.NotFound {
		t.Fatalf("same-named workload Identity created a Session: %v, want NotFound", err)
	}
	first, err := client.CreateSession(authoritySVIDContext(t), request)
	if err != nil || !proto.Equal(first.GetSession().GetIdentity(), identity) {
		t.Fatalf("backend CreateSession = %v, %v; want the retained Identity", first, err)
	}
	stored, err := queueStore.GetSession(t.Context(), request.GetSessionId())
	if err != nil {
		t.Fatal(err)
	}

	commandID := uuid.NewString()
	if _, err := client.SubmitInput(authoritySVIDContext(t), &dispatcherpb.SubmitInputRequest{
		SessionId: stored.SessionID, CommandId: commandID, Text: "test retained input",
	}); err != nil {
		t.Fatal(err)
	}

	// An accepted receipt retry does not need live inventory or rebind the execution Identity.
	inventory.failSandboxRead(errors.New("inventory unavailable after accepted opening"))
	again, err := client.CreateSession(authoritySVIDContext(t), request)
	if err != nil || !proto.Equal(first, again) {
		t.Fatalf("privileged receipt retry: %v %v", again, err)
	}
	for name, peer := range map[string]dispatcherpb.SessionDispatcherClient{"same-named Identity": peers.sameNamedIdentity, "other Identity": peers.otherIdentity} {
		if _, err := peer.CreateSession(authoritySVIDContext(t), request); status.Code(err) != codes.NotFound {
			t.Fatalf("%s learned a retained receipt: %v", name, err)
		}
	}
	conflict := proto.Clone(request).(*dispatcherpb.CreateSessionRequest)
	conflict.GetSpec().GetPlacement().GetEnvironmentHosted().Environment.Name += "-other"
	if _, err := client.CreateSession(authoritySVIDContext(t), conflict); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("backend request conflict = %v, want AlreadyExists", err)
	}

	// Reading failed or uncertain materialization never requeues it or changes its evidence.
	for _, state := range []string{store.SessionFailedState, store.SessionInProgressState} {
		if _, err := queueStore.DB.ExecContext(t.Context(), "UPDATE session SET state=$2,reason='retained outcome' WHERE session_id=$1", stored.SessionID, state); err != nil {
			t.Fatal(err)
		}
		if _, err := client.CreateSession(authoritySVIDContext(t), request); err != nil {
			t.Fatal(err)
		}
		after, err := queueStore.GetSession(t.Context(), stored.SessionID)
		if err != nil || after.State != state || after.Reason != "retained outcome" ||
			!after.UpdatedAt.Equal(stored.UpdatedAt) || !reflect.DeepEqual(after.Creator, stored.Creator) || !reflect.DeepEqual(after.Attribution, stored.Attribution) {
			t.Fatalf("receipt retry changed durable state: %+v %v", after, err)
		}
	}
	inventory.failSandboxRead(nil)
	if err := queueStore.EndSandbox(t.Context(), snapshot.UID); err != nil {
		t.Fatal(err)
	}
	read := &dispatcherpb.GetDispatchRequest{CommandId: commandID}
	if got, err := client.GetDispatch(authoritySVIDContext(t), read); err != nil ||
		got.GetDispatch().GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED || got.GetDispatch().GetSandboxUid() != snapshot.UID {
		t.Fatalf("component read after Environment deletion: %v, %v", got, err)
	}
	if _, err := peers.otherIdentity.GetDispatch(authoritySVIDContext(t), read); status.Code(err) != codes.NotFound {
		t.Fatalf("other Identity could discover retained command: %v", err)
	}
	retained, err := client.CreateSession(authoritySVIDContext(t), request)
	if err != nil || retained.GetSession().GetStatus().GetRequiredEnvironment() != dispatcherpb.SessionStatus_REQUIRED_ENVIRONMENT_DELETED ||
		retained.GetSession().GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_UNKNOWN {
		t.Fatalf("retained retry after deletion: %+v %v", retained, err)
	}
	if _, err := client.SubmitInput(authoritySVIDContext(t), &dispatcherpb.SubmitInputRequest{
		SessionId: stored.SessionID, ExpectedSandboxUid: snapshot.UID, CommandId: uuid.NewString(), Text: "must not send",
	}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("retained Session accepted work for deleted UID: %v", err)
	}
	if row, err := queueStore.GetSession(t.Context(), stored.SessionID); err != nil || row.EnvironmentDeletedAt == nil || row.SandboxUID != snapshot.UID {
		t.Fatalf("retained Session lost its exact placement: %+v %v", row, err)
	}
	if _, err := peers.sameNamedIdentity.GetSession(authoritySVIDContext(t), &dispatcherpb.GetSessionRequest{SessionId: stored.SessionID}); status.Code(err) != codes.NotFound {
		t.Fatalf("same-named workload Identity inherited backend Session access: %v", err)
	}
	if got, err := client.GetSession(authoritySVIDContext(t), &dispatcherpb.GetSessionRequest{SessionId: stored.SessionID}); err != nil ||
		got.GetSession().GetStatus().GetRequiredEnvironment() != dispatcherpb.SessionStatus_REQUIRED_ENVIRONMENT_DELETED {
		t.Fatalf("backend could not inspect retained Session: %+v %v", got, err)
	}
}

func TestSessionDiscoveryUsesRetainedAuthorityAndStablePages(t *testing.T) {
	queueStore := testStore(t, 3)
	uid := uuid.NewString()
	const queuedID = "00000000-0000-4000-8000-000000000001"
	const foreignID = "00000000-0000-4000-8000-000000000002"
	const failedID = "00000000-0000-4000-8000-000000000003"
	// Insert out of order; ordering is the immutable public ID, never delivery state.
	for _, row := range []struct {
		id      string
		harness sessionpb.Harness
	}{
		{failedID, sessionpb.Harness_HARNESS_CLAUDE},
		{foreignID, sessionpb.Harness_HARNESS_CENTRAL_SAMPLER},
		{queuedID, sessionpb.Harness_HARNESS_CODEX},
	} {
		spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "discovery-sandbox", Uid: uid}, Harness: row.harness}}}}
		if row.harness == sessionpb.Harness_HARNESS_CENTRAL_SAMPLER {
			spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: "discovery-sandbox", Uid: uid}}}}
		}
		if _, err := createSession(queueStore, t.Context(), row.id, "discovery-sandbox", uid, spec, nil, nil); err != nil {
			t.Fatal(err)
		}
	}
	deliveryOwner := claimSessionOwner(t, queueStore, failedID)
	if row, err := queueStore.SettleSession(t.Context(), failedID, deliveryOwner, []string{store.SessionWaitingState}, store.SessionFailedState, "runner_rejected"); err != nil || row == nil {
		t.Fatalf("reject opening: %+v %v", row, err)
	}
	inventory := &authorityInventory{sandboxErr: errors.New("sandbox inventory unavailable")}
	peers := authorityClients(t, queueStore, inventory, nil)
	client := peers.component
	first, err := client.ListSessions(authoritySVIDContext(t), &dispatcherpb.ListSessionsRequest{Limit: 1})
	if err != nil || len(first.GetSessions()) != 1 || first.GetSessions()[0].GetSessionId() != queuedID || first.GetNextPageToken() == "" {
		t.Fatalf("backend first page: %v %v", first, err)
	}
	if first.GetSessions()[0].GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_WAITING ||
		first.GetSessions()[0].GetPlacement().GetEnvironmentHosted().GetEnvironment().GetUid() != uid ||
		first.GetSessions()[0].GetPlacement().GetEnvironmentHosted().GetHarness() != sessionpb.Harness_HARNESS_CODEX {
		t.Fatalf("queued opening placement/state: %v", first)
	}
	got, err := client.GetSession(authoritySVIDContext(t), &dispatcherpb.GetSessionRequest{SessionId: queuedID})
	if err != nil || got.GetSession().GetPlacement().GetEnvironmentHosted().GetHarness() != sessionpb.Harness_HARNESS_CODEX {
		t.Fatalf("backend opening harness: %v %v", got, err)
	}
	// The last row of a page can disappear between requests without invalidating its cursor.
	if _, err := queueStore.DB.ExecContext(t.Context(), "DELETE FROM session WHERE session_id=$1", queuedID); err != nil {
		t.Fatal(err)
	}
	second, err := client.ListSessions(authoritySVIDContext(t), &dispatcherpb.ListSessionsRequest{
		Limit: 1, PageToken: first.GetNextPageToken(),
	})
	if err != nil || len(second.GetSessions()) != 1 || second.GetSessions()[0].GetSessionId() != foreignID ||
		second.GetNextPageToken() == "" || second.GetSessions()[0].GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment().GetUid() != uid {
		t.Fatalf("backend second page: %v %v", second, err)
	}
	third, err := client.ListSessions(authoritySVIDContext(t), &dispatcherpb.ListSessionsRequest{
		Limit: 1, PageToken: second.GetNextPageToken(),
	})
	if err != nil || len(third.GetSessions()) != 1 || third.GetSessions()[0].GetSessionId() != failedID ||
		third.GetNextPageToken() != "" || third.GetSessions()[0].GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_FAILED ||
		third.GetSessions()[0].GetPlacement().GetEnvironmentHosted().GetHarness() != sessionpb.Harness_HARNESS_CLAUDE {
		t.Fatalf("backend third page: %v %v", third, err)
	}
	// A cursor is position only; workload Identities cannot use a backend cursor as authority.
	for name, peer := range map[string]dispatcherpb.SessionDispatcherClient{"same-named Identity": peers.sameNamedIdentity, "other Identity": peers.otherIdentity} {
		page, err := peer.ListSessions(authoritySVIDContext(t), &dispatcherpb.ListSessionsRequest{PageToken: first.GetNextPageToken()})
		if err != nil || len(page.GetSessions()) != 0 || page.GetNextPageToken() != "" {
			t.Fatalf("%s gained backend scope from cursor: %v %v", name, page, err)
		}
	}
	for _, request := range []*dispatcherpb.ListSessionsRequest{
		{Limit: 101}, {PageToken: "not-a-page-token"}, {PageToken: string(make([]byte, 129))},
	} {
		if _, err := client.ListSessions(authoritySVIDContext(t), request); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("invalid page request %v: %v", request, err)
		}
	}
}

func TestSessionDispatchPagesRespectReadAuthorityAndImmutableSubmissionOrder(t *testing.T) {
	ctx := t.Context()
	queueStore := testStore(t, 5)
	id, uid := uuid.NewString(), uuid.NewString()
	enqueue := func(command string) {
		t.Helper()
		if _, err := enqueueWithSession(t, queueStore, ctx, id, "test-page-env", uid, input(command, "test retained text"), nil); err != nil {
			t.Fatal(err)
		}
	}
	for _, command := range []string{"test-first", "test-second", "test-third"} {
		enqueue(command)
	}
	peers := authorityClients(t, queueStore, &authorityInventory{sandboxErr: errors.New("inventory unavailable")}, nil)
	page := func(token string, limit uint32) *dispatcherpb.ListSessionDispatchesResponse {
		t.Helper()
		result, err := peers.component.ListSessionDispatches(authoritySVIDContext(t), &dispatcherpb.ListSessionDispatchesRequest{SessionId: id, PageToken: token, Limit: limit})
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	first := page("", 1)
	if len(first.Dispatches) != 1 || first.Dispatches[0].GetCommandId() != "test-third" || first.NextPageToken == "" {
		t.Fatalf("first receipt page: %v", first)
	}
	if _, err := enqueueWithSession(t, queueStore, ctx, uuid.NewString(), "foreign-env", uuid.NewString(), input("foreign", "private"), nil); err != nil {
		t.Fatal(err)
	}
	batch := &dispatcherpb.BatchGetDispatchesRequest{SessionId: id, CommandIds: []string{"foreign", "test-first", "test-third"}}
	values, err := peers.component.BatchGetDispatches(authoritySVIDContext(t), batch)
	if err != nil || len(values.GetResults()) != 3 || values.Results[0].CommandId != "foreign" || values.Results[0].GetNotFound() == nil {
		t.Fatalf("partial batch: %v, %v", values, err)
	}
	for i, id := range batch.CommandIds[1:] {
		row := values.Results[i+1]
		if row.CommandId != id || row.GetDispatch().GetReceiptVersion() <= 0 || !proto.Equal(row.GetDispatch().GetCommand(), input(id, "test retained text")) {
			t.Fatalf("ordered batch command %s: %v", id, row)
		}
	}
	if _, err := queueStore.DB.ExecContext(ctx, "ALTER TABLE dispatch DISABLE TRIGGER keep_session_command_on_environment_deletion"); err != nil {
		t.Fatal(err)
	}
	if _, err := peers.component.BatchGetDispatches(authoritySVIDContext(t), batch); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("unprotected database returned batch: %v", err)
	}
	if _, err := peers.otherIdentity.BatchGetDispatches(authoritySVIDContext(t), batch); status.Code(err) != codes.NotFound {
		t.Fatalf("cross-owner lookup: %v", err)
	}
	for _, ids := range [][]string{nil, {""}, {"a", "a"}, make([]string, 17)} {
		if _, err := peers.component.BatchGetDispatches(authoritySVIDContext(t), &dispatcherpb.BatchGetDispatchesRequest{SessionId: id, CommandIds: ids}); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("invalid batch: %v", err)
		}
	}
	for _, session := range []string{id, "test-no-such-session"} {
		_, err := peers.otherIdentity.ListSessionDispatches(authoritySVIDContext(t), &dispatcherpb.ListSessionDispatchesRequest{SessionId: session})
		if status.Code(err) != codes.NotFound {
			t.Fatalf("foreign Identity enumerated Session %q: %v", session, err)
		}
	}
	for _, req := range []*dispatcherpb.ListSessionDispatchesRequest{
		{SessionId: id, Limit: 101},
		{SessionId: id, PageToken: "invalid"},
		{SessionId: "other-session", PageToken: first.NextPageToken},
	} {
		if _, err := peers.component.ListSessionDispatches(authoritySVIDContext(t), req); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("invalid page request %v: %v", req, err)
		}
	}
	initial := page("", 3)
	if len(initial.Dispatches) != 3 {
		t.Fatalf("initial receipt page: %v", initial)
	}
	oldVersion := initial.Dispatches[1].GetReceiptVersion()
	if oldVersion <= 0 || initial.Dispatches[1].GetCommandId() != "test-second" {
		t.Fatalf("initial receipt version: %v", initial)
	}
	point, err := peers.component.GetDispatch(authoritySVIDContext(t), &dispatcherpb.GetDispatchRequest{CommandId: "test-second"})
	if err != nil || point.GetDispatch() == nil || point.GetDispatch().ReceiptVersion != nil {
		t.Fatalf("point receipt version presence: %v, %v", point, err)
	}
	enqueue("test-later")
	reason := strings.Repeat("test-reason-", 100)
	if _, err := queueStore.Settle(ctx, "test-second", claimSessionOwner(t, queueStore, id), []string{store.QueueState}, store.FailedState, reason, nil); err != nil {
		t.Fatal(err)
	}
	last := page(first.NextPageToken, 2)
	if len(last.Dispatches) != 2 || last.Dispatches[0].GetCommandId() != "test-second" || last.Dispatches[0].GetReason() != reason || last.Dispatches[0].GetReceiptVersion() <= oldVersion ||
		last.Dispatches[1].GetCommandId() != "test-first" || last.NextPageToken != "" || last.Coverage != dispatcherpb.ListSessionDispatchesResponse_RETAINED_DISPATCH_ROWS {
		t.Fatalf("continued after admission and receipt update: %v", last)
	}
	if _, err := queueStore.DB.ExecContext(ctx, "DELETE FROM dispatch WHERE command_id='test-first'"); err != nil {
		t.Fatal(err)
	}
	if _, err := queueStore.DB.ExecContext(ctx, "UPDATE dispatch_tombstone SET deleted_at=now()-interval '2 days'"); err != nil {
		t.Fatal(err)
	}
	if count, err := queueStore.PruneDispatchTombstones(ctx); err != nil || count != 1 {
		t.Fatalf("prune: %d, %v", count, err)
	}
	if _, err := peers.component.ListSessionDispatches(authoritySVIDContext(t), &dispatcherpb.ListSessionDispatchesRequest{SessionId: id, PageToken: first.NextPageToken}); status.Code(err) != codes.OutOfRange {
		t.Fatalf("continued across pruned deletion: %v", err)
	}
	fresh := page("", 0)
	if len(fresh.Dispatches) != 3 || fresh.Dispatches[0].GetCommandId() != "test-later" || fresh.NextPageToken != "" || fresh.Coverage != dispatcherpb.ListSessionDispatchesResponse_RETAINED_DISPATCH_ROWS {
		t.Fatalf("fresh retained-row enumeration: %v", fresh)
	}
}

func TestBatchGetDispatchesRetainsOriginalsAcrossEnvironmentDeletionAndLimitsResponseBytes(t *testing.T) {
	ctx := t.Context()
	queueStore := testStore(t, 5)
	id, uid := uuid.NewString(), uuid.NewString()
	text := strings.Repeat("x", 2<<20)
	for _, cmd := range []string{"one", "two"} {
		if _, err := enqueueWithSession(t, queueStore, ctx, id, "test-batch-env", uid, input(cmd, text), nil); err != nil {
			t.Fatal(err)
		}
	}
	if err := queueStore.EndSandbox(ctx, uid); err != nil {
		t.Fatal(err)
	}
	peers := authorityClients(t, queueStore, &authorityInventory{sandboxErr: errors.New("inventory down")}, nil)
	req := &dispatcherpb.BatchGetDispatchesRequest{SessionId: id, CommandIds: []string{"one", "two"}}
	if _, err := peers.component.BatchGetDispatches(authoritySVIDContext(t), req); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("batch byte limit: %v", err)
	}
	req.CommandIds = []string{"two"}
	result, err := peers.component.BatchGetDispatches(authoritySVIDContext(t), req)
	if err != nil || len(result.GetResults()) != 1 {
		t.Fatalf("retained command read: %v", err)
	}
	row := result.Results[0].GetDispatch()
	if row.GetSessionId() != id || row.GetSandboxUid() != uid || row.GetCommand().GetSubmitInput().GetText() != text || row.GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_FAILED {
		t.Fatal("did not recover the original and final delivery outcome")
	}
}
