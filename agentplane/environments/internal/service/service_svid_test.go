package service

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/internal/resume"
	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
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
	lifecycleManager, err := resume.NewManager(lifecycle.Options{
		Namespace: discoveryNamespace, Objects: objects, Identities: kubernetesIdentities(t, objects, discoveryNamespace),
		OperatorServiceCallers: []string{operatorID},
	})
	if err != nil {
		t.Fatal(err)
	}
	service, err := New(discoveryNamespace, objects, kubernetesIdentities(t, objects, discoveryNamespace), runtimeconfig.Catalog{}, lifecycleManager, Options{
		ServiceReaders: sets.New(readerID), ServiceManagers: sets.New(managerID), ServiceLaunchers: sets.New(launcherID),
	})
	if err != nil {
		t.Fatal(err)
	}
	methods, err := WithServiceCallers(Methods(nil), []string{readerID}, []string{managerID}, []string{launcherID}, []string{operatorID})
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.ServeTLSWithTimeout(t, time.Second, peertls.SessionDispatcherURI, methods, func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, service)
	}, readerID, managerID, operatorID, launcherID)

	dial := func(serviceID string) environmentpb.EnvironmentServiceClient {
		t.Helper()
		return environmentpb.NewEnvironmentServiceClient(grpcmethods.AsCaller(connection, serviceID))
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
	lifecycleManager, err := resume.NewManager(lifecycle.Options{
		Namespace: discoveryNamespace, Objects: objects, Identities: kubernetesIdentities(t, objects, discoveryNamespace), OperatorServiceCallers: []string{actionsID},
	})
	if err != nil {
		t.Fatal(err)
	}
	service, err := New(discoveryNamespace, objects, kubernetesIdentities(t, objects, discoveryNamespace), runtimeconfig.Catalog{}, lifecycleManager, Options{
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
	connection := grpcmethods.ServeTLSWithTimeout(t, time.Second, peertls.SessionDispatcherURI, methods, func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, service)
	}, actionsID)
	client := environmentpb.NewEnvironmentServiceClient(grpcmethods.AsCaller(connection, actionsID))
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}

	direct, err := client.ListEnvironments(t.Context(), &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil || len(direct.GetItems()) != 2 {
		t.Fatalf("direct Actions component reader = %v, %v; want both Environments", direct, err)
	}
	delegated := grpcmethods.WithActionDelegation(t, t.Context(), auth.Identity{Name: identityName, UID: identityUID})
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
	stale := grpcmethods.WithActionDelegation(t, t.Context(), auth.Identity{Name: identityName, UID: "agent-uid-replaced"})
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
