package environments

import (
	"context"
	"encoding/json"
	"net"
	"testing"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
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

type serviceCreateWithUIDClient struct{ client.WithWatch }

func (c serviceCreateWithUIDClient) Create(ctx context.Context, object client.Object, options ...client.CreateOption) error {
	if sandbox, ok := object.(*sandboxapi.Sandbox); ok && sandbox.UID == "" {
		sandbox.UID = types.UID("created-" + sandbox.Name + "-uid")
	}
	return c.WithWatch.Create(ctx, object, options...)
}

type serviceRoleWakeBudget struct {
	caller lifecycle.WakeBudgetKey
}

func (b *serviceRoleWakeBudget) ReserveWakeAttempt(_ context.Context, caller lifecycle.WakeBudgetKey, _ string, _ int64) (time.Duration, error) {
	b.caller = caller
	return 0, nil
}

func TestEnvironmentServiceServiceRolesStayMethodScoped(t *testing.T) {
	const (
		readerID   = peertls.CentralSamplerURI
		managerID  = peertls.SessionsURI
		operatorID = peertls.ActionsURI
	)
	sandbox := sandboxObject(sandboxUID)
	sandbox.Spec.WakeGeneration = 3
	sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	sandbox.Status.StopStage = sandboxapi.StopStageStopped
	idle := sandboxapi.SuspendedByIdle
	sandbox.Status.SuspendedBy = &idle
	sandbox.Status.SuspendedAtWakeGeneration = proto.Int64(3)
	objects := serviceCreateWithUIDClient{WithWatch: readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID), &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: "agent", UID: "agent-uid"},
	})}
	budget := &serviceRoleWakeBudget{}
	lifecycleManager, err := lifecycle.NewManager(lifecycle.Options{
		Namespace: discoveryNamespace, Objects: objects, WakeBudget: budget,
		OperatorServiceCallers: []string{operatorID},
	})
	if err != nil {
		t.Fatal(err)
	}
	service, err := New(discoveryNamespace, objects, runtimeconfig.Catalog{}, lifecycleManager, nil, ManagementOptions{
		ServiceReaders: sets.New(readerID), ServiceManagers: sets.New(managerID), ServiceLaunchers: sets.New(launcherID),
	})
	if err != nil {
		t.Fatal(err)
	}
	methods, err := WithServiceCallers(Methods(nil), []string{readerID}, []string{managerID}, []string{launcherID}, []string{operatorID})
	if err != nil {
		t.Fatal(err)
	}
	ca := peertlstesting.NewCA(t)
	serverFiles := ca.Issue(t, peertls.SessionDispatcherURI)
	serverTLS, err := serverFiles.WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	environmentpb.RegisterEnvironmentServiceServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)

	dial := func(serviceID string) environmentpb.EnvironmentServiceClient {
		t.Helper()
		files := ca.Issue(t, serviceID)
		clientTLS, err := files.ClientTLSConfig(peertls.SessionDispatcherURI)
		if err != nil {
			t.Fatal(err)
		}
		connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = connection.Close() })
		return environmentpb.NewEnvironmentServiceClient(connection)
	}
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	reader := dial(readerID)
	listed, err := reader.ListEnvironments(t.Context(), &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil || len(listed.GetItems()) != 1 {
		t.Fatalf("service reader list = %v, %v", listed, err)
	}
	if _, err := reader.SetEnvironmentWakeOnSend(t.Context(), &environmentpb.SetEnvironmentWakeOnSendRequest{Target: target, WakeOnSend: true}); status.Code(err) == 0 {
		t.Fatal("service reader mutated Sandbox wake policy")
	}
	manager := dial(managerID)
	if _, err := manager.ListEnvironments(t.Context(), &environmentpb.ListEnvironmentsRequest{Limit: 10}); status.Code(err) == 0 {
		t.Fatal("service manager inherited the reader role")
	}
	if _, err := manager.SetEnvironmentWakeOnSend(t.Context(), &environmentpb.SetEnvironmentWakeOnSendRequest{Target: target, WakeOnSend: true}); err != nil {
		t.Fatalf("service manager could not change wake policy: %v", err)
	}
	launcher := dial(launcherID)
	if _, err := launcher.SetEnvironmentWakeOnSend(t.Context(), &environmentpb.SetEnvironmentWakeOnSendRequest{Target: target, WakeOnSend: true}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("service launcher acquired manager authority: %v", err)
	}
	launchRequest := &environmentpb.CreateEnvironmentRequest{
		Name: "service-created", Template: "runner-vm", Identity: "agent",
		LaunchOrigin: &environmentpb.EnvironmentLaunchOrigin{
			CallerIdentity: &identitypb.Identity{Name: "agent", Uid: "agent-uid"}, ActionGroup: "group-a",
		},
	}
	created, err := launcher.CreateEnvironment(t.Context(), launchRequest)
	if err != nil || created.GetUid() != "created-service-created-uid" {
		t.Fatalf("service launcher create = %v, %v", created, err)
	}
	var createdSandbox sandboxapi.Sandbox
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: "service-created"}, &createdSandbox); err != nil {
		t.Fatal(err)
	}
	var receipt struct {
		Actor struct {
			Kind            string `json:"kind"`
			ServiceSPIFFEID string `json:"service_spiffe_id"`
		} `json:"actor"`
	}
	if err := json.Unmarshal([]byte(createdSandbox.Annotations[sandboxapi.CreateIntentAnnotation]), &receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.Actor.Kind != "service" || receipt.Actor.ServiceSPIFFEID != launcherID {
		t.Fatalf("service create receipt actor = %+v", receipt.Actor)
	}
	retried, err := launcher.CreateEnvironment(t.Context(), launchRequest)
	if err != nil || !proto.Equal(created, retried) {
		t.Fatalf("service launcher retry = %v, %v; want exact original target %v", retried, err, created)
	}
	operator := dial(operatorID)
	response, err := operator.ResumeEnvironment(t.Context(), &environmentpb.ResumeEnvironmentRequest{Target: target})
	if err != nil || response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED {
		t.Fatalf("service operator resume = %v, %v", response, err)
	}
	if budget.caller != (lifecycle.WakeBudgetKey{ServiceSPIFFEID: operatorID}) {
		t.Fatalf("operator wake budget caller = %+v, want SPIFFE identity only", budget.caller)
	}
	var updated sandboxapi.Sandbox
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, &updated); err != nil {
		t.Fatal(err)
	}
	if !updated.Spec.Running || updated.Spec.WakeGeneration != 4 {
		t.Fatalf("service operator did not update desired state: running=%t generation=%d", updated.Spec.Running, updated.Spec.WakeGeneration)
	}
}

const (
	launcherID = peertls.RuntimeProxyURI
	actionsID  = peertls.ActionsURI
)

func serviceDelegationContext(t *testing.T, identity auth.Identity) context.Context {
	t.Helper()
	envelope, err := auth.MarshalDelegation(&delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: identity.Name, Uid: identity.UID}},
		Provenance: &delegationpb.Delegation_Action{Action: &delegationpb.ActionProvenance{
			RequestId: uuid.NewString(), ExecutionId: uuid.NewString(),
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return metadata.NewOutgoingContext(t.Context(), metadata.Pairs(auth.DelegationMetadataKey, string(envelope)))
}

func TestEnvironmentServiceActionDelegationKeepsIdentityScopeAndNoLifecycleAuthority(t *testing.T) {
	const (
		identityName = "agent"
		identityUID  = "agent-uid-current"
		otherName    = "other-agent"
		otherUID     = "other-agent-uid"
	)
	current := sandboxObject(sandboxUID)
	current.Spec.Identity = &sandboxapi.IdentityRef{Name: identityName}
	other := sandboxObject("other-sandbox-uid")
	other.Name = "other-workspace"
	other.Spec.Identity = &sandboxapi.IdentityRef{Name: otherName}
	otherAccount := executionAccountObject(other.UID)
	otherAccount.Name = other.Name
	otherAccount.OwnerReferences[0].Name = other.Name
	objects := readerWithObjects(
		t,
		current, executionAccountObject(current.UID), other, otherAccount,
		&policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: identityName, UID: identityUID}},
		&policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: otherName, UID: otherUID}},
	)
	lifecycleManager, err := lifecycle.NewManager(lifecycle.Options{
		Namespace: discoveryNamespace, Objects: objects, WakeBudget: &serviceRoleWakeBudget{}, OperatorServiceCallers: []string{actionsID},
	})
	if err != nil {
		t.Fatal(err)
	}
	service, err := New(discoveryNamespace, objects, runtimeconfig.Catalog{}, lifecycleManager, nil, ManagementOptions{
		ServiceReaders: sets.New(actionsID),
	})
	if err != nil {
		t.Fatal(err)
	}
	methods, err := WithServiceCallers(Methods(nil), []string{actionsID}, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	identities, err := auth.KubernetesIdentityUIDs(objects, discoveryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	methods, err = methods.WithServiceDelegation([]string{
		environmentpb.EnvironmentService_ListEnvironments_FullMethodName,
		environmentpb.EnvironmentService_GetEnvironment_FullMethodName,
	}, []auth.ServiceDelegator{{SPIFFEID: actionsID}}, identities)
	if err != nil {
		t.Fatal(err)
	}
	ca := peertlstesting.NewCA(t)
	serverFiles := ca.Issue(t, peertls.SessionDispatcherURI)
	serverTLS, err := serverFiles.WorkloadServerTLSConfigWithServices(peertls.SessionDispatcherURI, true)
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(methods.ServerOptions(time.Second), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	environmentpb.RegisterEnvironmentServiceServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	files := ca.Issue(t, actionsID)
	clientTLS, err := files.ClientTLSConfig(peertls.SessionDispatcherURI)
	if err != nil {
		t.Fatal(err)
	}
	connection, err := grpc.NewClient(listener.Addr().String(), grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	client := environmentpb.NewEnvironmentServiceClient(connection)
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}

	direct, err := client.ListEnvironments(t.Context(), &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil || len(direct.GetItems()) != 2 {
		t.Fatalf("direct Actions component reader = %v, %v; want both Environments", direct, err)
	}
	delegated := serviceDelegationContext(t, auth.Identity{Name: identityName, UID: identityUID})
	listed, err := client.ListEnvironments(delegated, &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil || len(listed.GetItems()) != 1 || listed.GetItems()[0].GetTarget().GetName() != sandboxName {
		t.Fatalf("delegated Actions list = %v, %v; want only effective Identity's Environment", listed, err)
	}
	if _, err := client.GetEnvironment(delegated, &environmentpb.GetEnvironmentRequest{Target: target}); err != nil {
		t.Fatalf("delegated Actions GetEnvironment = %v", err)
	}
	foreignTarget := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: other.Name, Uid: string(other.UID)}
	if _, err := client.GetEnvironment(delegated, &environmentpb.GetEnvironmentRequest{Target: foreignTarget}); status.Code(err) != codes.NotFound {
		t.Fatalf("delegated Actions read crossed Identity binding: %v", err)
	}
	stale := serviceDelegationContext(t, auth.Identity{Name: identityName, UID: "agent-uid-replaced"})
	if _, err := client.ListEnvironments(stale, &environmentpb.ListEnvironmentsRequest{Limit: 10}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("stale delegated Identity UID was admitted: %v", err)
	}
	if _, err := client.SetEnvironmentWakeOnSend(delegated, &environmentpb.SetEnvironmentWakeOnSendRequest{Target: target, WakeOnSend: true}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("delegated Action inherited lifecycle manager authority: %v", err)
	}
	if _, err := client.ResumeEnvironment(delegated, &environmentpb.ResumeEnvironmentRequest{Target: target}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("delegated Action inherited operator authority: %v", err)
	}
}
