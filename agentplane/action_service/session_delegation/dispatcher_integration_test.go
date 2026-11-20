package sessiondelegation

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/session_delegation/contract"
	"github.com/agentydragon/ducktape/agentplane/environments/access"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadgrpc "github.com/agentydragon/ducktape/agentplane/identity/auth/grpc"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
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

type identitySandboxes map[string]identitySandbox

func (s identitySandboxes) GetSandbox(_ context.Context, name string) (dispatcher.SandboxSnapshot, bool, error) {
	target, found := s[name]
	return target.sandbox, found, nil
}

func (s identitySandboxes) GetEnvironment(_ context.Context, target dispatcher.SandboxSnapshot) (*sandboxapi.Sandbox, bool, error) {
	item, found := s[target.Name]
	if !found || target.Namespace != item.sandbox.Namespace || target.UID != item.sandbox.UID {
		return nil, false, nil
	}
	return item.environment, item.environment != nil, nil
}

func (s identitySandboxes) GetExecutionIdentity(_ context.Context, target dispatcher.SandboxSnapshot) (*identitypb.Identity, bool, error) {
	item, found := s[target.Name]
	if !found || target.UID != item.sandbox.UID {
		return nil, false, nil
	}
	return item.identity, true, nil
}

// recordingDispatcher observes the authenticated boundary, after the shared middleware has
// verified the service credential and decoded the executor's Identity and Action metadata.
type recordingDispatcher struct {
	*dispatcher.Service
	submissions chan auth.Admission
}

func (s *recordingDispatcher) SubmitInput(ctx context.Context, request *dispatcherpb.SubmitInputRequest) (*dispatcherpb.SubmitInputResponse, error) {
	admitted, err := auth.Admitted(ctx)
	if err != nil {
		return nil, err
	}
	s.submissions <- admitted
	return s.Service.SubmitInput(ctx, request)
}

// An Identity's Session-delegation Actions run through this executor against a real Dispatcher,
// which authorizes them as that Identity: its own Session's input is queued, recorded as the
// Action Service acting for it, and cancellable; another Identity's Session and opening a
// Session are refused.
func TestAnIdentitysSessionActionsSucceedAgainstTheDispatcher(t *testing.T) {
	api := kubeapiserver.StartForTest(t)
	const policyNamespace = "test-delegation-policies"
	owner := workloadauthtesting.NewIdentityWorkload(t, api, integrationNamespace, "test-delegating-identity", policyNamespace)
	ownerIdentity := &identitypb.Identity{Name: owner.Identity.Name, Uid: string(owner.Identity.UID)}
	other := workloadauthtesting.NewIdentityWorkload(t, api, integrationNamespace, "test-other-identity", policyNamespace)
	inventory := identitySandboxes{}
	environmentRules := access.NewIndex()
	for _, workload := range []workloadauthtesting.IdentityWorkload{owner, other} {
		sandbox := dispatcher.SandboxSnapshot{Name: workload.Sandbox.Name, Namespace: workload.Sandbox.Namespace, UID: string(workload.Sandbox.UID)}
		identity := &identitypb.Identity{Name: workload.Identity.Name, Uid: string(workload.Identity.UID)}
		environment := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{
			Namespace: sandbox.Namespace, Name: sandbox.Name, UID: types.UID(sandbox.UID),
		}, Spec: sandboxapi.SandboxSpec{Identity: &sandboxapi.IdentityRef{Name: identity.GetName()}, Running: true}}
		inventory[sandbox.Name] = identitySandbox{sandbox: sandbox, environment: environment, identity: identity}
		environmentRules.PutIdentity(workload.Identity)
	}
	sandbox := inventory[owner.Sandbox.Name].sandbox
	foreignSandbox := inventory[other.Sandbox.Name].sandbox
	identities, err := auth.KubernetesIdentityUIDs(owner.Objects, policyNamespace)
	if err != nil {
		t.Fatal(err)
	}
	var methods []string
	for method := range dispatcher.Methods(auth.Rule{}) {
		methods = append(methods, method)
	}
	rule, err := auth.Services([]string{peertls.ActionsURI, peertls.AppURI})
	if err != nil {
		t.Fatal(err)
	}
	served, err := dispatcher.Methods(rule).WithServiceDelegation(methods, []auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}}, identities)
	if err != nil {
		t.Fatal(err)
	}
	service := dispatcher.NewService(postgres.Open(t, dispatcherDatabase.Clone(t)), 10)
	service.DispatcherServices = sets.New(peertls.ActionsURI, peertls.AppURI)
	service.WatchLease = 10 * time.Second
	service.Sandboxes = inventory
	service.EnvironmentRules = environmentRules
	service.PolicyReady = func() error { return nil }
	recording := &recordingDispatcher{Service: service, submissions: make(chan auth.Admission, 2)}
	ca := transporttesting.NewCA(t)
	serverTLS, err := ca.Issue(t, peertls.SessionDispatcherURI).WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(served.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	dispatcherpb.RegisterSessionDispatcherServer(server, recording)
	serverAddr := grpcserver.Serve(t, server)
	transport := func(serviceURI string) *peertls.ClientTLS {
		t.Helper()
		leaf := ca.Issue(t, serviceURI)
		return &peertls.ClientTLS{
			CAFile: leaf.CAFile, ServerURI: peertls.SessionDispatcherURI,
			CertFile: leaf.CertFile, KeyFile: leaf.KeyFile, ClientURI: serviceURI,
		}
	}
	connection, err := workloadgrpc.DialDelegated(serverAddr, "", 10*time.Second, transport(peertls.ActionsURI), workloadgrpc.ClientInterceptors{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	appConnection, err := bearer.Dial(serverAddr, "", 10*time.Second, transport(peertls.AppURI))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = appConnection.Close() })
	app := dispatcherpb.NewSessionDispatcherClient(appConnection)
	executor, err := NewExecutor(contract.GroupSessions, catalog.SessionDelegationBinding{Description: "forwards caller"},
		environmentpb.NewEnvironmentServiceClient(connection), dispatcherpb.NewSessionDispatcherClient(connection),
		historypb.NewHistoryServiceClient(connection))
	if err != nil {
		t.Fatal(err)
	}

	seed := func(target dispatcher.SandboxSnapshot) string {
		t.Helper()
		sessionID := uuid.NewString()
		spec := &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
			Environment: &sessionpb.ExactEnvironment{Name: target.Name, Uid: target.UID}, Harness: sessionpb.Harness_HARNESS_CODEX,
		}}}}
		created, err := app.CreateSession(t.Context(), &dispatcherpb.CreateSessionRequest{SessionId: sessionID, Spec: spec})
		if err != nil {
			t.Fatalf("seed Session through Dispatcher: %v", err)
		}
		if created.GetSession().GetIdentity().GetUid() != inventory[target.Name].identity.GetUid() {
			t.Fatalf("seed Session executed as %v", created.GetSession().GetIdentity())
		}
		return sessionID
	}
	owned := seed(sandbox)
	foreign := seed(foreignSandbox)

	caller := store.IdentityCaller{Identity: store.Identity{Name: ownerIdentity.GetName(), UID: ownerIdentity.GetUid()}}
	run := func(action string, args map[string]any) (store.ExecutionOutcome, store.ExecutionRequest) {
		t.Helper()
		encoded, err := json.Marshal(args)
		if err != nil {
			t.Fatal(err)
		}
		request := store.ExecutionRequest{
			RequestID: uuid.New(), ExecutionID: uuid.New(), Arguments: encoded, Caller: caller,
			Action: store.ActionIdentity{Group: contract.GroupSessions, Name: action},
		}
		outcome, err := executor.attempt(t.Context(), request)
		if err != nil {
			t.Fatalf("%s: %v", action, err)
		}
		return outcome, request
	}
	target := map[string]string{"namespace": sandbox.Namespace, "name": sandbox.Name, "uid": sandbox.UID}
	command := uuid.NewString()

	submitted, request := run(contract.ActionSubmitInput, map[string]any{
		"target": target, "session_id": owned, "command_id": command, "text": "Do the requested work.",
	})
	if submitted.State != store.ExecutionSucceeded {
		t.Fatalf("submit_input for the Identity's Session: %+v", submitted)
	}
	received, ok := (<-recording.submissions).(auth.DelegatedServiceIdentityAdmission)
	if !ok || received.Actor.SPIFFEID != peertls.ActionsURI || received.EffectiveCaller.Name != ownerIdentity.GetName() || received.EffectiveCaller.UID != ownerIdentity.GetUid() ||
		received.Provenance.GetAction().GetRequestId() != request.RequestID.String() || received.Provenance.GetAction().GetExecutionId() != request.ExecutionID.String() {
		t.Fatalf("Dispatcher received admission %#v, want Actions acting for %v in request %v execution %v", received, ownerIdentity, request.RequestID, request.ExecutionID)
	}
	queued, err := app.GetDispatch(t.Context(), &dispatcherpb.GetDispatchRequest{CommandId: command})
	if err != nil || queued.GetDispatch().GetSessionId() != owned || queued.GetDispatch().GetState() != dispatcherpb.DeliveryState_DELIVERY_STATE_QUEUED_IN_DISPATCHER {
		t.Fatalf("queued input receipt: %v %v", queued, err)
	}

	if refused, _ := run(contract.ActionSubmitInput, map[string]any{
		"target": map[string]string{"namespace": foreignSandbox.Namespace, "name": foreignSandbox.Name, "uid": foreignSandbox.UID}, "session_id": foreign, "command_id": uuid.NewString(), "text": "foreign",
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
