package dispatcher

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	transporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"
)

const (
	authorityTestAudience = "dispatcher-authority-test"
	authoritySandboxUID   = "00000000-0000-4000-8000-000000009013"
	pendingSandboxUID     = "00000000-0000-4000-8000-000000009014"
)

type reviewedToken struct {
	account auth.Account
	uid     string
	pod     bool
}

type tokenReviewStub map[string]reviewedToken

func (r tokenReviewStub) Create(_ context.Context, request *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	identity, found := r[request.Spec.Token]
	if !found {
		return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{}}, nil
	}
	extra := map[string]authenticationv1.ExtraValue{}
	if identity.pod {
		extra["authentication.kubernetes.io/pod-name"] = authenticationv1.ExtraValue{"dispatcher-backend"}
		extra["authentication.kubernetes.io/pod-uid"] = authenticationv1.ExtraValue{"dispatcher-backend-uid"}
	}
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     request.Spec.Audiences,
		User: authenticationv1.UserInfo{
			Username: fmt.Sprintf("system:serviceaccount:%s:%s", identity.account.Namespace, identity.account.Name),
			UID:      identity.uid,
			Extra:    extra,
		},
	}}, nil
}

type authorityInventory struct {
	mu              sync.Mutex
	sandbox         SandboxSnapshot
	identity        *identitypb.Identity
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

func (i *authorityInventory) failSandboxRead(err error) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.sandboxErr = err
}

func authorityClient(t *testing.T, store *Store, inventory *authorityInventory, reviews tokenReviewStub, hub *WatchHub) dispatcherpb.SessionDispatcherClient {
	t.Helper()
	resolver, err := auth.NewResolver(reviews, authorityTestAudience, []string{caller.Namespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	service := &Service{
		Store: store, DispatcherCallers: configuredDispatcherCallers(),
		Sandboxes: inventory, Hub: hub, WatchLease: 10 * time.Second,
	}
	connection := grpcmethods.Serve(t, Methods(auth.ReviewedServiceAccount(resolver)), func(server *grpc.Server) {
		dispatcherpb.RegisterSessionDispatcherServer(server, service)
	})
	return dispatcherpb.NewSessionDispatcherClient(connection)
}

func authorityContext(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 8*time.Second)
	t.Cleanup(cancel)
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+token)
}

func authoritySVIDContext(t *testing.T, token string) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 8*time.Second)
	t.Cleanup(cancel)
	return metadata.NewOutgoingContext(ctx, metadata.Pairs(
		"authorization", "Bearer "+token,
		"x-agentplane-subject", "attacker/forged",
	))
}

func authoritySVIDClient(
	t *testing.T,
	address, workloadURI string,
	ca *transporttesting.CA,
	presented environment_runtime_transport.Config,
) dispatcherpb.SessionDispatcherClient {
	t.Helper()
	clientCredential := ca.Issue(t, workloadURI)
	clientTLS, err := clientCredential.ClientTLSConfig(environment_runtime_transport.SessionDispatcherURI)
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

func requireMissingSessionCommands(t *testing.T, client dispatcherpb.SessionDispatcherClient, ctx context.Context, sessionID, sandboxName, sandboxUID string) {
	t.Helper()
	if _, err := client.SubmitInput(ctx, &dispatcherpb.SubmitInputRequest{
		SessionId: sessionID, ExpectedSandboxUid: sandboxUID, CommandId: uuid.NewString(), Text: "unregistered",
	}); status.Code(err) != codes.NotFound {
		t.Fatalf("unregistered Session input: %v", err)
	}
	if _, err := client.InterruptTurn(ctx, &dispatcherpb.InterruptTurnRequest{
		SessionId: sessionID, ExpectedSandboxUid: sandboxUID, CommandId: uuid.NewString(), TurnId: "unregistered-turn",
	}); status.Code(err) != codes.NotFound {
		t.Fatalf("unregistered Session interrupt: %v", err)
	}
}

func TestDispatcherServiceAccountSVIDDoesNotOwnSandboxIdentitySessions(t *testing.T) {
	api, err := kubeapiserver.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := api.Stop(); err != nil {
			t.Error(err)
		}
	})
	owner := workloadauthtesting.NewWorkload(t, api, caller.Namespace, caller.Name)
	foreign := workloadauthtesting.NewWorkload(t, api, caller.Namespace, "test-foreign-caller")
	ownerSandbox := SandboxSnapshot{Name: owner.Sandbox.Name, Namespace: owner.Sandbox.Namespace, UID: string(owner.Sandbox.UID)}
	inventory := &authorityInventory{sandbox: ownerSandbox}
	svids, err := auth.NewSVIDResolver(owner.Objects, []string{caller.Namespace}, &resolver.Identities{PolicyNamespace: caller.Namespace})
	if err != nil {
		t.Fatal(err)
	}
	store := testStore(t, 10)
	hub := &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true}
	service := &Service{
		Store: store, DispatcherCallers: sets.New[auth.Account](),
		Sandboxes: inventory, Hub: hub, WatchLease: 10 * time.Second,
	}
	counted := &countingAuthorityDispatcher{SessionDispatcherServer: service}
	methods := Methods(auth.AnySubject(svids))
	ca := transporttesting.NewCA(t)
	serverCredential := ca.Issue(t, environment_runtime_transport.SessionDispatcherURI)
	serverTLS, err := serverCredential.WorkloadServerTLSConfig(environment_runtime_transport.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS.NextProtos = []string{"h2", "http/1.1"}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	dispatcherpb.RegisterSessionDispatcherServer(server, counted)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)

	validOwnerLeaf := ca.Issue(t, owner.URI)
	invalidLeaves := map[string]environment_runtime_transport.Config{
		"forged":  transporttesting.NewCA(t).Issue(t, owner.URI),
		"expired": ca.IssueWithValidity(t, owner.URI, time.Now().Add(-time.Hour), time.Now().Add(-time.Minute), x509.ExtKeyUsageClientAuth),
	}
	for name, leaf := range invalidLeaves {
		t.Run(name+"_leaf_rejected_before_dispatcher_handler", func(t *testing.T) {
			client := authoritySVIDClient(t, listener.Addr().String(), owner.URI, ca, leaf)
			_, err := client.ListSessions(authoritySVIDContext(t, "owner-bearer"), &dispatcherpb.ListSessionsRequest{})
			if err == nil {
				t.Fatal("invalid workload leaf reached a successful Dispatcher RPC")
			}
			if calls := counted.listCalls.Load(); calls != 0 {
				t.Fatalf("invalid workload leaf reached Dispatcher ListSessions handler %d times", calls)
			}
		})
	}

	ownerClient := authoritySVIDClient(t, listener.Addr().String(), owner.URI, ca, validOwnerLeaf)
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
	if _, err := ownerClient.CreateSession(authoritySVIDContext(t, "foreign-bearer"), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ServiceAccount SVID created a Session: %v, want PermissionDenied", err)
	}
	identity := &identitypb.Identity{Name: "svid-session-owner", Uid: "00000000-0000-4000-8000-00000000a002"}
	if _, err := store.CreateSessionWithPlacementForCaller(t.Context(), sessionID, ownerSandbox.Name, ownerSandbox.UID,
		testPlacement(request.GetSpec(), ownerSandbox.Name, ownerSandbox.UID), request.GetSpec(), nil, nil,
		SessionExecution{Identity: identity}, IdentityCaller{Identity: identity, Proof: IdentitySVID{}}); err != nil {
		t.Fatalf("seed Identity-owned Session: %v", err)
	}
	if _, err := ownerClient.GetSession(authoritySVIDContext(t, "foreign-bearer"), &dispatcherpb.GetSessionRequest{SessionId: sessionID}); status.Code(err) != codes.NotFound {
		t.Fatalf("ServiceAccount SVID read an Identity-owned Session: %v, want NotFound", err)
	}

	foreignClient := authoritySVIDClient(t, listener.Addr().String(), foreign.URI, ca, ca.Issue(t, foreign.URI))
	if _, err := foreignClient.GetSession(authoritySVIDContext(t, "owner-bearer"), &dispatcherpb.GetSessionRequest{SessionId: sessionID}); status.Code(err) != codes.NotFound {
		t.Fatalf("foreign ServiceAccount SVID read an Identity-owned Session: %v, want NotFound", err)
	}
}

func TestDispatcherIdentitySVIDDoesNotInheritSameNamedAccountAuthority(t *testing.T) {
	api, err := kubeapiserver.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := api.Stop(); err != nil {
			t.Error(err)
		}
	})
	owner := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, caller.Name, caller.Namespace).Workload
	foreign := workloadauthtesting.NewIdentityWorkload(t, api, caller.Namespace, "test-foreign-caller", caller.Namespace).Workload
	ownerIdentity := &identitypb.Identity{Name: owner.Identity.Name, Uid: string(owner.Identity.UID)}
	ownerSandbox := SandboxSnapshot{Name: owner.Sandbox.Name, Namespace: owner.Sandbox.Namespace, UID: string(owner.Sandbox.UID)}
	inventory := &authorityInventory{sandbox: ownerSandbox, identity: ownerIdentity}
	svids, err := auth.NewSVIDResolver(owner.Objects, []string{caller.Namespace}, &resolver.Identities{PolicyNamespace: caller.Namespace})
	if err != nil {
		t.Fatal(err)
	}
	store := testStore(t, 10)
	hub := &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true}
	service := &Service{
		Store: store, DispatcherCallers: sets.New[auth.Account](),
		Sandboxes: inventory, Hub: hub, WatchLease: 10 * time.Second,
	}
	counted := &countingAuthorityDispatcher{SessionDispatcherServer: service}
	methods := Methods(auth.AnySubject(svids))
	ca := transporttesting.NewCA(t)
	serverCredential := ca.Issue(t, environment_runtime_transport.SessionDispatcherURI)
	serverTLS, err := serverCredential.WorkloadServerTLSConfig(environment_runtime_transport.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	serverTLS.NextProtos = []string{"h2", "http/1.1"}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	dispatcherpb.RegisterSessionDispatcherServer(server, counted)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)

	validOwnerLeaf := ca.Issue(t, owner.URI)
	invalidLeaves := map[string]environment_runtime_transport.Config{
		"forged":  transporttesting.NewCA(t).Issue(t, owner.URI),
		"expired": ca.IssueWithValidity(t, owner.URI, time.Now().Add(-time.Hour), time.Now().Add(-time.Minute), x509.ExtKeyUsageClientAuth),
	}
	for name, leaf := range invalidLeaves {
		t.Run(name+"_leaf_rejected_before_dispatcher_handler", func(t *testing.T) {
			client := authoritySVIDClient(t, listener.Addr().String(), owner.URI, ca, leaf)
			_, err := client.ListSessions(authoritySVIDContext(t, "owner-bearer"), &dispatcherpb.ListSessionsRequest{})
			if err == nil {
				t.Fatal("invalid workload leaf reached a successful Dispatcher RPC")
			}
			if calls := counted.listCalls.Load(); calls != 0 {
				t.Fatalf("invalid workload leaf reached Dispatcher ListSessions handler %d times", calls)
			}
		})
	}

	ownerClient := authoritySVIDClient(t, listener.Addr().String(), owner.URI, ca, validOwnerLeaf)
	sessionID := uuid.NewString()
	_, err = ownerClient.CreateSession(authoritySVIDContext(t, "foreign-bearer"), &dispatcherpb.CreateSessionRequest{
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
	listed, err := ownerClient.ListSessions(authoritySVIDContext(t, "foreign-bearer"), &dispatcherpb.ListSessionsRequest{})
	if err != nil || len(listed.GetSessions()) != 0 {
		t.Fatalf("Identity SVID listed a Session through its same-named ServiceAccount: %v, %v", listed, err)
	}
	if calls := counted.listCalls.Load(); calls != 1 {
		t.Fatalf("valid owner ListSessions handler calls = %d, want 1", calls)
	}

	foreignClient := authoritySVIDClient(t, listener.Addr().String(), foreign.URI, ca, ca.Issue(t, foreign.URI))
	if _, err := foreignClient.GetSession(authoritySVIDContext(t, "owner-bearer"), &dispatcherpb.GetSessionRequest{SessionId: sessionID}); status.Code(err) != codes.NotFound {
		t.Fatalf("foreign Sandbox SVID with owner bearer GetSession = %v, want NotFound", err)
	}
}
func TestRPCIdentityOnlyOwnershipKeepsPodBoundBackendReads(t *testing.T) {
	store := testStore(t, 10)
	ownerAccount := serviceAccountIdentityFor(Account(caller), "owner-uid")
	ownerIdentity := &identitypb.Identity{Name: "authority-owner", Uid: "00000000-0000-4000-8000-00000000a003"}
	inventory := &authorityInventory{
		sandbox:  SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: authoritySandboxUID},
		identity: ownerIdentity,
	}
	hub := &WatchHub{subscribers: map[*watchSubscription]struct{}{}, pending: map[string]*terminalAssembly{}, connected: true}
	client := authorityClient(t, store, inventory, tokenReviewStub{
		"owner-account":   {account: caller, uid: ownerAccount.GetUid()},
		"owner-pod":       {account: caller, uid: ownerAccount.GetUid(), pod: true},
		"foreign-account": {account: auth.Account{Namespace: caller.Namespace, Name: "test-stranger"}, uid: "foreign-uid"},
	}, hub)

	spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
		Environment: &sessionpb.ExactEnvironment{Name: caller.Name, Uid: authoritySandboxUID}, Harness: sessionpb.Harness_HARNESS_CODEX,
	}}}}
	request := &dispatcherpb.CreateSessionRequest{SessionId: uuid.NewString(), Spec: spec}
	if _, err := client.CreateSession(authorityContext(t, "owner-account"), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("account-only ServiceAccount created a Session: %v, want PermissionDenied", err)
	}
	opened, err := client.CreateSession(authorityContext(t, "owner-pod"), request)
	if err != nil || !proto.Equal(opened.GetSession().GetIdentity(), ownerIdentity) {
		t.Fatalf("configured Pod-bound component CreateSession = %v, %v; want the exact Sandbox Identity", opened, err)
	}
	if _, err := client.GetSession(authorityContext(t, "owner-account"), &dispatcherpb.GetSessionRequest{SessionId: request.GetSessionId()}); status.Code(err) != codes.NotFound {
		t.Fatalf("account-only ServiceAccount read Session ownership: %v, want NotFound", err)
	}
	if _, err := client.GetSession(authorityContext(t, "foreign-account"), &dispatcherpb.GetSessionRequest{SessionId: request.GetSessionId()}); status.Code(err) != codes.NotFound {
		t.Fatalf("foreign ServiceAccount read Session: %v, want NotFound", err)
	}
	if _, err := client.GetSession(authorityContext(t, "owner-pod"), &dispatcherpb.GetSessionRequest{SessionId: request.GetSessionId()}); err != nil {
		t.Fatalf("configured Pod-bound component could not read Session: %v", err)
	}
	listed, err := client.ListSessions(authorityContext(t, "owner-pod"), &dispatcherpb.ListSessionsRequest{})
	if err != nil || len(listed.GetSessions()) != 1 || listed.GetSessions()[0].GetSessionId() != request.GetSessionId() {
		t.Fatalf("configured Pod-bound component ListSessions = %v, %v", listed, err)
	}
	ordinary, err := client.ListSessions(authorityContext(t, "owner-account"), &dispatcherpb.ListSessionsRequest{})
	if err != nil || len(ordinary.GetSessions()) != 0 {
		t.Fatalf("account-only ServiceAccount ListSessions = %v, %v; want no owned Sessions", ordinary, err)
	}

	// Historical account-owned rows stay available to configured backend readers without acquiring
	// the Sandbox's current Identity. The old tuple remains in its inert columns for operator recovery.
	legacyID := "authority-legacy-session"
	if _, err := store.CreateSession(t.Context(), legacyID, caller.Name, authoritySandboxUID, &sessionpb.SessionSpec{}, nil, nil); err != nil {
		t.Fatal("seed legacy row:", err)
	}
	if _, err := store.DB.ExecContext(t.Context(), `UPDATE session SET execution_identity_name=NULL,execution_identity_uid=NULL,
		execution_account_namespace=$2,execution_account_name=$3,execution_account_uid=$4 WHERE session_id=$1`,
		legacyID, caller.Namespace, caller.Name, "legacy-service-account-uid"); err != nil {
		t.Fatal("mark retained legacy row:", err)
	}
	if _, err := client.GetSession(authorityContext(t, "owner-account"), &dispatcherpb.GetSessionRequest{SessionId: legacyID}); status.Code(err) != codes.NotFound {
		t.Fatalf("account-only ServiceAccount read legacy Session: %v, want NotFound", err)
	}
	legacyRead, err := client.GetSession(authorityContext(t, "owner-pod"), &dispatcherpb.GetSessionRequest{SessionId: legacyID})
	if err != nil || legacyRead.GetSession().GetIdentity() != nil {
		t.Fatalf("legacy Session was reassigned to the current Identity: %v, %v", legacyRead, err)
	}
	if _, err := client.SubmitInput(authorityContext(t, "owner-pod"), &dispatcherpb.SubmitInputRequest{
		CommandId: "authority-legacy-input", SessionId: legacyID, ExpectedSandboxUid: authoritySandboxUID, Text: "must stay queued nowhere",
	}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("backend mutated legacy Session without an Identity: %v, want FailedPrecondition", err)
	}
	var legacyNamespace, legacyName, legacyUID string
	if err := store.DB.QueryRowContext(t.Context(), `SELECT execution_account_namespace,execution_account_name,execution_account_uid FROM session WHERE session_id=$1`, legacyID).
		Scan(&legacyNamespace, &legacyName, &legacyUID); err != nil || legacyNamespace != caller.Namespace || legacyName != caller.Name || legacyUID != "legacy-service-account-uid" {
		t.Fatalf("legacy ServiceAccount tuple changed: %q/%q/%q, %v", legacyNamespace, legacyName, legacyUID, err)
	}
}

func TestCreateSessionRejectsMissingIdentityEvenWhenSandboxLookupSucceeds(t *testing.T) {
	store := testStore(t, 3)
	inventory := &authorityInventory{
		sandbox:         SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: authoritySandboxUID},
		identityMissing: true,
	}
	client := authorityClient(t, store, inventory, tokenReviewStub{
		"owner-pod": {account: caller, uid: "owner-uid", pod: true},
	}, nil)
	request := &dispatcherpb.CreateSessionRequest{
		SessionId: uuid.NewString(),
		Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
			Environment: &sessionpb.ExactEnvironment{Name: caller.Name, Uid: authoritySandboxUID}, Harness: sessionpb.Harness_HARNESS_CODEX,
		}}}},
	}
	if _, err := client.CreateSession(authorityContext(t, "owner-pod"), request); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("CreateSession with a successful lookup but no Identity = %v, want FailedPrecondition", err)
	}
	if _, err := store.GetSession(t.Context(), request.GetSessionId()); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing-Identity opening was persisted: %v", err)
	}
}

// admissionAttribution is a row's ServiceAccount attribution, nil when it has none.
func admissionAttribution(attribution Attribution) *AdmissionAttribution {
	account, _ := attribution.(*AdmissionAttribution)
	return account
}

func TestLegacySessionWithoutIdentityIsRetainedButNeverDelivered(t *testing.T) {
	ctx := context.Background()
	store := testStore(t, 3)
	snapshot := SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: pendingSandboxUID}
	session, err := store.CreateSession(ctx, "authority-legacy-delivery-session", snapshot.Name, snapshot.UID, &sessionpb.SessionSpec{}, nil, nil)
	if err != nil {
		t.Fatalf("seed retained Session: %v", err)
	}
	if _, err := store.DB.ExecContext(ctx, `UPDATE session SET execution_identity_name=NULL,execution_identity_uid=NULL,
		execution_account_namespace=$2,execution_account_name=$3,execution_account_uid=$4 WHERE session_id=$1`,
		session.SessionID, caller.Namespace, caller.Name, "retained-account-uid"); err != nil {
		t.Fatalf("mark legacy ownership tuple: %v", err)
	}
	command, err := enqueueWithSession(t, store, ctx, session.SessionID, snapshot.Name, snapshot.UID,
		input("authority-legacy-delivery-command", "must remain queued"), nil)
	if err != nil {
		t.Fatalf("seed retained command: %v", err)
	}

	runner := &acceptingRunner{calls: make(chan string, 2)}
	worker := &DeliveryWorker{
		Store: store, Catalog: testCatalog{sandbox: snapshot},
		Leases: testLease{store: store}, Runner: runner,
	}
	worker.init()
	defer worker.queue.ShutDown()
	owner, err := worker.Leases.Acquire(ctx, snapshot.Name, snapshot.UID)
	if err != nil || owner == nil {
		t.Fatalf("acquire delivery owner: %#v %v", owner, err)
	}
	worker.owners[snapshot.UID] = *owner

	if err := worker.processSession(ctx, session.SessionID); err != nil {
		t.Fatalf("process retained Session: %v", err)
	}
	retained, err := store.GetSession(ctx, session.SessionID)
	if err != nil || retained.State != SessionWaitingState || hasExecutionIdentity(retained) {
		t.Fatalf("retained Session was changed or attributed: %+v %v", retained, err)
	}
	pending, err := store.GetDispatch(ctx, command.CommandID)
	if err != nil || pending.State != QueueState || pending.DeliveryAttempted {
		t.Fatalf("retained command was claimed or replayed: %+v %v", pending, err)
	}
	select {
	case call := <-runner.calls:
		t.Fatalf("Runner received legacy Session work: %q", call)
	default:
	}
	var legacyNamespace, legacyName, legacyUID string
	if err := store.DB.QueryRowContext(ctx, `SELECT execution_account_namespace,execution_account_name,execution_account_uid FROM session WHERE session_id=$1`, session.SessionID).
		Scan(&legacyNamespace, &legacyName, &legacyUID); err != nil || legacyNamespace != caller.Namespace || legacyName != caller.Name || legacyUID != "retained-account-uid" {
		t.Fatalf("legacy ServiceAccount tuple changed: %q/%q/%q, %v", legacyNamespace, legacyName, legacyUID, err)
	}
}

func TestSessionRetryReadsDurableReceiptWithoutOwnerInventory(t *testing.T) {
	store := testStore(t, 3)
	identity := &identitypb.Identity{Name: "durable-retry-identity", Uid: "00000000-0000-4000-8000-00000000a005"}
	snapshot := SandboxSnapshot{Name: caller.Name, Namespace: caller.Namespace, UID: uuid.NewString()}
	inventory := &authorityInventory{sandbox: snapshot, identity: identity}
	foreign := Account{Namespace: caller.Namespace, Name: "retry-foreign"}
	client := authorityClient(t, store, inventory, tokenReviewStub{
		"retry-owner":   {account: caller, uid: "ordinary-account-uid"},
		"retry-foreign": {account: foreign, uid: "foreign-uid"},
		"retry-backend": {account: caller, uid: "backend-uid", pod: true},
	}, nil)
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
	if _, err := client.CreateSession(authorityContext(t, "retry-owner"), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ordinary ServiceAccount created a Session: %v, want PermissionDenied", err)
	}
	first, err := client.CreateSession(authorityContext(t, "retry-backend"), request)
	if err != nil || !proto.Equal(first.GetSession().GetIdentity(), identity) {
		t.Fatalf("backend CreateSession = %v, %v; want the retained Identity", first, err)
	}
	stored, err := store.GetSession(t.Context(), request.GetSessionId())
	if err != nil {
		t.Fatal(err)
	}

	// An accepted receipt retry does not need live inventory or rebind the execution Identity.
	inventory.failSandboxRead(errors.New("inventory unavailable after accepted opening"))
	again, err := client.CreateSession(authorityContext(t, "retry-backend"), request)
	if err != nil || !proto.Equal(first, again) {
		t.Fatalf("privileged receipt retry: %v %v", again, err)
	}
	for _, token := range []string{"retry-owner", "retry-foreign"} {
		if _, err := client.CreateSession(authorityContext(t, token), request); status.Code(err) != codes.NotFound {
			t.Fatalf("%s learned a retained receipt: %v", token, err)
		}
	}
	conflict := proto.Clone(request).(*dispatcherpb.CreateSessionRequest)
	conflict.GetSpec().GetPlacement().GetEnvironmentHosted().Environment.Name += "-other"
	if _, err := client.CreateSession(authorityContext(t, "retry-backend"), conflict); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("backend request conflict = %v, want AlreadyExists", err)
	}

	// Reading failed or uncertain materialization never requeues it or changes its evidence.
	for _, state := range []string{SessionFailedState, SessionInProgressState} {
		if _, err := store.DB.ExecContext(t.Context(), "UPDATE session SET state=$2,reason='retained outcome' WHERE session_id=$1", stored.SessionID, state); err != nil {
			t.Fatal(err)
		}
		if _, err := client.CreateSession(authorityContext(t, "retry-backend"), request); err != nil {
			t.Fatal(err)
		}
		after, err := store.GetSession(t.Context(), stored.SessionID)
		if err != nil || after.State != state || after.Reason != "retained outcome" ||
			!after.UpdatedAt.Equal(stored.UpdatedAt) || after.Creator.GetUid() != stored.Creator.GetUid() {
			t.Fatalf("receipt retry changed durable state: %+v %v", after, err)
		}
	}
	inventory.failSandboxRead(nil)
	if _, err := store.EndSandbox(t.Context(), snapshot.UID); err != nil {
		t.Fatal(err)
	}
	retained, err := client.CreateSession(authorityContext(t, "retry-backend"), request)
	if err != nil || retained.GetSession().GetStatus().GetRequiredEnvironment() != dispatcherpb.SessionStatus_REQUIRED_ENVIRONMENT_DELETED ||
		retained.GetSession().GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_UNKNOWN {
		t.Fatalf("retained retry after deletion: %+v %v", retained, err)
	}
	if _, err := client.SubmitInput(authorityContext(t, "retry-backend"), &dispatcherpb.SubmitInputRequest{
		SessionId: stored.SessionID, ExpectedSandboxUid: snapshot.UID, CommandId: uuid.NewString(), Text: "must not send",
	}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("retained Session accepted work for deleted UID: %v", err)
	}
	if row, err := store.GetSession(t.Context(), stored.SessionID); err != nil || row.EnvironmentDeletedAt == nil || row.SandboxUID != snapshot.UID {
		t.Fatalf("retained Session lost its exact placement: %+v %v", row, err)
	}
	if _, err := client.GetSession(authorityContext(t, "retry-owner"), &dispatcherpb.GetSessionRequest{SessionId: stored.SessionID}); status.Code(err) != codes.NotFound {
		t.Fatalf("ordinary ServiceAccount inherited backend Session access: %v", err)
	}
	if got, err := client.GetSession(authorityContext(t, "retry-backend"), &dispatcherpb.GetSessionRequest{SessionId: stored.SessionID}); err != nil ||
		got.GetSession().GetStatus().GetRequiredEnvironment() != dispatcherpb.SessionStatus_REQUIRED_ENVIRONMENT_DELETED {
		t.Fatalf("backend could not inspect retained Session: %+v %v", got, err)
	}
}

func TestSessionTransactionChecksRetainedAuthorityBeforeConflict(t *testing.T) {
	store := testStore(t, 3)
	owner := &identitypb.Identity{Name: "transaction-owner", Uid: "00000000-0000-4000-8000-00000000a006"}
	foreign := &identitypb.Identity{Name: "transaction-foreign", Uid: "00000000-0000-4000-8000-00000000a007"}
	ownerCaller := IdentityCaller{Identity: owner, Proof: IdentitySVID{}}
	foreignCaller := IdentityCaller{Identity: foreign, Proof: IdentitySVID{}}
	id, uid := uuid.NewString(), uuid.NewString()
	spec := &sessionpb.SessionSpec{}
	if _, err := store.CreateSessionWithPlacementForCaller(t.Context(), id, "transaction-sandbox", uid,
		testPlacement(spec, "transaction-sandbox", uid), spec, nil, nil, SessionExecution{Identity: owner}, ownerCaller); err != nil {
		t.Fatal(err)
	}
	// This is the transactional path after another opener wins the Session-ID lock,
	// including when the API's initial read did not yet see that winning opening.
	for _, name := range []string{"transaction-sandbox", "conflicting-sandbox"} {
		conflictingSpec := &sessionpb.SessionSpec{}
		if _, err := store.CreateSessionWithPlacementForCaller(t.Context(), id, name, uid,
			testPlacement(conflictingSpec, name, uid), conflictingSpec, nil, nil, SessionExecution{Identity: foreign}, foreignCaller); !errors.Is(err, ErrNotFound) {
			t.Fatalf("transaction exposed an existing opening to foreign Identity: %v", err)
		}
	}
}

func TestSessionDiscoveryUsesRetainedAuthorityAndStablePages(t *testing.T) {
	store := testStore(t, 3)
	uid := uuid.NewString()
	const queuedID = "00000000-0000-4000-8000-000000000001"
	const foreignID = "00000000-0000-4000-8000-000000000002"
	const failedID = "00000000-0000-4000-8000-000000000003"
	const legacyID = "00000000-0000-4000-8000-000000000004"
	// Insert out of order; ordering is the immutable public ID, never delivery state.
	for _, row := range []struct {
		id      string
		harness sessionpb.Harness
	}{
		{failedID, sessionpb.Harness_HARNESS_CLAUDE},
		{foreignID, sessionpb.Harness_HARNESS_CENTRAL_SAMPLER},
		{queuedID, sessionpb.Harness_HARNESS_CODEX},
		{legacyID, sessionpb.Harness_HARNESS_CODEX},
	} {
		spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "discovery-sandbox", Uid: uid}, Harness: row.harness}}}}
		if row.harness == sessionpb.Harness_HARNESS_CENTRAL_SAMPLER {
			spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: "discovery-sandbox", Uid: uid}}}}
		}
		if _, err := store.CreateSession(t.Context(), row.id, "discovery-sandbox", uid, spec, nil, nil); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := store.DB.ExecContext(t.Context(), "UPDATE session SET execution_identity_name=NULL,execution_identity_uid=NULL, execution_account_namespace=$2,execution_account_name=$3,execution_account_uid=$4 WHERE session_id=$1",
		legacyID, caller.Namespace, caller.Name, "retained-legacy-account-uid"); err != nil {
		t.Fatal(err)
	}
	lease, err := store.ClaimSandboxOwner(t.Context(), uid, "discovery-lease", 1, "discovery-worker", 0)
	if err != nil || lease == nil {
		t.Fatalf("claim: %+v %v", lease, err)
	}
	if row, err := store.SettleSession(t.Context(), failedID, *lease, []string{SessionWaitingState}, SessionFailedState, "runner_rejected"); err != nil || row == nil {
		t.Fatalf("reject opening: %+v %v", row, err)
	}
	inventory := &authorityInventory{sandboxErr: errors.New("sandbox inventory unavailable")}
	client := authorityClient(t, store, inventory, tokenReviewStub{
		"discovery-owner":       {account: caller, uid: "ordinary-account-uid"},
		"discovery-replacement": {account: caller, uid: "replacement-account-uid"},
		"discovery-foreign":     {account: auth.Account{Namespace: caller.Namespace, Name: "discovery-foreign"}, uid: "foreign-account-uid"},
		"discovery-backend":     {account: caller, uid: "backend-account-uid", pod: true},
	}, nil)
	first, err := client.ListSessions(authorityContext(t, "discovery-backend"), &dispatcherpb.ListSessionsRequest{Limit: 1})
	if err != nil || len(first.GetSessions()) != 1 || first.GetSessions()[0].GetSessionId() != queuedID || first.GetNextPageToken() == "" {
		t.Fatalf("backend first page: %v %v", first, err)
	}
	if first.GetSessions()[0].GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_WAITING ||
		first.GetSessions()[0].GetPlacement().GetEnvironmentHosted().GetEnvironment().GetUid() != uid ||
		first.GetSessions()[0].GetPlacement().GetEnvironmentHosted().GetHarness() != sessionpb.Harness_HARNESS_CODEX {
		t.Fatalf("queued opening placement/state: %v", first)
	}
	got, err := client.GetSession(authorityContext(t, "discovery-backend"), &dispatcherpb.GetSessionRequest{SessionId: queuedID})
	if err != nil || got.GetSession().GetPlacement().GetEnvironmentHosted().GetHarness() != sessionpb.Harness_HARNESS_CODEX {
		t.Fatalf("backend opening harness: %v %v", got, err)
	}
	// The last row of a page can disappear between requests without invalidating its cursor.
	if _, err := store.DB.ExecContext(t.Context(), "DELETE FROM session WHERE session_id=$1", queuedID); err != nil {
		t.Fatal(err)
	}
	second, err := client.ListSessions(authorityContext(t, "discovery-backend"), &dispatcherpb.ListSessionsRequest{
		Limit: 1, PageToken: first.GetNextPageToken(),
	})
	if err != nil || len(second.GetSessions()) != 1 || second.GetSessions()[0].GetSessionId() != foreignID ||
		second.GetNextPageToken() == "" || second.GetSessions()[0].GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment().GetUid() != uid {
		t.Fatalf("backend second page: %v %v", second, err)
	}
	third, err := client.ListSessions(authorityContext(t, "discovery-backend"), &dispatcherpb.ListSessionsRequest{
		Limit: 1, PageToken: second.GetNextPageToken(),
	})
	if err != nil || len(third.GetSessions()) != 1 || third.GetSessions()[0].GetSessionId() != failedID ||
		third.GetNextPageToken() == "" || third.GetSessions()[0].GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_FAILED ||
		third.GetSessions()[0].GetPlacement().GetEnvironmentHosted().GetHarness() != sessionpb.Harness_HARNESS_CLAUDE {
		t.Fatalf("backend third page: %v %v", third, err)
	}
	fourth, err := client.ListSessions(authorityContext(t, "discovery-backend"), &dispatcherpb.ListSessionsRequest{
		Limit: 1, PageToken: third.GetNextPageToken(),
	})
	if err != nil || len(fourth.GetSessions()) != 1 || fourth.GetSessions()[0].GetSessionId() != legacyID ||
		fourth.GetSessions()[0].GetIdentity() != nil || fourth.GetNextPageToken() != "" {
		t.Fatalf("backend legacy page: %v %v", fourth, err)
	}
	// A cursor is position only; ordinary ServiceAccounts cannot use a backend cursor as authority.
	for _, token := range []string{"discovery-owner", "discovery-replacement", "discovery-foreign"} {
		page, err := client.ListSessions(authorityContext(t, token), &dispatcherpb.ListSessionsRequest{PageToken: first.GetNextPageToken()})
		if err != nil || len(page.GetSessions()) != 0 || page.GetNextPageToken() != "" {
			t.Fatalf("%s gained backend scope from cursor: %v %v", token, page, err)
		}
	}
	for _, request := range []*dispatcherpb.ListSessionsRequest{
		{Limit: 101}, {PageToken: "not-a-page-token"}, {PageToken: string(make([]byte, 129))},
	} {
		if _, err := client.ListSessions(authorityContext(t, "discovery-backend"), request); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("invalid page request %v: %v", request, err)
		}
	}
}
