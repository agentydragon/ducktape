package sessiondelegation

import (
	"context"
	"encoding/json"
	"net"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/session_delegation/contract"
	"github.com/agentydragon/ducktape/agentplane/environments/access"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadgrpc "github.com/agentydragon/ducktape/agentplane/identity/auth/grpc"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
)

const integrationNamespace = "test-delegation-sandboxes"

// identitySandbox is one live Sandbox naming an Identity.
type identitySandbox struct {
	sandbox     dispatcher.SandboxSnapshot
	environment *sandboxapi.Sandbox
	identity    *identitypb.Identity
}

func (s identitySandbox) GetSandbox(_ context.Context, name string) (dispatcher.SandboxSnapshot, bool, error) {
	return s.sandbox, name == s.sandbox.Name, nil
}

func (s identitySandbox) GetEnvironment(_ context.Context, target dispatcher.SandboxSnapshot) (*sandboxapi.Sandbox, bool, error) {
	if target.Name != s.sandbox.Name || target.Namespace != s.sandbox.Namespace || target.UID != s.sandbox.UID {
		return nil, false, nil
	}
	return s.environment, s.environment != nil, nil
}

func (s identitySandbox) GetExecutionIdentity(context.Context, dispatcher.SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return s.identity, true, nil
}

// An Identity's Session-delegation Actions run through this executor against a real Dispatcher,
// which authorizes them as that Identity: its own Session's input is queued, recorded as the
// Action Service acting for it, and cancellable; another Identity's Session and opening a
// Session are refused.
func TestAnIdentitysSessionActionsSucceedAgainstTheDispatcher(t *testing.T) {
	api, err := kubeapiserver.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := api.Stop(); err != nil {
			t.Error(err)
		}
	})
	const policyNamespace = "test-delegation-policies"
	owner := workloadauthtesting.NewIdentityWorkload(t, api, integrationNamespace, "test-delegating-identity", policyNamespace)
	ownerIdentity := &identitypb.Identity{Name: owner.Identity.Name, Uid: string(owner.Identity.UID)}
	sandbox := dispatcher.SandboxSnapshot{Name: owner.Sandbox.Name, Namespace: owner.Sandbox.Namespace, UID: string(owner.Sandbox.UID)}
	environment := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
		Namespace: sandbox.Namespace, Name: sandbox.Name, UID: types.UID(sandbox.UID),
	}, Spec: sandboxapi.SandboxSpec{Identity: &sandboxapi.IdentityRef{Name: ownerIdentity.GetName()}, Running: true}}
	environmentRules := access.NewIndex()
	environmentRules.PutIdentity(owner.Identity)
	identities, err := auth.KubernetesIdentityUIDs(owner.Objects, policyNamespace)
	if err != nil {
		t.Fatal(err)
	}
	var methods []string
	for method := range dispatcher.Methods(auth.Rule{}) {
		methods = append(methods, method)
	}
	rule, err := auth.Services([]string{peertls.ActionsURI})
	if err != nil {
		t.Fatal(err)
	}
	served, err := dispatcher.Methods(rule).WithServiceDelegation(methods, []auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}}, identities)
	if err != nil {
		t.Fatal(err)
	}
	sessions := dispatcher.NewStore(postgres.Open(t, dispatcherDatabase.Clone(t)), 10)
	ca := transporttesting.NewCA(t)
	serverTLS, err := ca.Issue(t, peertls.SessionDispatcherURI).WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(served.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	dispatcherpb.RegisterSessionDispatcherServer(server, &dispatcher.Service{
		Store: sessions, DispatcherServices: sets.New(peertls.ActionsURI), WatchLease: 10 * time.Second,
		Sandboxes: identitySandbox{sandbox: sandbox, environment: environment, identity: ownerIdentity}, EnvironmentRules: environmentRules,
		PolicyReady: func() error { return nil },
	})
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	leaf := ca.Issue(t, peertls.ActionsURI)
	clientTLS := &peertls.ClientTLS{
		CAFile: leaf.CAFile, ServerURI: peertls.SessionDispatcherURI,
		CertFile: leaf.CertFile, KeyFile: leaf.KeyFile, ClientURI: peertls.ActionsURI,
	}
	connection, err := workloadgrpc.DialDelegated(listener.Addr().String(), "", 10*time.Second, clientTLS, workloadgrpc.ClientInterceptors{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	executor, err := NewExecutor(contract.GroupSessions, catalog.SessionDelegationBinding{Description: "forwards caller"},
		environmentpb.NewEnvironmentServiceClient(connection), dispatcherpb.NewSessionDispatcherClient(connection),
		historypb.NewHistoryServiceClient(connection))
	if err != nil {
		t.Fatal(err)
	}

	spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
		Environment: &sessionpb.ExactEnvironment{Name: sandbox.Name, Uid: sandbox.UID}, Harness: sessionpb.Harness_HARNESS_CODEX,
	}}}}
	seed := func(identity *identitypb.Identity) string {
		sessionID := uuid.NewString()
		if _, err := sessions.CreateSessionWithPlacementForCaller(t.Context(), sessionID, sandbox.Name, sandbox.UID,
			spec, nil, nil, dispatcher.SessionExecution{Identity: identity},
			dispatcher.IdentityCaller{Identity: identity, Proof: dispatcher.IdentitySVID{}}); err != nil {
			t.Fatalf("seed Session: %v", err)
		}
		return sessionID
	}
	owned := seed(ownerIdentity)
	foreign := seed(&identitypb.Identity{Name: "test-other-identity", Uid: uuid.NewString()})

	caller := store.IdentityCaller{Identity: store.Identity{Name: ownerIdentity.GetName(), UID: ownerIdentity.GetUid()}}
	run := func(action string, args map[string]any) (store.ExecutionOutcome, uuid.UUID) {
		t.Helper()
		encoded, err := json.Marshal(args)
		if err != nil {
			t.Fatal(err)
		}
		requestID := uuid.New()
		outcome, err := executor.attempt(t.Context(), store.ExecutionRequest{
			RequestID: requestID, ExecutionID: uuid.New(), Arguments: encoded, Caller: caller,
			Action: store.ActionIdentity{Group: contract.GroupSessions, Name: action},
		})
		if err != nil {
			t.Fatalf("%s: %v", action, err)
		}
		return outcome, requestID
	}
	target := map[string]string{"namespace": sandbox.Namespace, "name": sandbox.Name, "uid": sandbox.UID}
	command := uuid.NewString()

	submitted, requestID := run(contract.ActionSubmitInput, map[string]any{
		"target": target, "session_id": owned, "command_id": command, "text": "Do the requested work.",
	})
	if submitted.State != store.ExecutionSucceeded {
		t.Fatalf("submit_input for the Identity's Session: %+v", submitted)
	}
	queued, err := sessions.GetDispatch(t.Context(), command)
	if err != nil {
		t.Fatal(err)
	}
	recorded, ok := queued.Attribution.(*dispatcher.ServiceAttribution)
	if !ok || recorded.Identity.GetUid() != ownerIdentity.GetUid() || recorded.ActorSPIFFEID != peertls.ActionsURI || recorded.Action == nil ||
		recorded.Action.RequestID != requestID.String() {
		t.Fatalf("queued input attributed to %#v, want the Action Service acting for %v in request %v", queued.Attribution, ownerIdentity, requestID)
	}

	if refused, _ := run(contract.ActionSubmitInput, map[string]any{
		"target": target, "session_id": foreign, "command_id": uuid.NewString(), "text": "foreign",
	}); refused.State != store.ExecutionFailed || refused.Error.GetKind() != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_TARGET_NOT_FOUND {
		t.Fatalf("submit_input for another Identity's Session: %+v", refused)
	}
	if refused, _ := run(contract.ActionCreateSession, map[string]any{
		"target": target, "session_id": uuid.NewString(),
		"spec": map[string]any{"harness": "HARNESS_CODEX", "model": "test-model", "cwd": "/workspace"},
	}); refused.State != store.ExecutionFailed || refused.Error.GetKind() != action_servicepb.ExecutionErrorKind_EXECUTION_ERROR_KIND_BACKEND_REFUSED {
		t.Fatalf("create for an Identity: %+v", refused)
	}
	if cancelled, _ := run(contract.ActionCancelInput, map[string]any{"session_id": owned, "command_id": command}); cancelled.State != store.ExecutionSucceeded {
		t.Fatalf("cancel_input of the Identity's own input: %+v", cancelled)
	}
}
