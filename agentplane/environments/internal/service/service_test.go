package service

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/internal/resume"
	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadauthtesting "github.com/agentydragon/ducktape/agentplane/identity/auth/testing"
	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/testscheme"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/emptypb"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const (
	discoveryNamespace = "test-envs"
	foreignNamespace   = "other-envs"

	sandboxName  = "workspace"
	sandboxUID   = "sandbox-uid-current"
	executionUID = "service-account-uid-current"

	delegatedIdentityName = "delegated-agent"
	delegatedIdentityUID  = "delegated-identity-uid"
)

// kubernetesIdentities reads namespace's Identities through objects, as the Kubernetes-backed production wiring does.
func kubernetesIdentities(t *testing.T, objects client.Reader, namespace string) auth.IdentityUIDReader {
	t.Helper()
	identities, err := auth.KubernetesIdentityUIDs(objects, namespace)
	if err != nil {
		t.Fatal(err)
	}
	return identities
}

func discoveryLifecycle(t *testing.T, objects client.WithWatch, wakeServiceCallers []string) *resume.Manager {
	t.Helper()
	manager, err := resume.NewManager(lifecycle.Options{
		Namespace: discoveryNamespace, Objects: objects, Identities: kubernetesIdentities(t, objects, discoveryNamespace), WakeServiceCallers: wakeServiceCallers,
	})
	if err != nil {
		t.Fatal(err)
	}
	return manager
}

func serveDiscovery(t *testing.T, objects client.WithWatch, catalog runtimeconfig.Catalog) environmentpb.EnvironmentServiceClient {
	t.Helper()
	return serveDiscoveryWithDelegation(t, objects, catalog, nil)
}

func serveDiscoveryWithDelegation(
	t *testing.T,
	objects client.WithWatch,
	catalog runtimeconfig.Catalog,
	delegatedMethods []string,
) environmentpb.EnvironmentServiceClient {
	return serveDiscoveryWithOptions(t, objects, catalog, delegatedMethods, nil)
}

func fixtureMethods(t *testing.T) auth.Methods {
	t.Helper()
	methods, err := WithServiceCallers(Methods(nil), []string{peertls.ActionsURI, peertls.NotificationsURI}, []string{peertls.ActionsURI}, []string{peertls.RuntimeProxyURI}, nil)
	if err != nil {
		t.Fatal(err)
	}
	return methods
}

func serveFixture(t *testing.T, methods auth.Methods, register func(*grpc.Server)) grpc.ClientConnInterface {
	return grpcmethods.ServeTLS(t, peertls.SandboxControllerURI, methods, register,
		peertls.ActionsURI, peertls.RuntimeProxyURI, peertls.NotificationsURI, peertls.AppURI)
}

func serveDiscoveryWithOptions(t *testing.T, objects client.WithWatch, catalog runtimeconfig.Catalog, delegatedMethods []string, wakeServiceCallers []string) environmentpb.EnvironmentServiceClient {
	t.Helper()
	service, err := New(discoveryNamespace, objects, kubernetesIdentities(t, objects, discoveryNamespace), catalog, discoveryLifecycle(t, objects, wakeServiceCallers), Options{
		ServiceManagers: sets.New(peertls.ActionsURI), ServiceLaunchers: sets.New(peertls.RuntimeProxyURI),
	})
	if err != nil {
		t.Fatal(err)
	}
	methods := fixtureMethods(t)
	if delegatedMethods != nil {
		configureDelegatedIdentity(t, objects, sandboxName, delegatedIdentityName, delegatedIdentityUID)
		identities, err := auth.KubernetesIdentityUIDs(objects, discoveryNamespace)
		if err != nil {
			t.Fatal(err)
		}
		methods, err = methods.WithServiceDelegation(delegatedMethods, []auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}}, identities)
		if err != nil {
			t.Fatal(err)
		}
	}
	return environmentpb.NewEnvironmentServiceClient(serveFixture(t, methods, func(server *grpc.Server) { environmentpb.RegisterEnvironmentServiceServer(server, service) }))
}

func serveDiscoveryWithComponentReader(t *testing.T, objects client.WithWatch, catalog runtimeconfig.Catalog, wakeServiceCallers []string) environmentpb.EnvironmentServiceClient {
	t.Helper()
	service, err := New(discoveryNamespace, objects, kubernetesIdentities(t, objects, discoveryNamespace), catalog, discoveryLifecycle(t, objects, wakeServiceCallers), Options{
		ServiceReaders: sets.New(peertls.ActionsURI), ServiceManagers: sets.New(peertls.ActionsURI), ServiceLaunchers: sets.New(peertls.RuntimeProxyURI),
	})
	if err != nil {
		t.Fatal(err)
	}
	return environmentpb.NewEnvironmentServiceClient(serveFixture(t, fixtureMethods(t), func(server *grpc.Server) { environmentpb.RegisterEnvironmentServiceServer(server, service) }))
}

func configureDelegatedIdentity(t *testing.T, objects client.WithWatch, sandboxName, identityName, identityUID string) auth.Identity {
	t.Helper()
	var sandbox sandboxapi.Sandbox
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, &sandbox); err != nil {
		t.Fatal(err)
	}
	sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: identityName}
	if err := objects.Update(t.Context(), &sandbox); err != nil {
		t.Fatal(err)
	}
	identity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: identityName, UID: types.UID(identityUID)}}
	if err := objects.Create(t.Context(), identity); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatal(err)
	}
	return auth.Identity{Name: identityName, UID: identityUID}
}

func readerWithObjects(t *testing.T, objects ...client.Object) client.WithWatch {
	t.Helper()
	builder := clientfake.NewClientBuilder().WithScheme(testscheme.New(t)).WithObjects(objects...)
	// The fake client implements API field selection through explicit indexes.
	for _, object := range []client.Object{&sandboxapi.Sandbox{}, &corev1.ServiceAccount{}} {
		builder.WithIndex(object, "metadata.name", func(object client.Object) []string { return []string{object.GetName()} })
	}
	return builder.Build()
}

func sandboxObject(uid types.UID) *sandboxapi.Sandbox {
	return &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: sandboxName, UID: uid},
		Spec: sandboxapi.SandboxSpec{
			Template: "runner-vm", Running: true, ResumePolicy: &sandboxapi.ResumePolicy{AllowOwnServiceAccount: true},
		},
		Status: sandboxapi.SandboxStatus{OperatingMode: sandboxapi.OperatingModeRunning},
	}
}

func executionAccountObject(sandboxUID types.UID) *corev1.ServiceAccount {
	controller := true
	return &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: discoveryNamespace,
		Name:      sandboxName,
		UID:       executionUID,
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: sandboxapi.GroupVersion.String(), Kind: "Sandbox", Name: sandboxName,
			UID: sandboxUID, Controller: &controller,
		}},
	}}
}

func baseReader(t *testing.T) client.WithWatch {
	t.Helper()
	sandbox := sandboxObject(sandboxUID)
	return readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID))
}

func TestIdentityChangeRequiresManagerAndCurrentSandbox(t *testing.T) {
	resource := sandboxObject(sandboxUID)
	resource.Spec.Running = false
	resource.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	resource.Status.StopStage = sandboxapi.StopStageStopped
	identity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: "test-agent", UID: "identity-uid"}}
	objects := readerWithObjects(t, resource, executionAccountObject(sandboxUID), identity)
	rpc := serveDiscoveryWithOptions(t, objects, runtimeconfig.Catalog{}, nil, nil)
	request := &environmentpb.SetEnvironmentIdentityRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}, IdentityName: "test-agent",
	}
	if _, err := rpc.SetEnvironmentIdentity(serviceContext(t, peertls.NotificationsURI), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ungranted caller = %v", err)
	}
	stale := proto.Clone(request).(*environmentpb.SetEnvironmentIdentityRequest)
	stale.Target.Uid = "old-sandbox-uid"
	if _, err := rpc.SetEnvironmentIdentity(serviceContext(t, peertls.ActionsURI), stale); status.Code(err) != codes.NotFound {
		t.Fatalf("stale target = %v", err)
	}
	if _, err := rpc.SetEnvironmentIdentity(serviceContext(t, peertls.ActionsURI), request); err != nil {
		t.Fatal(err)
	}
	stored := &sandboxapi.Sandbox{}
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, stored); err != nil {
		t.Fatal(err)
	}
	if stored.Spec.Identity == nil || stored.Spec.Identity.Name != "test-agent" {
		t.Fatalf("saved identity = %v", stored.Spec.Identity)
	}
	request.IdentityName = ""
	if _, err := rpc.SetEnvironmentIdentity(serviceContext(t, peertls.ActionsURI), request); err != nil {
		t.Fatal(err)
	}
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, stored); err != nil || stored.Spec.Identity != nil {
		t.Fatalf("clear identity = %v, %v", stored.Spec.Identity, err)
	}
}

func TestIdentityRecordIsOnlyTheCurrentSandboxOwnedCredential(t *testing.T) {
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	request := &environmentpb.GetEnvironmentRequest{Target: target}
	controller := true
	owner := metav1.OwnerReference{APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind, Name: sandboxName, UID: sandboxUID, Controller: &controller}
	for _, test := range []struct {
		name     string
		record   *workloadapi.WorkloadCredential
		expected string
	}{
		{name: "no retained record"},
		{name: "identity subject", record: &workloadapi.WorkloadCredential{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: sandboxName, OwnerReferences: []metav1.OwnerReference{owner}}, Spec: workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: "review-agent", UID: "identity-uid"}}}}, expected: "review-agent"},
		{name: "legacy subject without Identity", record: &workloadapi.WorkloadCredential{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: sandboxName, OwnerReferences: []metav1.OwnerReference{owner}}, Spec: workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{}}}},
		{name: "prior Sandbox owner", record: &workloadapi.WorkloadCredential{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: sandboxName, OwnerReferences: []metav1.OwnerReference{{APIVersion: owner.APIVersion, Kind: owner.Kind, Name: owner.Name, UID: "old-sandbox-uid"}}}, Spec: workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: "old-agent", UID: "old-identity-uid"}}}}},
		{name: "noncontroller reference", record: &workloadapi.WorkloadCredential{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: sandboxName, OwnerReferences: []metav1.OwnerReference{{APIVersion: owner.APIVersion, Kind: owner.Kind, Name: owner.Name, UID: owner.UID}}}, Spec: workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: "other-agent", UID: "other-identity-uid"}}}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			objects := []client.Object{sandboxObject(sandboxUID), executionAccountObject(sandboxUID)}
			if test.record != nil {
				objects = append(objects, test.record)
			}
			rpc := serveDiscoveryWithComponentReader(t, readerWithObjects(t, objects...), runtimeconfig.Catalog{}, nil)
			got, err := rpc.GetEnvironmentIdentityRecord(serviceContext(t, peertls.ActionsURI), request)
			if err != nil || got.GetIdentity().GetName() != test.expected {
				t.Fatalf("identity record = %v, %v; want %q", got, err, test.expected)
			}
			stale := proto.Clone(request).(*environmentpb.GetEnvironmentRequest)
			stale.Target.Uid = "old-sandbox-uid"
			if _, err := rpc.GetEnvironmentIdentityRecord(serviceContext(t, peertls.ActionsURI), stale); status.Code(err) != codes.NotFound {
				t.Fatalf("stale target = %v", err)
			}
		})
	}
}

func serviceContext(t *testing.T, uri string) context.Context {
	t.Helper()
	return grpcmethods.Caller(t.Context(), uri)
}

func delegatedServiceContext(t *testing.T, uri string, identity auth.Identity) context.Context {
	t.Helper()
	return grpcmethods.WithActionDelegation(t, serviceContext(t, uri), identity)
}

func serveIdentityDiscovery(
	t *testing.T,
	objects client.WithWatch,
	catalog runtimeconfig.Catalog,
	readers sets.Set[string],
) environmentpb.EnvironmentServiceClient {
	t.Helper()
	return serveIdentityDiscoveryWithLifecycle(t, objects, catalog, readers, discoveryLifecycle(t, objects, nil))
}

func serveIdentityDiscoveryWithLifecycle(
	t *testing.T, objects client.WithWatch, catalog runtimeconfig.Catalog,
	readers sets.Set[string], manager *resume.Manager,
) environmentpb.EnvironmentServiceClient {
	t.Helper()
	service, err := New(discoveryNamespace, objects, kubernetesIdentities(t, objects, discoveryNamespace), catalog, manager, Options{ServiceReaders: readers})
	if err != nil {
		t.Fatal(err)
	}
	identities, err := auth.KubernetesIdentityUIDs(objects, discoveryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	methods, err := fixtureMethods(t).WithServiceDelegation(
		[]string{
			environmentpb.EnvironmentService_ListEnvironments_FullMethodName,
			environmentpb.EnvironmentService_GetEnvironment_FullMethodName,
			environmentpb.EnvironmentService_ResumeEnvironment_FullMethodName,
		},
		[]auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}},
		identities,
	)
	if err != nil {
		t.Fatal(err)
	}
	connection := serveFixture(t, methods, func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, service)
	})
	return environmentpb.NewEnvironmentServiceClient(connection)
}

func TestIdentityEnvironmentReadsFollowSandboxBindingWithoutActorPrivileges(t *testing.T) {
	first := sandboxObject(sandboxUID)
	first.Spec.Identity = &sandboxapi.IdentityRef{Name: "agent"}
	second := sandboxObject("other-sandbox-uid")
	second.Name = "other"
	second.Spec.Identity = &sandboxapi.IdentityRef{Name: "other-agent"}
	secondAccount := executionAccountObject(second.UID)
	secondAccount.Name = second.Name
	secondAccount.OwnerReferences[0].Name = second.Name
	objects := readerWithObjects(
		t,
		first,
		executionAccountObject(first.UID),
		second,
		secondAccount,
		&policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: "agent", UID: "agent-uid-current"}},
		&policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: "other-agent", UID: "other-agent-uid"}},
	)
	// The Action actor is a direct broad reader. Its component grant must not cross the delegation
	// boundary to the effective Identity.
	rpc := serveIdentityDiscovery(t, objects, runtimeconfig.Catalog{}, sets.New(peertls.ActionsURI))
	identity := auth.Identity{Name: "agent", UID: "agent-uid-current"}
	ctx := delegatedServiceContext(t, peertls.ActionsURI, identity)
	listed, err := rpc.ListEnvironments(ctx, &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil {
		t.Fatal(err)
	}
	if len(listed.Items) != 1 || listed.Items[0].GetTarget().GetName() != sandboxName {
		t.Fatalf("Identity list = %+v; want only the Sandbox bound to agent", listed.Items)
	}
	descriptor := listed.Items[0]
	if descriptor.GetExecutionAccount() != nil || descriptor.GetActions().GetResumeEligibility() != nil || descriptor.GetActions().GetResumeToken() != "" ||
		descriptor.GetActions().GetAutomaticWake() != environmentpb.AutomaticWakeState_AUTOMATIC_WAKE_STATE_NOT_AVAILABLE {
		t.Fatalf("Identity descriptor inherited account or lifecycle authority: %+v", descriptor)
	}
	if _, err := rpc.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: descriptor.GetTarget()}); err != nil {
		t.Fatalf("Identity GetEnvironment own Sandbox: %v", err)
	}
	if _, err := rpc.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: &identitypb.EnvironmentTarget{
		Namespace: discoveryNamespace, Name: "other", Uid: string(second.UID),
	}}); status.Code(err) != codes.NotFound {
		t.Fatalf("Identity GetEnvironment other Identity Sandbox = %v, want NotFound", err)
	}
	// The name-based Sandbox reference deliberately follows the current same-name Identity; old
	// credentials remain fenced by UID at admission and cannot inherit that replacement.
	if _, err := rpc.GetEnvironment(delegatedServiceContext(t, peertls.ActionsURI, auth.Identity{Name: "agent", UID: "agent-uid-old"}), &environmentpb.GetEnvironmentRequest{Target: descriptor.GetTarget()}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("replaced Identity UID GetEnvironment = %v, want PermissionDenied", err)
	}

	// A direct service reader keeps its broad inventory grant.
	direct, err := rpc.ListEnvironments(serviceContext(t, peertls.ActionsURI), &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil || len(direct.Items) != 2 {
		t.Fatalf("direct component reader list = %+v, %v; want both Sandboxes", direct, err)
	}
}

func TestWorkloadSVIDInventoryAllowsZeroAndMultipleSandboxAttachments(t *testing.T) {
	const credentialID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	const uri = "spiffe://agentplane/" + discoveryNamespace + "/standing-agent/" + credentialID
	for _, count := range []int{0, 1, 2} {
		t.Run(fmt.Sprintf("attachments=%d", count), func(t *testing.T) {
			identity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: "agent", UID: "agent-uid-current"}}
			record := &workloadapi.WorkloadCredential{
				ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: "standing-agent"},
				Spec:       workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: identity.Name, UID: identity.UID}}},
				Status:     workloadapi.WorkloadCredentialStatus{CredentialID: credentialID, URI: uri},
			}
			objects := readerWithObjects(t, identity, record)
			for i := range count {
				sandbox := sandboxObject(types.UID(fmt.Sprintf("sandbox-%d", i)))
				sandbox.Name = fmt.Sprintf("workspace-%d", i)
				sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: identity.Name}
				if err := objects.Create(t.Context(), sandbox); err != nil {
					t.Fatal(err)
				}
			}
			service, err := New(discoveryNamespace, objects, kubernetesIdentities(t, objects, discoveryNamespace), runtimeconfig.Catalog{}, discoveryLifecycle(t, objects, nil), Options{})
			if err != nil {
				t.Fatal(err)
			}
			svids, err := auth.NewSVIDResolver(objects, []string{discoveryNamespace}, workloadauthtesting.IdentityUIDs(t, objects, discoveryNamespace))
			if err != nil {
				t.Fatal(err)
			}
			connection := grpcmethods.ServeTLS(t, peertls.SandboxControllerURI, Methods(svids), func(server *grpc.Server) {
				environmentpb.RegisterEnvironmentServiceServer(server, service)
			}, uri)
			rpc := environmentpb.NewEnvironmentServiceClient(grpcmethods.AsCaller(connection, uri))
			listed, err := rpc.ListEnvironments(t.Context(), &environmentpb.ListEnvironmentsRequest{Limit: 10})
			if err != nil || len(listed.GetItems()) != count {
				t.Fatalf("direct Identity list = %+v, %v; want %d attachments", listed, err, count)
			}
			for _, item := range listed.GetItems() {
				if _, err := rpc.GetEnvironment(t.Context(), &environmentpb.GetEnvironmentRequest{Target: item.GetTarget()}); err != nil {
					t.Fatalf("direct Identity GetEnvironment: %v", err)
				}
			}
			if err := objects.Delete(t.Context(), identity); err != nil {
				t.Fatal(err)
			}
			if _, err := rpc.ListEnvironments(t.Context(), &environmentpb.ListEnvironmentsRequest{Limit: 10}); status.Code(err) != codes.Unauthenticated {
				t.Fatalf("deleted Identity admission = %v", err)
			}
		})
	}
}

func TestManagementRequiresDirectGrantedServiceAndFencesTarget(t *testing.T) {
	objects := baseReader(t)
	rpc := serveDiscovery(t, objects, runtimeconfig.Catalog{})
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	request := &environmentpb.SetEnvironmentWakeOnSendRequest{Target: target, WakeOnSend: true}
	if _, err := rpc.SetEnvironmentWakeOnSend(serviceContext(t, peertls.NotificationsURI), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("service without management authority: got %v", err)
	}
	if _, err := rpc.SetEnvironmentWakeOnSend(delegatedServiceContext(t, peertls.ActionsURI, auth.Identity{Name: delegatedIdentityName, UID: delegatedIdentityUID}), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("Identity delegation reached a mutation method without an Identity grant: got %v", err)
	}
	stale := proto.Clone(request).(*environmentpb.SetEnvironmentWakeOnSendRequest)
	stale.Target.Uid = "old-uid"
	if _, err := rpc.SetEnvironmentWakeOnSend(serviceContext(t, peertls.ActionsURI), stale); status.Code(err) != codes.NotFound {
		t.Fatalf("stale target: got %v", err)
	}
	if _, err := rpc.SetEnvironmentWakeOnSend(serviceContext(t, peertls.ActionsURI), request); err != nil {
		t.Fatal(err)
	}
	stored := &sandboxapi.Sandbox{}
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, stored); err != nil {
		t.Fatal(err)
	}
	if !stored.Spec.WakeOnSend {
		t.Fatal("authorized mutation was not applied")
	}
}

func TestActionLauncherGrantDoesNotConferGeneralManagement(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	origin := &environmentpb.EnvironmentLaunchOrigin{Caller: &identitypb.ServiceAccount{Namespace: discoveryNamespace, Name: sandboxName}, ActionGroup: "orchestration_v1"}
	request := &environmentpb.CreateEnvironmentRequest{Name: "created", Template: "test", LaunchOrigin: origin}
	if _, err := rpc.CreateEnvironment(serviceContext(t, peertls.ActionsURI), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("manager supplied broker origin: %v", err)
	}
	if _, err := rpc.CreateEnvironment(serviceContext(t, peertls.RuntimeProxyURI), &environmentpb.CreateEnvironmentRequest{Name: "created", Template: "test"}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("launcher omitted broker origin: %v", err)
	}
	if _, err := rpc.SuspendEnvironment(serviceContext(t, peertls.RuntimeProxyURI), &environmentpb.EnvironmentMutationRequest{Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("launcher suspended environment: %v", err)
	}
}

func TestActionLauncherOriginRequiresExactlyOneCompleteCaller(t *testing.T) {
	objects := readerWithObjects(t, &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: "test-agent", UID: "test-agent-uid"}})
	rpc := serveDiscovery(t, objects, runtimeconfig.Catalog{})
	for name, origin := range map[string]*environmentpb.EnvironmentLaunchOrigin{
		"both":           {Caller: &identitypb.ServiceAccount{Namespace: discoveryNamespace, Name: "test-launcher"}, CallerIdentity: &identitypb.Identity{Name: "test-launcher", Uid: "test-launcher-uid"}, ActionGroup: "orchestration_v1"},
		"missing UID":    {CallerIdentity: &identitypb.Identity{Name: "test-launcher"}, ActionGroup: "orchestration_v1"},
		"missing name":   {CallerIdentity: &identitypb.Identity{Uid: "test-launcher-uid"}, ActionGroup: "orchestration_v1"},
		"missing caller": {ActionGroup: "orchestration_v1"},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := rpc.CreateEnvironment(serviceContext(t, peertls.RuntimeProxyURI), &environmentpb.CreateEnvironmentRequest{Name: "identity-launched", Template: "test", Identity: "test-agent", LaunchOrigin: origin})
			if status.Code(err) != codes.InvalidArgument {
				t.Fatalf("invalid launch origin result = %v", err)
			}
		})
	}
}

func TestManagementRejectsUnrepresentableDuration(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	tooLarge := &durationpb.Duration{Seconds: 315576000000}
	_, err := rpc.SetEnvironmentIdleSuspend(serviceContext(t, peertls.ActionsURI), &environmentpb.SetEnvironmentIdleSuspendRequest{Target: target, IdleSuspendAfter: tooLarge})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("unrepresentable duration: got %v", err)
	}
	_, err = rpc.CreateEnvironment(serviceContext(t, peertls.ActionsURI), &environmentpb.CreateEnvironmentRequest{Name: "overflow", Template: "runner-vm", IdleSuspendAfter: tooLarge})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("unrepresentable create duration: got %v", err)
	}
}

func TestManagementTemplatesNeedManagerOrLauncherGrant(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	if _, err := rpc.ListEnvironmentTemplates(serviceContext(t, peertls.NotificationsURI), &emptypb.Empty{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ungranted service listed templates: %v", err)
	}
}

func TestDirectComponentReaderListAndGetReturnTheExecutionAccountIdentity(t *testing.T) {
	objects := baseReader(t)
	rpc := serveDiscoveryWithComponentReader(t, objects, runtimeconfig.Catalog{}, nil)
	listed, err := rpc.ListEnvironments(serviceContext(t, peertls.ActionsURI), &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil {
		t.Fatal(err)
	}
	if len(listed.Items) != 1 {
		t.Fatalf("ListEnvironments returned %d items, want 1: %+v", len(listed.Items), listed)
	}
	item := listed.Items[0]
	if item.Target.GetNamespace() != discoveryNamespace || item.Target.GetName() != sandboxName || item.Target.GetUid() != sandboxUID {
		t.Fatalf("target handle = %+v", item.Target)
	}
	if item.ExecutionAccount.GetAccount().GetNamespace() != discoveryNamespace || item.ExecutionAccount.GetAccount().GetName() != sandboxName || item.ExecutionAccount.GetUid() != executionUID {
		t.Fatalf("execution account = %+v", item.ExecutionAccount)
	}
	got, err := rpc.GetEnvironment(serviceContext(t, peertls.ActionsURI), &environmentpb.GetEnvironmentRequest{Target: item.Target})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetTarget().GetUid() != sandboxUID || got.GetExecutionAccount().GetUid() != executionUID {
		t.Fatalf("GetEnvironment returned target/account %+v / %+v", got.GetTarget(), got.GetExecutionAccount())
	}
	if got.GetActions().GetResumeEligibility().GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED ||
		got.GetActions().GetResumeEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_CALLER_NOT_EXECUTION_ACCOUNT ||
		got.GetActions().GetResumeToken() != "" {
		t.Fatalf("component reader inherited workload resume eligibility = %+v", got.GetActions().GetResumeEligibility())
	}
	if got.GetActions().GetWakeGeneration() != 0 {
		t.Fatalf("read-only component discovery changed wake generation: %d", got.GetActions().GetWakeGeneration())
	}
	var unchanged sandboxapi.Sandbox
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, &unchanged); err != nil {
		t.Fatal(err)
	}
	if !unchanged.Spec.Running || unchanged.Spec.WakeGeneration != 0 {
		t.Fatalf("discovery changed Sandbox desired state: running=%t generation=%d", unchanged.Spec.Running, unchanged.Spec.WakeGeneration)
	}
}

func TestRevalidateDescriptorRefreshesIdentityAfterSameUIDChange(t *testing.T) {
	objects := baseReader(t)
	service, err := New(discoveryNamespace, objects, kubernetesIdentities(t, objects, discoveryNamespace), runtimeconfig.Catalog{}, discoveryLifecycle(t, objects, nil), Options{})
	if err != nil {
		t.Fatal(err)
	}
	descriptor := &environmentpb.EnvironmentDescriptor{
		Target:       &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
		IdentityName: proto.String("old-agent"),
	}
	resource := &sandboxapi.Sandbox{}
	key := client.ObjectKey{Namespace: discoveryNamespace, Name: sandboxName}
	if err := objects.Get(t.Context(), key, resource); err != nil {
		t.Fatal(err)
	}
	resource.Spec.Identity = &sandboxapi.IdentityRef{Name: "new-agent"}
	if err := objects.Update(t.Context(), resource); err != nil {
		t.Fatal(err)
	}
	if visible, err := service.revalidateDescriptor(t.Context(), descriptor, environmentCaller{}, true); err != nil || !visible || descriptor.GetIdentityName() != "new-agent" {
		t.Fatalf("revalidated identity = %q, visible %t, error %v; want new-agent", descriptor.GetIdentityName(), visible, err)
	}
	if err := objects.Get(t.Context(), key, resource); err != nil {
		t.Fatal(err)
	}
	resource.Spec.Identity = nil
	if err := objects.Update(t.Context(), resource); err != nil {
		t.Fatal(err)
	}
	if visible, err := service.revalidateDescriptor(t.Context(), descriptor, environmentCaller{}, true); err != nil || !visible || descriptor.IdentityName != nil {
		t.Fatalf("revalidated unbound identity = %q, visible %t, error %v; want absent", descriptor.GetIdentityName(), visible, err)
	}
}

func TestInventoryReaderDoesNotGrantServiceResumeAuthority(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Spec.WakeGeneration = 7
	sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	sandbox.Status.StopStage = sandboxapi.StopStageStopped
	by := sandboxapi.SuspendedByIdle
	sandbox.Status.SuspendedBy = &by
	sandbox.Status.SuspendedAtWakeGeneration = proto.Int64(7)
	objects := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID))
	rpc := serveDiscoveryWithComponentReader(t, objects, runtimeconfig.Catalog{}, nil)
	ctx := serviceContext(t, peertls.ActionsURI)
	listed, err := rpc.ListEnvironments(ctx, &environmentpb.ListEnvironmentsRequest{Limit: 1})
	if err != nil || len(listed.GetItems()) != 1 {
		t.Fatalf("ListEnvironments = %+v, %v", listed, err)
	}
	descriptor := listed.Items[0]
	_, err = rpc.ResumeEnvironment(serviceContext(t, peertls.NotificationsURI), &environmentpb.ResumeEnvironmentRequest{
		Target: descriptor.Target,
		ResumeToken: lifecycle.NewResumeToken(&lifecycle.Target{
			Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID,
		}, 7),
	})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("service without operator role ResumeEnvironment = %v, want PermissionDenied", err)
	}
	current, err := rpc.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: descriptor.Target})
	if err != nil || current.GetActions().GetWakeGeneration() != 7 || current.GetActions().GetResumeToken() != "" {
		t.Fatalf("GetEnvironment after refused resume = %+v, %v", current, err)
	}
	if current.GetObservedState().GetReadiness().GetState() != environmentpb.ReadinessState_READINESS_STATE_NOT_READY {
		t.Fatalf("resume acceptance invented readiness: %+v", current.GetObservedState().GetReadiness())
	}
}

func TestDiscoveryReportsResumeDenialAndReadinessFactsWithoutEffects(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Spec.ResumePolicy = &sandboxapi.ResumePolicy{AllowOwnServiceAccount: false}
	objects := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID))
	rpc := serveDiscoveryWithComponentReader(t, objects, runtimeconfig.Catalog{}, nil)
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	got, err := rpc.GetEnvironment(serviceContext(t, peertls.ActionsURI), &environmentpb.GetEnvironmentRequest{
		Target: target,
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetObservedState().GetReadiness().GetState() != environmentpb.ReadinessState_READINESS_STATE_NOT_READY ||
		got.GetObservedState().GetReadiness().GetReason() != "ReadyConditionUnavailable" {
		t.Fatalf("current readiness descriptor = %+v", got.GetObservedState().GetReadiness())
	}
	if got.GetActions().GetResumeEligibility().GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED ||
		got.GetActions().GetResumeEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_CALLER_NOT_EXECUTION_ACCOUNT || got.GetActions().GetResumeToken() != "" {
		t.Fatalf("denied resume facts = eligibility %+v token=%q", got.GetActions().GetResumeEligibility(), got.GetActions().GetResumeToken())
	}
	var unchanged sandboxapi.Sandbox
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, &unchanged); err != nil {
		t.Fatal(err)
	}
	if !unchanged.Spec.Running || unchanged.Spec.WakeGeneration != 0 {
		t.Fatalf("discovery changed Sandbox desired state: running=%t generation=%d", unchanged.Spec.Running, unchanged.Spec.WakeGeneration)
	}
}

func TestComponentReaderDoesNotInheritOtherServiceWakeAuthority(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Spec.WakeOnSend = true
	sandbox.Spec.ResumePolicy = &sandboxapi.ResumePolicy{AllowOwnServiceAccount: false}
	sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	sandbox.Status.StopStage = sandboxapi.StopStageStopped
	objects := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID))
	rpc := serveDiscoveryWithComponentReader(t, objects, runtimeconfig.Catalog{}, []string{peertls.NotificationsURI})
	got, err := rpc.GetEnvironment(serviceContext(t, peertls.ActionsURI), &environmentpb.GetEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetActions().GetAutomaticWake() != environmentpb.AutomaticWakeState_AUTOMATIC_WAKE_STATE_NOT_AVAILABLE {
		t.Fatalf("component reader inherited another service automatic wake = %s", got.GetActions().GetAutomaticWake())
	}
	if got.GetActions().GetResumeEligibility().GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED ||
		got.GetActions().GetResumeEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_CALLER_NOT_EXECUTION_ACCOUNT ||
		got.GetActions().GetResumeToken() != "" {
		t.Fatalf("explicit resume projection = %+v token=%q, want policy denial and no token", got.GetActions().GetResumeEligibility(), got.GetActions().GetResumeToken())
	}
	var unchanged sandboxapi.Sandbox
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, &unchanged); err != nil {
		t.Fatal(err)
	}
	if !unchanged.Spec.Running || unchanged.Spec.WakeGeneration != 0 {
		t.Fatalf("wake assessment changed Sandbox desired state: running=%t generation=%d", unchanged.Spec.Running, unchanged.Spec.WakeGeneration)
	}
}

func TestGetReturnsTheCanonicalSandboxAndCurrentPodProjection(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	createdAt := metav1.NewTime(time.Date(2026, time.November, 2, 12, 0, 0, 0, time.UTC))
	sandbox.CreationTimestamp = createdAt
	sandbox.Spec.Bootstrap = "echo initialized"
	sandbox.Spec.WakeOnSend = true
	sandbox.Spec.IdleSuspendAfter = &metav1.Duration{Duration: 3 * time.Minute}
	sandbox.Labels = map[string]string{"agentplane.allegedly.works/preset": "gpu-workbench"}
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: "vm-launcher", UID: "pod-uid-current"}
	sandbox.Status.Conditions = []metav1.Condition{{
		Type: sandboxapi.ConditionReady, Status: metav1.ConditionTrue, Reason: "RuntimeReady",
		Message: "Environment Runtime is accepting work", LastTransitionTime: createdAt,
	}}
	suspendedBy := sandboxapi.SuspendedByIdle
	sandbox.Status.SuspendedBy = &suspendedBy
	idleSuspendAt := metav1.NewTime(createdAt.Add(5 * time.Minute))
	sandbox.Status.IdleSuspendAt = &idleSuspendAt
	sessionID := "session-123"
	sandbox.Status.IdleSuspendBlockers = []sandboxapi.AutosuspendBlocker{
		{Reason: sandboxapi.AutosuspendBlockerActiveTurn, SessionID: &sessionID},
		{Reason: sandboxapi.AutosuspendBlockerBootstrap},
	}
	controller := true
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: discoveryNamespace, Name: "vm-launcher", UID: "pod-uid-current",
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: "kubevirt.io/v1", Kind: "VirtualMachineInstance", Name: "vm", UID: "vmi-uid", Controller: &controller,
			}},
		},
		Spec:   corev1.PodSpec{NodeName: "worker-a"},
		Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "10.0.0.42"},
	}
	objects := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID), pod)
	rpc := serveDiscoveryWithComponentReader(t, objects, runtimeconfig.Catalog{}, nil)
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	got, err := rpc.GetEnvironment(serviceContext(t, peertls.ActionsURI), &environmentpb.GetEnvironmentRequest{Target: target})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetObservedState().GetCreatedAt().AsTime() != createdAt.Time || got.GetConfiguration().GetBinding().GetBootstrap() != sandbox.Spec.Bootstrap || got.GetConfiguration().GetPreset() != "gpu-workbench" {
		t.Fatalf("creation/binding/preset projection = %v / %v / %q", got.GetObservedState().GetCreatedAt(), got.GetConfiguration().GetBinding(), got.GetConfiguration().GetPreset())
	}
	if got.GetKubernetesDiagnostics().GetPod().GetUid() != "pod-uid-current" || got.GetKubernetesDiagnostics().GetPod().GetNodeName() != "worker-a" || got.GetKubernetesDiagnostics().GetPod().GetStatus().AsMap()["phase"] != "Running" ||
		len(got.GetKubernetesDiagnostics().GetPod().GetOwnerReferences()) != 1 || !got.GetKubernetesDiagnostics().GetPod().GetOwnerReferences()[0].GetController() {
		t.Fatalf("current Pod projection = %v", got.GetKubernetesDiagnostics().GetPod())
	}
	incarnation, ok := got.GetKubernetesDiagnostics().GetSandboxStatus().AsMap()["incarnation"].(map[string]any)
	if !ok || incarnation["uid"] != "pod-uid-current" {
		t.Fatalf("raw Sandbox status = %v", got.GetKubernetesDiagnostics().GetSandboxStatus())
	}
	if got.GetObservedState().GetReadiness().GetState() != environmentpb.ReadinessState_READINESS_STATE_READY ||
		got.GetObservedState().GetPhase() != environmentpb.SandboxPhase_SANDBOX_PHASE_RUNNING || got.GetObservedState().GetSuspendedBy() != "Idle" ||
		got.GetConfiguration().GetIdleSuspendAfter().AsDuration() != 3*time.Minute || got.GetObservedState().GetIdleSuspendAt().AsTime() != idleSuspendAt.Time {
		t.Fatalf("Sandbox status projection = %v", got)
	}
	if len(got.GetObservedState().GetAutosuspendBlockers()) != 2 || got.GetObservedState().GetAutosuspendBlockers()[0].GetSessionId() != sessionID {
		t.Fatalf("autosuspend blockers = %v", got.GetObservedState().GetAutosuspendBlockers())
	}
	if _, err := rpc.GetEnvironment(serviceContext(t, peertls.NotificationsURI), &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.NotFound {
		t.Fatalf("foreign caller GetEnvironment error = %v, want NotFound", err)
	}
}

func TestGetDoesNotProjectAReplacedPodWithTheSameName(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: "runner", UID: "pod-uid-expected"}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: discoveryNamespace, Name: "runner", UID: "pod-uid-replacement",
	}}
	rpc := serveDiscoveryWithComponentReader(t, readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID), pod), runtimeconfig.Catalog{}, nil)
	got, err := rpc.GetEnvironment(serviceContext(t, peertls.ActionsURI), &environmentpb.GetEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetKubernetesDiagnostics().GetPod() != nil || got.GetObservedState().GetRuntimeIncarnationUid() != "pod-uid-expected" {
		t.Fatalf("replacement Pod was projected as current: Pod=%v incarnationUID=%q", got.GetKubernetesDiagnostics().GetPod(), got.GetObservedState().GetRuntimeIncarnationUid())
	}
}

func TestDelegatedIdentityReadsUseLiveBindingAndTargetUIDFences(t *testing.T) {
	rpc := serveDiscoveryWithDelegation(t, baseReader(t), runtimeconfig.Catalog{}, []string{
		environmentpb.EnvironmentService_ListEnvironments_FullMethodName,
		environmentpb.EnvironmentService_GetEnvironment_FullMethodName,
	})
	effective := auth.Identity{Name: delegatedIdentityName, UID: delegatedIdentityUID}
	ctx := delegatedServiceContext(t, peertls.ActionsURI, effective)

	listed, err := rpc.ListEnvironments(ctx, &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil {
		t.Fatal(err)
	}
	if len(listed.Items) != 1 || listed.Items[0].Target.GetUid() != sandboxUID {
		t.Fatalf("delegated ListEnvironments returned %+v, want the effective caller's current Sandbox", listed.Items)
	}
	got, err := rpc.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: listed.Items[0].Target})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetExecutionAccount() != nil {
		t.Fatalf("delegated Identity GetEnvironment exposed a ServiceAccount account: %+v", got.GetExecutionAccount())
	}
	if got.GetActions().GetResumeEligibility() != nil {
		t.Fatalf("Identity read inherited ServiceAccount lifecycle authority: %+v", got.GetActions().GetResumeEligibility())
	}

	for _, target := range []*identitypb.EnvironmentTarget{
		{Namespace: foreignNamespace, Name: sandboxName, Uid: sandboxUID},
		{Namespace: discoveryNamespace, Name: sandboxName, Uid: "replaced-sandbox-uid"},
	} {
		if _, err := rpc.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.NotFound {
			t.Errorf("delegated GetEnvironment target %+v error = %v, want NotFound", target, err)
		}
	}

	// An admitted service without a reader role cannot borrow Identity inventory access.
	if _, err := rpc.GetEnvironment(serviceContext(t, peertls.NotificationsURI), &environmentpb.GetEnvironmentRequest{Target: listed.Items[0].Target}); status.Code(err) != codes.NotFound {
		t.Fatalf("service without reader role received environment access: %v", err)
	}
}

func TestDelegatedDiscoveryRejectsUntrustedActorAndReplacedCallerUID(t *testing.T) {
	rpc := serveDiscoveryWithDelegation(t, baseReader(t), runtimeconfig.Catalog{}, []string{
		environmentpb.EnvironmentService_ListEnvironments_FullMethodName,
		environmentpb.EnvironmentService_GetEnvironment_FullMethodName,
	})
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	effective := auth.Identity{Name: delegatedIdentityName, UID: delegatedIdentityUID}
	for _, test := range []struct {
		name string
		ctx  context.Context
		code codes.Code
	}{
		{
			name: "untrusted actor",
			ctx:  delegatedServiceContext(t, peertls.AppURI, effective),
			code: codes.PermissionDenied,
		},
		{
			name: "replaced effective Identity UID",
			ctx:  delegatedServiceContext(t, peertls.ActionsURI, auth.Identity{Name: delegatedIdentityName, UID: "old-identity-uid"}),
			code: codes.PermissionDenied,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			if _, err := rpc.GetEnvironment(test.ctx, &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != test.code {
				t.Fatalf("GetEnvironment error = %v, want %s", err, test.code)
			}
		})
	}
}

func TestDelegatedDiscoveryRequiresOneValidEnvelopeAndAnOptedInMethod(t *testing.T) {
	methods := []string{
		environmentpb.EnvironmentService_ListEnvironments_FullMethodName,
		environmentpb.EnvironmentService_GetEnvironment_FullMethodName,
	}
	rpc := serveDiscoveryWithDelegation(t, baseReader(t), runtimeconfig.Catalog{}, methods)
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	valid := delegatedServiceContext(t, peertls.ActionsURI, auth.Identity{Name: delegatedIdentityName, UID: delegatedIdentityUID})
	validMetadata, _ := metadata.FromOutgoingContext(valid)
	encodedEnvelope := validMetadata.Get(auth.DelegationMetadataKey)[0]
	for _, test := range []struct {
		name string
		ctx  context.Context
	}{
		{
			name: "missing envelope",
			ctx:  metadata.NewOutgoingContext(t.Context(), metadata.Pairs("authorization", "Agentplane-Delegated retired-actions")),
		},
		{
			name: "malformed envelope",
			ctx: metadata.NewOutgoingContext(serviceContext(t, peertls.ActionsURI), metadata.Pairs(
				auth.DelegationMetadataKey, "\x00",
			)),
		},
		{
			name: "repeated envelope",
			ctx: metadata.NewOutgoingContext(serviceContext(t, peertls.ActionsURI), metadata.Pairs(
				auth.DelegationMetadataKey, encodedEnvelope,
				auth.DelegationMetadataKey, encodedEnvelope,
			)),
		},
		{
			name: "bearer mode with envelope",
			ctx: metadata.NewOutgoingContext(t.Context(), metadata.Pairs(
				"authorization", "Bearer retired-account",
				auth.DelegationMetadataKey, encodedEnvelope,
			)),
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			if _, err := rpc.GetEnvironment(test.ctx, &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.Unauthenticated {
				t.Fatalf("GetEnvironment error = %v, want Unauthenticated", err)
			}
		})
	}

	listOnly := serveDiscoveryWithDelegation(t, baseReader(t), runtimeconfig.Catalog{}, []string{
		environmentpb.EnvironmentService_ListEnvironments_FullMethodName,
	})
	if _, err := listOnly.GetEnvironment(valid, &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("delegated GetEnvironment without method opt-in error = %v, want PermissionDenied", err)
	}
	if _, err := rpc.ResumeEnvironment(valid, &environmentpb.ResumeEnvironmentRequest{Target: target}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("delegated reads granted resume without method opt-in: %v", err)
	}
}

func TestGetUsesTheSameNotFoundForMissingAndUnauthorizedTargets(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	var want *status.Status
	for _, test := range []struct {
		name   string
		uri    string
		target *identitypb.EnvironmentTarget
	}{
		{name: "missing name", uri: peertls.NotificationsURI, target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: "missing", Uid: "missing-uid"}},
		{name: "foreign caller", uri: peertls.NotificationsURI, target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, err := rpc.GetEnvironment(serviceContext(t, test.uri), &environmentpb.GetEnvironmentRequest{Target: test.target})
			got := status.Convert(err)
			if got.Code() != codes.NotFound {
				t.Fatalf("GetEnvironment status = %v, want NotFound", got)
			}
			if want == nil {
				want = got
			} else if !proto.Equal(want.Proto(), got.Proto()) {
				t.Fatalf("GetEnvironment status for %s = %v, want the same status as missing name: %v", test.name, got, want)
			}
		})
	}
}

func TestGetRejectsStaleAndDeletingSandboxIncarnations(t *testing.T) {
	for _, test := range []struct {
		name       string
		sandbox    *sandboxapi.Sandbox
		requested  string
		wantListed int
		wantGet    bool
	}{
		{name: "stale UID", sandbox: sandboxObject(sandboxUID), requested: "old-sandbox-uid", wantListed: 1},
		{name: "deleting Sandbox", sandbox: deletingSandbox(), requested: sandboxUID, wantListed: 1, wantGet: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			reader := readerWithObjects(t, test.sandbox, executionAccountObject(test.sandbox.UID))
			rpc := serveDiscoveryWithComponentReader(t, reader, runtimeconfig.Catalog{}, nil)
			_, err := rpc.GetEnvironment(serviceContext(t, peertls.ActionsURI), &environmentpb.GetEnvironmentRequest{
				Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: test.requested},
			})
			if test.wantGet {
				if err != nil {
					t.Fatalf("GetEnvironment error = %v, want broad-reader visibility during deletion", err)
				}
			} else if status.Code(err) != codes.NotFound {
				t.Fatalf("GetEnvironment error = %v, want NotFound", err)
			}
			listed, err := rpc.ListEnvironments(serviceContext(t, peertls.ActionsURI), &environmentpb.ListEnvironmentsRequest{Limit: 10})
			if err != nil {
				t.Fatal(err)
			}
			if len(listed.Items) != test.wantListed {
				t.Fatalf("ListEnvironments returned %d items, want %d: %+v", len(listed.Items), test.wantListed, listed.Items)
			}
		})
	}
}

func deletingSandbox() *sandboxapi.Sandbox {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Finalizers = []string{"test.agentplane/finalizer"}
	deletionTime := metav1.NewTime(time.Now())
	sandbox.DeletionTimestamp = &deletionTime
	return sandbox
}

func TestInventoryDoesNotProjectUnownedExecutionAccounts(t *testing.T) {
	for _, test := range []struct {
		name    string
		account *corev1.ServiceAccount
	}{
		{name: "missing execution account"},
		{name: "missing controller owner reference", account: &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: sandboxName, UID: executionUID}}},
		{name: "owner reference names another Sandbox incarnation", account: executionAccountObject("old-sandbox-uid")},
		{name: "owner reference is not controller-owned", account: accountWithoutControllerOwner()},
	} {
		t.Run(test.name, func(t *testing.T) {
			sandbox := sandboxObject(sandboxUID)
			objects := []client.Object{sandbox}
			if test.account != nil {
				objects = append(objects, test.account)
			}
			rpc := serveDiscoveryWithComponentReader(t, readerWithObjects(t, objects...), runtimeconfig.Catalog{}, nil)
			got, err := rpc.GetEnvironment(serviceContext(t, peertls.ActionsURI), &environmentpb.GetEnvironmentRequest{
				Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
			})
			if err != nil {
				t.Fatalf("GetEnvironment error = %v", err)
			}
			if got.GetExecutionAccount() != nil {
				t.Fatalf("GetEnvironment projected unowned execution account: %+v", got.GetExecutionAccount())
			}
			listed, err := rpc.ListEnvironments(serviceContext(t, peertls.ActionsURI), &environmentpb.ListEnvironmentsRequest{Limit: 10})
			if err != nil {
				t.Fatal(err)
			}
			if len(listed.Items) != 1 || listed.Items[0].GetExecutionAccount() != nil {
				t.Fatalf("ListEnvironments account projection without ownership proof: %+v", listed.Items)
			}
		})
	}
}

func accountWithoutControllerOwner() *corev1.ServiceAccount {
	account := executionAccountObject(sandboxUID)
	controller := false
	account.OwnerReferences[0].Controller = &controller
	return account
}

func TestDiscoveryValidatesLimitsPageTokensAndExactHandles(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	for _, limit := range []uint32{0, maxLimit + 1} {
		if _, err := rpc.ListEnvironments(serviceContext(t, peertls.NotificationsURI), &environmentpb.ListEnvironmentsRequest{Limit: limit}); status.Code(err) != codes.InvalidArgument {
			t.Errorf("limit %d error = %v, want InvalidArgument", limit, err)
		}
	}
	if _, err := rpc.ListEnvironments(serviceContext(t, peertls.NotificationsURI), &environmentpb.ListEnvironmentsRequest{Limit: 10, PageToken: "next-page"}); status.Code(err) != codes.InvalidArgument {
		t.Errorf("page token error = %v, want InvalidArgument", err)
	}
	for _, target := range []*identitypb.EnvironmentTarget{
		nil,
		{Namespace: discoveryNamespace, Name: sandboxName},
		{Namespace: discoveryNamespace, Uid: sandboxUID},
		{Name: sandboxName, Uid: sandboxUID},
	} {
		if _, err := rpc.GetEnvironment(serviceContext(t, peertls.NotificationsURI), &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.InvalidArgument {
			t.Errorf("target %+v error = %v, want InvalidArgument", target, err)
		}
	}
}

type failingReader struct {
	client.WithWatch
	failSandboxRead int
	sandboxReads    int
	failAccountRead bool
}

func (r *failingReader) Get(ctx context.Context, key types.NamespacedName, object client.Object, options ...client.GetOption) error {
	switch object.(type) {
	case *sandboxapi.Sandbox:
		r.sandboxReads++
		if r.failSandboxRead == r.sandboxReads {
			return apierrors.NewInternalError(errors.New("injected Sandbox API failure"))
		}
	case *corev1.ServiceAccount:
		if r.failAccountRead && key.Name == sandboxName {
			return apierrors.NewInternalError(errors.New("injected ServiceAccount API failure"))
		}
	}
	return r.WithWatch.Get(ctx, key, object, options...)
}

func TestKubernetesReadFailuresReturnUnavailable(t *testing.T) {
	for _, test := range []struct {
		name   string
		reader func(t *testing.T) client.WithWatch
	}{
		{
			name: "Sandbox read",
			reader: func(t *testing.T) client.WithWatch {
				return &failingReader{WithWatch: baseReader(t), failSandboxRead: 1}
			},
		},
		{
			name: "execution ServiceAccount read",
			reader: func(t *testing.T) client.WithWatch {
				return &failingReader{WithWatch: baseReader(t), failAccountRead: true}
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			for _, operation := range []string{"GetEnvironment", "ListEnvironments"} {
				t.Run(operation, func(t *testing.T) {
					rpc := serveDiscoveryWithComponentReader(t, test.reader(t), runtimeconfig.Catalog{}, nil)
					var err error
					switch operation {
					case "GetEnvironment":
						_, err = rpc.GetEnvironment(serviceContext(t, peertls.ActionsURI), &environmentpb.GetEnvironmentRequest{
							Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
						})
					case "ListEnvironments":
						_, err = rpc.ListEnvironments(serviceContext(t, peertls.ActionsURI), &environmentpb.ListEnvironmentsRequest{Limit: 10})
					}
					if status.Code(err) != codes.Unavailable {
						t.Fatalf("%s error = %v, want Unavailable", operation, err)
					}
				})
			}
		})
	}
}

func TestEveryGeneratedRPCRejectsBearerAndUnlistedMethods(t *testing.T) {
	register := func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, environmentpb.UnimplementedEnvironmentServiceServer{})
	}
	configured := fixtureMethods(t)
	connection := serveFixture(t, configured, register)
	for _, method := range grpcmethods.Of(environmentpb.EnvironmentService_ServiceDesc) {
		t.Run(method.Name, func(t *testing.T) {
			ctx := metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer retired-account")
			if got := status.Code(method.Call(ctx, connection)); got != codes.Unauthenticated {
				t.Fatalf("bearer reached %s: %v", method.Name, got)
			}
			unlisted := maps.Clone(configured)
			delete(unlisted, method.Name)
			if got := status.Code(method.Call(serviceContext(t, peertls.ActionsURI), serveFixture(t, unlisted, register))); got != codes.PermissionDenied {
				t.Fatalf("unlisted %s: %v", method.Name, got)
			}
		})
	}
}

func TestServiceInventoryRequiresHandlerReaderRole(t *testing.T) {
	other := sandboxObject("other-uid")
	other.Name = "other"
	terminating := sandboxObject("deleting-uid")
	terminating.Name = "deleting"
	deletion := metav1.Now()
	terminating.DeletionTimestamp = &deletion
	terminating.Finalizers = []string{"test.example/retain"}
	terminating.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	objects := readerWithObjects(t, sandboxObject(sandboxUID), executionAccountObject(sandboxUID), other, terminating)
	service, err := New(discoveryNamespace, objects, kubernetesIdentities(t, objects, discoveryNamespace), runtimeconfig.Catalog{}, discoveryLifecycle(t, objects, nil), Options{ServiceReaders: sets.New(peertls.ActionsURI)})
	if err != nil {
		t.Fatal(err)
	}
	connection := serveFixture(t, fixtureMethods(t), func(server *grpc.Server) { environmentpb.RegisterEnvironmentServiceServer(server, service) })
	rpc := environmentpb.NewEnvironmentServiceClient(connection)
	own, err := rpc.ListEnvironments(serviceContext(t, peertls.NotificationsURI), &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil || len(own.GetItems()) != 0 {
		t.Fatalf("service without reader role list = %v, %v; want empty", own, err)
	}
	all, err := rpc.ListEnvironments(serviceContext(t, peertls.ActionsURI), &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil || len(all.GetItems()) != 3 {
		t.Fatalf("reader list = %v, %v", all, err)
	}
	for _, target := range []*identitypb.EnvironmentTarget{
		{Namespace: discoveryNamespace, Name: "other", Uid: "other-uid"},
		{Namespace: discoveryNamespace, Name: "deleting", Uid: "deleting-uid"},
	} {
		if _, err := rpc.GetEnvironment(serviceContext(t, peertls.NotificationsURI), &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.NotFound {
			t.Fatalf("own get = %v", err)
		}
		got, err := rpc.GetEnvironment(serviceContext(t, peertls.ActionsURI), &environmentpb.GetEnvironmentRequest{Target: target})
		if err != nil || got.GetTarget().GetName() != target.Name || got.ExecutionAccount != nil {
			t.Fatalf("reader get = %v, %v", got, err)
		}
		if target.Name == "deleting" && (got.GetActions().GetResumeEligibility() != nil || got.GetActions().GetResumeToken() != "" || got.GetActions().GetAutomaticWake() != environmentpb.AutomaticWakeState_AUTOMATIC_WAKE_STATE_NOT_AVAILABLE) {
			t.Fatalf("deleting environment advertises resume: %v", got)
		}
	}
}

func TestDelegatedActorDoesNotLendInventoryReaderGrant(t *testing.T) {
	ownedIdentity := auth.Identity{Name: "owned-identity", UID: "owned-identity-uid"}
	ownedRecord := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: ownedIdentity.Name, UID: types.UID(ownedIdentity.UID)}}
	otherIdentity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: "other-identity", UID: "other-identity-uid"}}
	first := sandboxObject(sandboxUID)
	first.Spec.Identity = &sandboxapi.IdentityRef{Name: ownedIdentity.Name}
	other := sandboxObject("other-uid")
	other.Name = "other"
	other.Spec.Identity = &sandboxapi.IdentityRef{Name: "other-identity"}
	objects := readerWithObjects(t, first, executionAccountObject(first.UID), ownedRecord, other, otherIdentity)
	service, err := New(discoveryNamespace, objects, kubernetesIdentities(t, objects, discoveryNamespace), runtimeconfig.Catalog{}, discoveryLifecycle(t, objects, nil), Options{ServiceReaders: sets.New(peertls.ActionsURI)})
	if err != nil {
		t.Fatal(err)
	}
	identities, err := auth.KubernetesIdentityUIDs(objects, discoveryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	methods, err := fixtureMethods(t).WithServiceDelegation([]string{environmentpb.EnvironmentService_ListEnvironments_FullMethodName}, []auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}}, identities)
	if err != nil {
		t.Fatal(err)
	}
	connection := serveFixture(t, methods, func(server *grpc.Server) { environmentpb.RegisterEnvironmentServiceServer(server, service) })
	rpc := environmentpb.NewEnvironmentServiceClient(connection)
	response, err := rpc.ListEnvironments(delegatedServiceContext(t, peertls.ActionsURI, ownedIdentity), &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil || len(response.GetItems()) != 1 || response.GetItems()[0].GetTarget().GetName() != first.Name {
		t.Fatalf("delegated Identity list inherited actor reader scope: %v, %v", response, err)
	}
}

func TestCollectionSnapshotSurvivesDeletionDuringEligibilityRead(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: "runner", UID: "pod-uid-current"}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: discoveryNamespace, Name: "runner", UID: "pod-uid-current",
	}}
	base := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID), pod)
	reader := &changeAfterPodRead{WithWatch: base, change: func(ctx context.Context) error {
		return base.Delete(ctx, sandbox)
	}}
	rpc := serveDiscoveryWithComponentReader(t, reader, runtimeconfig.Catalog{}, nil)
	stream, err := rpc.WatchEnvironments(serviceContext(t, peertls.ActionsURI), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if rows := receiveCollection(t, stream); len(rows) != 0 {
		t.Fatalf("collection snapshot included the deleted Sandbox: %v", rows)
	}
	if !reader.changed {
		t.Fatal("test did not delete the Sandbox during projection")
	}
}

func TestGetFencesSandboxReplacementDuringCallerAuthorization(t *testing.T) {
	replacement := sandboxObject("replacement-sandbox-uid")
	reader := &replaceSandboxOnAuthorizationRead{
		WithWatch:   baseReader(t),
		replacement: replacement,
	}
	rpc := serveDiscoveryWithComponentReader(t, reader, runtimeconfig.Catalog{}, nil)
	_, err := rpc.GetEnvironment(serviceContext(t, peertls.ActionsURI), &environmentpb.GetEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("GetEnvironment during same-name replacement = %v, want NotFound", err)
	}
	if !reader.replaced {
		t.Fatal("test did not install the same-name Sandbox replacement during authorization")
	}
	var current sandboxapi.Sandbox
	if err := reader.WithWatch.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, &current); err != nil {
		t.Fatal(err)
	}
	if current.UID != replacement.UID {
		t.Fatalf("current Sandbox UID = %q, want replacement UID %q", current.UID, replacement.UID)
	}
}

func TestGetRechecksExecutionAccountProjectionAfterPodProjection(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: "runner", UID: "pod-uid-current"}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: discoveryNamespace, Name: "runner", UID: "pod-uid-current",
	}}
	base := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID), pod)
	reader := &changeAfterPodRead{WithWatch: base, change: func(ctx context.Context) error {
		var account corev1.ServiceAccount
		if err := base.Get(ctx, types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, &account); err != nil {
			return err
		}
		account.OwnerReferences = nil
		return base.Update(ctx, &account)
	}}
	rpc := serveDiscoveryWithComponentReader(t, reader, runtimeconfig.Catalog{}, nil)
	got, err := rpc.GetEnvironment(serviceContext(t, peertls.ActionsURI), &environmentpb.GetEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if err != nil {
		t.Fatalf("GetEnvironment after owner changed during Pod projection = %v", err)
	}
	if !reader.changed {
		t.Fatal("test did not remove the execution account owner during projection")
	}
	if got.GetExecutionAccount() != nil {
		t.Fatalf("GetEnvironment projected stale execution account after recheck: %+v", got.GetExecutionAccount())
	}
}

func TestGetUsesRevalidatedIdentityAfterPodProjection(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: delegatedIdentityName}
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: "runner", UID: "pod-uid-current"}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: discoveryNamespace, Name: "runner", UID: "pod-uid-current",
	}}
	base := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID), pod)
	reader := &changeAfterPodRead{WithWatch: base, change: func(ctx context.Context) error {
		var current sandboxapi.Sandbox
		if err := base.Get(ctx, types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, &current); err != nil {
			return err
		}
		current.Spec.Identity = &sandboxapi.IdentityRef{Name: "after-pod-read"}
		return base.Update(ctx, &current)
	}}
	rpc := serveDiscoveryWithDelegation(t, reader, runtimeconfig.Catalog{}, []string{
		environmentpb.EnvironmentService_GetEnvironment_FullMethodName,
	})
	_, err := rpc.GetEnvironment(delegatedServiceContext(t, peertls.ActionsURI, auth.Identity{Name: delegatedIdentityName, UID: delegatedIdentityUID}), &environmentpb.GetEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("GetEnvironment after Identity binding changed during Pod projection = %v, want NotFound", err)
	}
	if !reader.changed {
		t.Fatal("test did not change Sandbox Identity during Pod projection")
	}
}

type replaceSandboxOnAuthorizationRead struct {
	client.WithWatch
	replacement *sandboxapi.Sandbox
	sandboxGets int
	replaced    bool
}

func (r *replaceSandboxOnAuthorizationRead) Get(ctx context.Context, key types.NamespacedName, object client.Object, options ...client.GetOption) error {
	if sandbox, ok := object.(*sandboxapi.Sandbox); ok {
		r.sandboxGets++
		if r.sandboxGets == 2 {
			var current sandboxapi.Sandbox
			if err := r.WithWatch.Get(ctx, key, &current); err != nil {
				return err
			}
			if err := r.WithWatch.Delete(ctx, &current); err != nil {
				return err
			}
			if err := r.WithWatch.Create(ctx, r.replacement); err != nil {
				return err
			}
			r.replaced = true
			r.replacement.DeepCopyInto(sandbox)
			return nil
		}
	}
	return r.WithWatch.Get(ctx, key, object, options...)
}

// changeAfterPodRead injects a concurrent change between reading the Pod and finishing its descriptor.
type changeAfterPodRead struct {
	client.WithWatch
	change  func(context.Context) error
	changed bool
}

func (r *changeAfterPodRead) Get(ctx context.Context, key types.NamespacedName, object client.Object, options ...client.GetOption) error {
	if err := r.WithWatch.Get(ctx, key, object, options...); err != nil {
		return err
	}
	if _, ok := object.(*corev1.Pod); ok && !r.changed {
		r.changed = true
		return r.change(ctx)
	}
	return nil
}
