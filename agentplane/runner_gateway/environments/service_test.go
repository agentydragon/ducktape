package environments

import (
	"context"
	"errors"
	"maps"
	"strings"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/emptypb"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
)

const (
	discoveryNamespace = "test-envs"
	foreignNamespace   = "other-envs"
	dispatcherAudience = "session-dispatcher-test"
	actionsToken       = "trusted-actions"
	launcherToken      = "trusted-launcher"
	untrustedToken     = "untrusted-actions"
	sandboxName        = "workspace"
	sandboxUID         = "sandbox-uid-current"
	executionUID       = "service-account-uid-current"
	matchingToken      = "matching-account"
	recreatedToken     = "same-name-recreated-account"
	foreignToken       = "foreign-account"
	actionRequestID    = "00000000-0000-4000-8000-000000000001"
	actionExecutionID  = "00000000-0000-4000-8000-000000000002"
)

type tokenReviewStub map[string]authenticationv1.UserInfo

type unusedWakeBudget struct{}

func (unusedWakeBudget) ReserveWakeAttempt(context.Context, lifecycle.WakeBudgetKey, string, int64) (time.Duration, error) {
	return 0, nil
}

func discoveryLifecycle(t *testing.T, objects client.WithWatch, wakeCallers []auth.Account) *lifecycle.Manager {
	t.Helper()
	manager, err := lifecycle.NewManager(lifecycle.Options{
		Namespace: discoveryNamespace, Objects: objects, WakeBudget: unusedWakeBudget{}, WakeCallers: wakeCallers,
	})
	if err != nil {
		t.Fatal(err)
	}
	return manager
}

func (s tokenReviewStub) Create(_ context.Context, request *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	user, ok := s[request.Spec.Token]
	if !ok {
		return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{Audiences: request.Spec.Audiences}}, nil
	}
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     request.Spec.Audiences,
		User:          user,
	}}, nil
}

func testTokenReviews() tokenReviewStub {
	return tokenReviewStub{
		matchingToken:  {Username: "system:serviceaccount:" + discoveryNamespace + ":" + sandboxName, UID: executionUID},
		recreatedToken: {Username: "system:serviceaccount:" + discoveryNamespace + ":" + sandboxName, UID: "service-account-uid-recreated"},
		foreignToken:   {Username: "system:serviceaccount:" + foreignNamespace + ":" + sandboxName, UID: executionUID},
		actionsToken: {
			Username: "system:serviceaccount:" + discoveryNamespace + ":actions", UID: "actions-uid",
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {"actions-pod"},
				"authentication.kubernetes.io/pod-uid":  {"actions-pod-uid"},
			},
		},
		launcherToken: {
			Username: "system:serviceaccount:" + discoveryNamespace + ":launcher", UID: "launcher-uid",
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {"launcher-pod"},
				"authentication.kubernetes.io/pod-uid":  {"launcher-pod-uid"},
			},
		},
		untrustedToken: {
			Username: "system:serviceaccount:" + foreignNamespace + ":actions", UID: "other-actions-uid",
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {"other-actions-pod"},
				"authentication.kubernetes.io/pod-uid":  {"other-actions-pod-uid"},
			},
		},
	}
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
	return serveDiscoveryWithOptions(t, objects, catalog, delegatedMethods, nil, nil)
}

func serveDiscoveryWithOptions(
	t *testing.T,
	objects client.WithWatch,
	catalog runtimeconfig.Catalog,
	delegatedMethods []string,
	wakeCallers []auth.Account,
	identityChanges IdentityChangeGuard,
) environmentpb.EnvironmentServiceClient {
	t.Helper()
	service, err := New(discoveryNamespace, objects, catalog, discoveryLifecycle(t, objects, wakeCallers), nil, ManagementOptions{
		Managers:        sets.New(auth.Account{Namespace: discoveryNamespace, Name: "actions"}),
		Launchers:       sets.New(auth.Account{Namespace: discoveryNamespace, Name: "launcher"}),
		IdentityChanges: identityChanges,
	})
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := auth.NewResolver(testTokenReviews(), dispatcherAudience, []string{discoveryNamespace, foreignNamespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	methods := Methods(resolver)
	if delegatedMethods != nil {
		methods, err = methods.WithDelegation(delegatedMethods, []auth.Delegator{{
			Account:          auth.Account{Namespace: discoveryNamespace, Name: "actions"},
			CallerNamespaces: []string{discoveryNamespace},
		}}, auth.ServiceAccountUIDReaderFunc(func(_ context.Context, account auth.Account) (string, bool, error) {
			if account != (auth.Account{Namespace: discoveryNamespace, Name: sandboxName}) {
				return "", false, nil
			}
			return executionUID, true, nil
		}), nil)
		if err != nil {
			t.Fatal(err)
		}
	}
	register := func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, service)
	}
	connection := grpcmethods.Serve(t, methods, register)
	return environmentpb.NewEnvironmentServiceClient(connection)
}

func readerWithObjects(t *testing.T, objects ...client.Object) client.WithWatch {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := workloadapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := policyapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	builder := clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...)
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

type identityChangeGuardFake struct {
	identity *identitypb.Identity
	err      error
}

func (g *identityChangeGuardFake) ChangeSandboxIdentity(_ context.Context, _ string, identity *identitypb.Identity, patch func() error) error {
	g.identity = identity
	if g.err != nil {
		return g.err
	}
	return patch()
}

type sessionConflictFake struct{}

func (sessionConflictFake) Error() string {
	return "environment identity is still named by Session test-session"
}
func (sessionConflictFake) ConflictingSessionIDs() []string { return []string{"test-session"} }

func TestIdentityChangeRequiresManagerStoppedSandboxAndSessionGuard(t *testing.T) {
	resource := sandboxObject(sandboxUID)
	resource.Spec.Running = false
	resource.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	resource.Status.StopStage = sandboxapi.StopStageStopped
	identity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: "test-agent", UID: "identity-uid"}}
	objects := readerWithObjects(t, resource, executionAccountObject(sandboxUID), identity)
	guard := &identityChangeGuardFake{}
	rpc := serveDiscoveryWithOptions(t, objects, runtimeconfig.Catalog{}, nil, nil, guard)
	request := &environmentpb.SetEnvironmentIdentityRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}, IdentityName: "test-agent",
	}
	if _, err := rpc.SetEnvironmentIdentity(bearing(t, matchingToken), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ungranted caller = %v", err)
	}
	stale := proto.Clone(request).(*environmentpb.SetEnvironmentIdentityRequest)
	stale.Target.Uid = "old-sandbox-uid"
	if _, err := rpc.SetEnvironmentIdentity(bearing(t, actionsToken), stale); status.Code(err) != codes.NotFound {
		t.Fatalf("stale target = %v", err)
	}
	guard.err = sessionConflictFake{}
	if _, err := rpc.SetEnvironmentIdentity(bearing(t, actionsToken), request); status.Code(err) != codes.FailedPrecondition || status.Convert(err).Message() != guard.err.Error() {
		t.Fatalf("Session refusal = %v", err)
	}
	guard.err = nil
	if _, err := rpc.SetEnvironmentIdentity(bearing(t, actionsToken), request); err != nil {
		t.Fatal(err)
	}
	stored := &sandboxapi.Sandbox{}
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, stored); err != nil {
		t.Fatal(err)
	}
	if stored.Spec.Identity == nil || stored.Spec.Identity.Name != "test-agent" || guard.identity.GetUid() != "identity-uid" {
		t.Fatalf("saved identity = %v, pinned guard identity = %v", stored.Spec.Identity, guard.identity)
	}
	request.IdentityName = ""
	if _, err := rpc.SetEnvironmentIdentity(bearing(t, actionsToken), request); err != nil {
		t.Fatal(err)
	}
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, stored); err != nil || stored.Spec.Identity != nil {
		t.Fatalf("clear identity = %v, %v", stored.Spec.Identity, err)
	}
	stored.Spec.Running = true
	stored.Status.OperatingMode = sandboxapi.OperatingModeRunning
	stored.Status.StopStage = sandboxapi.StopStageQuiescing
	if err := objects.Update(t.Context(), stored); err != nil {
		t.Fatal(err)
	}
	request.IdentityName = "test-agent"
	if _, err := rpc.SetEnvironmentIdentity(bearing(t, actionsToken), request); status.Code(err) != codes.FailedPrecondition || !strings.Contains(status.Convert(err).Message(), "spec.identity changes only while nothing runs") {
		t.Fatalf("running Sandbox mutation = %v", err)
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
		{name: "service account subject", record: &workloadapi.WorkloadCredential{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: sandboxName, OwnerReferences: []metav1.OwnerReference{owner}}, Spec: workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{ServiceAccount: sandboxName}}}},
		{name: "prior Sandbox owner", record: &workloadapi.WorkloadCredential{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: sandboxName, OwnerReferences: []metav1.OwnerReference{{APIVersion: owner.APIVersion, Kind: owner.Kind, Name: owner.Name, UID: "old-sandbox-uid"}}}, Spec: workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: "old-agent", UID: "old-identity-uid"}}}}},
		{name: "noncontroller reference", record: &workloadapi.WorkloadCredential{ObjectMeta: metav1.ObjectMeta{Namespace: discoveryNamespace, Name: sandboxName, OwnerReferences: []metav1.OwnerReference{{APIVersion: owner.APIVersion, Kind: owner.Kind, Name: owner.Name, UID: owner.UID}}}, Spec: workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: "other-agent", UID: "other-identity-uid"}}}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			objects := []client.Object{sandboxObject(sandboxUID), executionAccountObject(sandboxUID)}
			if test.record != nil {
				objects = append(objects, test.record)
			}
			rpc := serveDiscovery(t, readerWithObjects(t, objects...), runtimeconfig.Catalog{})
			got, err := rpc.GetEnvironmentIdentityRecord(bearing(t, matchingToken), request)
			if err != nil || got.GetIdentity().GetName() != test.expected {
				t.Fatalf("identity record = %v, %v; want %q", got, err, test.expected)
			}
			stale := proto.Clone(request).(*environmentpb.GetEnvironmentRequest)
			stale.Target.Uid = "old-sandbox-uid"
			if _, err := rpc.GetEnvironmentIdentityRecord(bearing(t, matchingToken), stale); status.Code(err) != codes.NotFound {
				t.Fatalf("stale target = %v", err)
			}
		})
	}
}

func bearing(t *testing.T, token string) context.Context {
	t.Helper()
	return metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer "+token)
}

func delegatedBearing(t *testing.T, token string, account auth.Account, uid string) context.Context {
	t.Helper()
	envelope := &delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Caller{Caller: &identitypb.ServiceAccountIdentity{
			Account: &identitypb.ServiceAccount{Namespace: account.Namespace, Name: account.Name}, Uid: uid,
		}},
		Provenance: &delegationpb.Delegation_Action{Action: &delegationpb.ActionProvenance{RequestId: actionRequestID, ExecutionId: actionExecutionID}},
	}
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		t.Fatal(err)
	}
	return metadata.NewOutgoingContext(t.Context(), metadata.Pairs(
		"authorization", "Agentplane-Delegated "+token,
		auth.DelegationMetadataKey, string(encoded),
	))
}

func TestManagementRequiresDirectGrantedPodAndFencesTarget(t *testing.T) {
	objects := baseReader(t)
	method := environmentpb.EnvironmentService_SetEnvironmentWakeOnSend_FullMethodName
	rpc := serveDiscoveryWithDelegation(t, objects, runtimeconfig.Catalog{}, []string{method})
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	request := &environmentpb.SetEnvironmentWakeOnSendRequest{Target: target, WakeOnSend: true}
	if _, err := rpc.SetEnvironmentWakeOnSend(bearing(t, matchingToken), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("account-only caller: got %v", err)
	}
	actor := auth.Account{Namespace: discoveryNamespace, Name: sandboxName}
	if _, err := rpc.SetEnvironmentWakeOnSend(delegatedBearing(t, actionsToken, actor, executionUID), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("delegated caller borrowed management grant: got %v", err)
	}
	stale := proto.Clone(request).(*environmentpb.SetEnvironmentWakeOnSendRequest)
	stale.Target.Uid = "old-uid"
	if _, err := rpc.SetEnvironmentWakeOnSend(bearing(t, actionsToken), stale); status.Code(err) != codes.NotFound {
		t.Fatalf("stale target: got %v", err)
	}
	if _, err := rpc.SetEnvironmentWakeOnSend(bearing(t, actionsToken), request); err != nil {
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
	if _, err := rpc.CreateEnvironment(bearing(t, actionsToken), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("manager supplied broker origin: %v", err)
	}
	if _, err := rpc.CreateEnvironment(bearing(t, launcherToken), &environmentpb.CreateEnvironmentRequest{Name: "created", Template: "test"}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("launcher omitted broker origin: %v", err)
	}
	if _, err := rpc.SuspendEnvironment(bearing(t, launcherToken), &environmentpb.EnvironmentMutationRequest{Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("launcher suspended environment: %v", err)
	}
}

func TestManagementRejectsUnrepresentableDuration(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	tooLarge := &durationpb.Duration{Seconds: 315576000000}
	_, err := rpc.SetEnvironmentIdleSuspend(bearing(t, actionsToken), &environmentpb.SetEnvironmentIdleSuspendRequest{Target: target, IdleSuspendAfter: tooLarge})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("unrepresentable duration: got %v", err)
	}
	_, err = rpc.CreateEnvironment(bearing(t, actionsToken), &environmentpb.CreateEnvironmentRequest{Name: "overflow", Template: "runner-vm", IdleSuspendAfter: tooLarge})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("unrepresentable create duration: got %v", err)
	}
}

func TestManagementTemplatesNeedManagerGrant(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	if _, err := rpc.ListEnvironmentTemplates(bearing(t, launcherToken), &emptypb.Empty{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("launcher listed templates: %v", err)
	}
}

func TestListAndGetReturnTheExactExecutionAccountIdentity(t *testing.T) {
	objects := baseReader(t)
	rpc := serveDiscovery(t, objects, runtimeconfig.Catalog{})
	listed, err := rpc.ListEnvironments(bearing(t, matchingToken), &environmentpb.ListEnvironmentsRequest{Limit: 10})
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
	got, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{Target: item.Target})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetTarget().GetUid() != sandboxUID || got.GetExecutionAccount().GetUid() != executionUID {
		t.Fatalf("GetEnvironment returned target/account %+v / %+v", got.GetTarget(), got.GetExecutionAccount())
	}
	if got.GetActions().GetResumeEligibility().GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE ||
		got.GetActions().GetResumeEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_OWN_SERVICE_ACCOUNT {
		t.Fatalf("explicit resume eligibility = %+v", got.GetActions().GetResumeEligibility())
	}
	if got.GetActions().GetWakeGeneration() != 0 || got.GetActions().GetResumeToken() != lifecycle.NewResumeToken(&lifecycle.Target{
		Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID,
	}, 0) {
		t.Fatalf("generation-bound resume precondition = generation %d, token %q", got.GetActions().GetWakeGeneration(), got.GetActions().GetResumeToken())
	}
	var unchanged sandboxapi.Sandbox
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, &unchanged); err != nil {
		t.Fatal(err)
	}
	if !unchanged.Spec.Running || unchanged.Spec.WakeGeneration != 0 {
		t.Fatalf("discovery changed Sandbox desired state: running=%t generation=%d", unchanged.Spec.Running, unchanged.Spec.WakeGeneration)
	}
}

func TestDiscoverResumeAndReadThroughOneService(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Spec.WakeGeneration = 7
	sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	sandbox.Status.StopStage = sandboxapi.StopStageStopped
	by := sandboxapi.SuspendedByIdle
	sandbox.Status.SuspendedBy = &by
	sandbox.Status.SuspendedAtWakeGeneration = proto.Int64(7)
	objects := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID))
	rpc := serveDiscovery(t, objects, runtimeconfig.Catalog{})
	ctx := bearing(t, matchingToken)
	listed, err := rpc.ListEnvironments(ctx, &environmentpb.ListEnvironmentsRequest{Limit: 1})
	if err != nil || len(listed.GetItems()) != 1 {
		t.Fatalf("ListEnvironments = %+v, %v", listed, err)
	}
	descriptor := listed.Items[0]
	resumed, err := rpc.ResumeEnvironment(ctx, &environmentpb.ResumeEnvironmentRequest{
		Target: descriptor.Target, ResumeToken: descriptor.Actions.ResumeToken,
	})
	if err != nil || resumed.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED || resumed.GetWakeGeneration() != 8 {
		t.Fatalf("ResumeEnvironment = %+v, %v", resumed, err)
	}
	current, err := rpc.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: descriptor.Target})
	if err != nil || current.GetActions().GetWakeGeneration() != 8 || current.GetActions().GetResumeToken() == descriptor.GetActions().GetResumeToken() {
		t.Fatalf("GetEnvironment after resume = %+v, %v", current, err)
	}
	if current.GetObservedState().GetReadiness().GetState() != environmentpb.ReadinessState_READINESS_STATE_NOT_READY {
		t.Fatalf("resume acceptance invented readiness: %+v", current.GetObservedState().GetReadiness())
	}
}

func TestDiscoveryReportsResumeDenialAndReadinessFactsWithoutEffects(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Spec.ResumePolicy = &sandboxapi.ResumePolicy{AllowOwnServiceAccount: false}
	objects := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID))
	rpc := serveDiscovery(t, objects, runtimeconfig.Catalog{})
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	got, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
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
		got.GetActions().GetResumeEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED || got.GetActions().GetResumeToken() != "" {
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

func TestLegacyWakeOnSendAvailabilityIsSeparateFromExplicitResumeEligibility(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Spec.WakeOnSend = true
	sandbox.Spec.ResumePolicy = &sandboxapi.ResumePolicy{AllowOwnServiceAccount: false}
	sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	sandbox.Status.StopStage = sandboxapi.StopStageStopped
	objects := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID))
	rpc := serveDiscoveryWithOptions(t, objects, runtimeconfig.Catalog{}, nil, []auth.Account{{
		Namespace: discoveryNamespace, Name: sandboxName,
	}}, nil)
	got, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetActions().GetAutomaticWake() != environmentpb.AutomaticWakeState_AUTOMATIC_WAKE_STATE_AVAILABLE {
		t.Fatalf("automatic wake projection = %s, want broker-enabled availability", got.GetActions().GetAutomaticWake())
	}
	if got.GetActions().GetResumeEligibility().GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED ||
		got.GetActions().GetResumeEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED ||
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
		Message: "runner is accepting work", LastTransitionTime: createdAt,
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
	rpc := serveDiscovery(t, objects, runtimeconfig.Catalog{})
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	got, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{Target: target})
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
	if _, err := rpc.GetEnvironment(bearing(t, foreignToken), &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.NotFound {
		t.Fatalf("foreign caller GetEnvironment error = %v, want NotFound", err)
	}
}

func TestGetDoesNotProjectAReplacedPodWithTheSameName(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: "runner", UID: "pod-uid-expected"}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: discoveryNamespace, Name: "runner", UID: "pod-uid-replacement",
	}}
	rpc := serveDiscovery(t, readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID), pod), runtimeconfig.Catalog{})
	got, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetKubernetesDiagnostics().GetPod() != nil || got.GetObservedState().GetRuntimeIncarnationUid() != "pod-uid-expected" {
		t.Fatalf("replacement Pod was projected as current: Pod=%v incarnationUID=%q", got.GetKubernetesDiagnostics().GetPod(), got.GetObservedState().GetRuntimeIncarnationUid())
	}
}

func TestDelegatedActionsUseTheEffectiveCallerAndKeepTargetUIDFences(t *testing.T) {
	rpc := serveDiscoveryWithDelegation(t, baseReader(t), runtimeconfig.Catalog{}, []string{
		environmentpb.EnvironmentService_ListEnvironments_FullMethodName,
		environmentpb.EnvironmentService_GetEnvironment_FullMethodName,
	})
	effective := auth.Account{Namespace: discoveryNamespace, Name: sandboxName}
	ctx := delegatedBearing(t, actionsToken, effective, executionUID)

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
	if got.GetExecutionAccount().GetUid() != executionUID {
		t.Fatalf("delegated GetEnvironment execution account = %+v", got.GetExecutionAccount())
	}
	if got.GetActions().GetResumeEligibility().GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE ||
		got.GetActions().GetResumeEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_OWN_SERVICE_ACCOUNT {
		t.Fatalf("delegated resume eligibility used the actor instead of effective caller: %+v", got.GetActions().GetResumeEligibility())
	}

	for _, target := range []*identitypb.EnvironmentTarget{
		{Namespace: foreignNamespace, Name: sandboxName, Uid: sandboxUID},
		{Namespace: discoveryNamespace, Name: sandboxName, Uid: "replaced-sandbox-uid"},
	} {
		if _, err := rpc.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.NotFound {
			t.Errorf("delegated GetEnvironment target %+v error = %v, want NotFound", target, err)
		}
	}

	// Existing projected ServiceAccount callers keep the direct mode on the same RPC methods.
	direct, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if err != nil || direct.GetTarget().GetUid() != sandboxUID {
		t.Fatalf("direct GetEnvironment = %+v, %v", direct, err)
	}
}

func TestDelegatedDiscoveryRejectsUntrustedActorAndReplacedCallerUID(t *testing.T) {
	rpc := serveDiscoveryWithDelegation(t, baseReader(t), runtimeconfig.Catalog{}, []string{
		environmentpb.EnvironmentService_ListEnvironments_FullMethodName,
		environmentpb.EnvironmentService_GetEnvironment_FullMethodName,
	})
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	for _, test := range []struct {
		name string
		ctx  context.Context
		code codes.Code
	}{
		{
			name: "untrusted actor",
			ctx:  delegatedBearing(t, untrustedToken, auth.Account{Namespace: discoveryNamespace, Name: sandboxName}, executionUID),
			code: codes.PermissionDenied,
		},
		{
			name: "replaced effective ServiceAccount UID",
			ctx:  delegatedBearing(t, actionsToken, auth.Account{Namespace: discoveryNamespace, Name: sandboxName}, "old-service-account-uid"),
			code: codes.PermissionDenied,
		},
		{
			name: "caller namespace outside delegator scope",
			ctx:  delegatedBearing(t, actionsToken, auth.Account{Namespace: foreignNamespace, Name: sandboxName}, executionUID),
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
	valid := delegatedBearing(t, actionsToken, auth.Account{Namespace: discoveryNamespace, Name: sandboxName}, executionUID)
	validMetadata, _ := metadata.FromOutgoingContext(valid)
	encodedEnvelope := validMetadata.Get(auth.DelegationMetadataKey)[0]
	for _, test := range []struct {
		name string
		ctx  context.Context
	}{
		{
			name: "missing envelope",
			ctx:  metadata.NewOutgoingContext(t.Context(), metadata.Pairs("authorization", "Agentplane-Delegated "+actionsToken)),
		},
		{
			name: "malformed envelope",
			ctx: metadata.NewOutgoingContext(t.Context(), metadata.Pairs(
				"authorization", "Agentplane-Delegated "+actionsToken,
				auth.DelegationMetadataKey, "\x00",
			)),
		},
		{
			name: "repeated envelope",
			ctx: metadata.NewOutgoingContext(t.Context(), metadata.Pairs(
				"authorization", "Agentplane-Delegated "+actionsToken,
				auth.DelegationMetadataKey, encodedEnvelope,
				auth.DelegationMetadataKey, encodedEnvelope,
			)),
		},
		{
			name: "bearer mode with envelope",
			ctx: metadata.NewOutgoingContext(t.Context(), metadata.Pairs(
				"authorization", "Bearer "+matchingToken,
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
	if _, err := listOnly.GetEnvironment(valid, &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("delegated GetEnvironment without method opt-in error = %v, want Unauthenticated", err)
	}
	if _, err := rpc.ResumeEnvironment(valid, &environmentpb.ResumeEnvironmentRequest{Target: target}); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("delegated reads granted resume without method opt-in: %v", err)
	}
}

func TestRecreatedAndForeignAccountsCannotDiscoverTheSandbox(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	for _, test := range []struct {
		name  string
		token string
	}{
		{name: "same namespace and name but replaced ServiceAccount UID", token: recreatedToken},
		{name: "foreign namespace", token: foreignToken},
	} {
		t.Run(test.name, func(t *testing.T) {
			listed, err := rpc.ListEnvironments(bearing(t, test.token), &environmentpb.ListEnvironmentsRequest{Limit: 10})
			if err != nil {
				t.Fatal(err)
			}
			if len(listed.Items) != 0 {
				t.Fatalf("ListEnvironments returned unauthorized items: %+v", listed.Items)
			}
			if _, err := rpc.GetEnvironment(bearing(t, test.token), &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.NotFound {
				t.Fatalf("GetEnvironment error = %v, want NotFound", err)
			}
		})
	}
}

func TestGetUsesTheSameNotFoundForMissingAndUnauthorizedTargets(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	var want *status.Status
	for _, test := range []struct {
		name   string
		token  string
		target *identitypb.EnvironmentTarget
	}{
		{name: "missing name", token: matchingToken, target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: "missing", Uid: "missing-uid"}},
		{name: "wrong ServiceAccount UID", token: recreatedToken, target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}},
		{name: "foreign caller", token: foreignToken, target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, err := rpc.GetEnvironment(bearing(t, test.token), &environmentpb.GetEnvironmentRequest{Target: test.target})
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
	}{
		{name: "stale UID", sandbox: sandboxObject(sandboxUID), requested: "old-sandbox-uid", wantListed: 1},
		{name: "deleting Sandbox", sandbox: deletingSandbox(), requested: sandboxUID},
	} {
		t.Run(test.name, func(t *testing.T) {
			reader := readerWithObjects(t, test.sandbox, executionAccountObject(test.sandbox.UID))
			rpc := serveDiscovery(t, reader, runtimeconfig.Catalog{})
			_, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
				Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: test.requested},
			})
			if status.Code(err) != codes.NotFound {
				t.Fatalf("GetEnvironment error = %v, want NotFound", err)
			}
			listed, err := rpc.ListEnvironments(bearing(t, matchingToken), &environmentpb.ListEnvironmentsRequest{Limit: 10})
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

func TestDiscoveryRequiresControllerOwnershipProof(t *testing.T) {
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
			rpc := serveDiscovery(t, readerWithObjects(t, objects...), runtimeconfig.Catalog{})
			_, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
				Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
			})
			if status.Code(err) != codes.NotFound {
				t.Fatalf("GetEnvironment error = %v, want NotFound", err)
			}
			listed, err := rpc.ListEnvironments(bearing(t, matchingToken), &environmentpb.ListEnvironmentsRequest{Limit: 10})
			if err != nil {
				t.Fatal(err)
			}
			if len(listed.Items) != 0 {
				t.Fatalf("ListEnvironments returned Sandbox without ownership proof: %+v", listed.Items)
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
		if _, err := rpc.ListEnvironments(bearing(t, matchingToken), &environmentpb.ListEnvironmentsRequest{Limit: limit}); status.Code(err) != codes.InvalidArgument {
			t.Errorf("limit %d error = %v, want InvalidArgument", limit, err)
		}
	}
	if _, err := rpc.ListEnvironments(bearing(t, matchingToken), &environmentpb.ListEnvironmentsRequest{Limit: 10, PageToken: "next-page"}); status.Code(err) != codes.InvalidArgument {
		t.Errorf("page token error = %v, want InvalidArgument", err)
	}
	for _, target := range []*identitypb.EnvironmentTarget{
		nil,
		{Namespace: discoveryNamespace, Name: sandboxName},
		{Namespace: discoveryNamespace, Uid: sandboxUID},
		{Name: sandboxName, Uid: sandboxUID},
	} {
		if _, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.InvalidArgument {
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
		if r.failAccountRead {
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
					rpc := serveDiscovery(t, test.reader(t), runtimeconfig.Catalog{})
					var err error
					switch operation {
					case "GetEnvironment":
						_, err = rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
							Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
						})
					case "ListEnvironments":
						_, err = rpc.ListEnvironments(bearing(t, matchingToken), &environmentpb.ListEnvironmentsRequest{Limit: 10})
					}
					if status.Code(err) != codes.Unavailable {
						t.Fatalf("%s error = %v, want Unavailable", operation, err)
					}
				})
			}
		})
	}
}

func TestEveryGeneratedRPCRequiresReviewedServiceAccountAdmission(t *testing.T) {
	methods := func(t *testing.T) auth.Methods {
		t.Helper()
		resolver, err := auth.NewResolver(testTokenReviews(), dispatcherAudience, []string{discoveryNamespace, foreignNamespace}, auth.WithVerdictTTL(0))
		if err != nil {
			t.Fatal(err)
		}
		return Methods(resolver)
	}
	register := func(server *grpc.Server) {
		objects := baseReader(t)
		service, err := New(discoveryNamespace, objects, runtimeconfig.Catalog{}, discoveryLifecycle(t, objects, nil), nil, ManagementOptions{})
		if err != nil {
			t.Fatal(err)
		}
		environmentpb.RegisterEnvironmentServiceServer(server, service)
	}
	configured := methods(t)
	connection := grpcmethods.Serve(t, configured, register)
	for _, method := range grpcmethods.Of(environmentpb.EnvironmentService_ServiceDesc) {
		t.Run(method.Name, func(t *testing.T) {
			want := codes.InvalidArgument
			switch method.Name {
			case environmentpb.EnvironmentService_WatchEnvironments_FullMethodName:
				want = codes.OK
			case environmentpb.EnvironmentService_ListEnvironmentTemplates_FullMethodName,
				environmentpb.EnvironmentService_CreateEnvironment_FullMethodName,
				environmentpb.EnvironmentService_SuspendEnvironment_FullMethodName,
				environmentpb.EnvironmentService_DeleteEnvironment_FullMethodName,
				environmentpb.EnvironmentService_SetEnvironmentWakeOnSend_FullMethodName,
				environmentpb.EnvironmentService_SetEnvironmentIdleSuspend_FullMethodName,
				environmentpb.EnvironmentService_SetEnvironmentIdentity_FullMethodName:
				want = codes.PermissionDenied
			}
			if got := status.Code(method.Call(bearing(t, matchingToken), connection)); got != want {
				t.Errorf("account-only reviewed caller reached %s with code %v, want %v", method.Name, got, want)
			}
			if got := status.Code(method.Call(bearing(t, "unreviewed-token"), connection)); got != codes.Unauthenticated {
				t.Errorf("unreviewed caller reached %s with code %v, want Unauthenticated", method.Name, got)
			}
			unlisted := maps.Clone(configured)
			delete(unlisted, method.Name)
			if got := status.Code(method.Call(bearing(t, matchingToken), grpcmethods.Serve(t, unlisted, register))); got != codes.PermissionDenied {
				t.Errorf("unlisted RPC %s with reviewed caller got %v, want PermissionDenied", method.Name, got)
			}
		})
	}
}

func TestInventoryReaderListAndGetAreBroadButOrdinaryCallersStayScoped(t *testing.T) {
	other := sandboxObject("other-uid")
	other.Name = "other"
	terminating := sandboxObject("deleting-uid")
	terminating.Name = "deleting"
	deletion := metav1.Now()
	terminating.DeletionTimestamp = &deletion
	terminating.Finalizers = []string{"test.example/retain"}
	terminating.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	objects := readerWithObjects(t, sandboxObject(sandboxUID), executionAccountObject(sandboxUID), other, terminating)
	reader := auth.Account{Namespace: discoveryNamespace, Name: "actions"}
	service, err := New(discoveryNamespace, objects, runtimeconfig.Catalog{}, discoveryLifecycle(t, objects, nil), sets.New(reader), ManagementOptions{})
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := auth.NewResolver(testTokenReviews(), dispatcherAudience, []string{discoveryNamespace, foreignNamespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.Serve(t, Methods(resolver), func(server *grpc.Server) { environmentpb.RegisterEnvironmentServiceServer(server, service) })
	rpc := environmentpb.NewEnvironmentServiceClient(connection)
	own, err := rpc.ListEnvironments(bearing(t, matchingToken), &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil || len(own.GetItems()) != 1 || own.GetItems()[0].GetTarget().GetName() != sandboxName {
		t.Fatalf("own list = %v, %v", own, err)
	}
	all, err := rpc.ListEnvironments(bearing(t, actionsToken), &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil || len(all.GetItems()) != 3 {
		t.Fatalf("reader list = %v, %v", all, err)
	}
	for _, target := range []*identitypb.EnvironmentTarget{
		{Namespace: discoveryNamespace, Name: "other", Uid: "other-uid"},
		{Namespace: discoveryNamespace, Name: "deleting", Uid: "deleting-uid"},
	} {
		if _, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.NotFound {
			t.Fatalf("own get = %v", err)
		}
		got, err := rpc.GetEnvironment(bearing(t, actionsToken), &environmentpb.GetEnvironmentRequest{Target: target})
		if err != nil || got.GetTarget().GetName() != target.Name || got.ExecutionAccount != nil {
			t.Fatalf("reader get = %v, %v", got, err)
		}
		if target.Name == "deleting" && (got.GetActions().GetResumeEligibility() != nil || got.GetActions().GetResumeToken() != "" || got.GetActions().GetAutomaticWake() != environmentpb.AutomaticWakeState_AUTOMATIC_WAKE_STATE_NOT_AVAILABLE) {
			t.Fatalf("deleting environment advertises resume: %v", got)
		}
	}
}

func TestDelegatedActorDoesNotLendInventoryReaderGrant(t *testing.T) {
	actor := auth.Account{Namespace: discoveryNamespace, Name: "actions"}
	other := sandboxObject("other-uid")
	other.Name = "other"
	objects := readerWithObjects(t, sandboxObject(sandboxUID), executionAccountObject(sandboxUID), other)
	service, err := New(discoveryNamespace, objects, runtimeconfig.Catalog{}, discoveryLifecycle(t, objects, nil), sets.New(actor), ManagementOptions{})
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := auth.NewResolver(testTokenReviews(), dispatcherAudience, []string{discoveryNamespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	methods, err := Methods(resolver).WithDelegation([]string{environmentpb.EnvironmentService_ListEnvironments_FullMethodName}, []auth.Delegator{{Account: actor, CallerNamespaces: []string{discoveryNamespace}}}, auth.ServiceAccountUIDReaderFunc(func(_ context.Context, _ auth.Account) (string, bool, error) { return executionUID, true, nil }), nil)
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.Serve(t, methods, func(server *grpc.Server) { environmentpb.RegisterEnvironmentServiceServer(server, service) })
	rpc := environmentpb.NewEnvironmentServiceClient(connection)
	effective := auth.Account{Namespace: discoveryNamespace, Name: sandboxName}
	response, err := rpc.ListEnvironments(delegatedBearing(t, actionsToken, effective, executionUID), &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil || len(response.GetItems()) != 1 || response.GetItems()[0].GetTarget().GetName() != sandboxName {
		t.Fatalf("delegated list = %v, %v", response, err)
	}
}

func TestCollectionSnapshotSurvivesDeletionDuringEligibilityRead(t *testing.T) {
	reader := &missingDuringEligibility{WithWatch: baseReader(t)}
	rpc := serveDiscovery(t, reader, runtimeconfig.Catalog{})
	stream, err := rpc.WatchEnvironments(bearing(t, matchingToken), &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	frame, err := stream.Recv()
	if err != nil || frame.GetSnapshotComplete() == nil {
		t.Fatalf("snapshot completion during deletion = %v, %v", frame, err)
	}
	if reader.sandboxGets != 4 {
		t.Fatalf("Sandbox reads = %d, want the eligibility race to be exercised", reader.sandboxGets)
	}
}

func TestGetFencesSandboxReplacementDuringCallerAuthorization(t *testing.T) {
	replacement := sandboxObject("replacement-sandbox-uid")
	reader := &replaceSandboxOnAuthorizationRead{
		WithWatch:   baseReader(t),
		replacement: replacement,
	}
	rpc := serveDiscovery(t, reader, runtimeconfig.Catalog{})
	_, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("GetEnvironment during same-name replacement = %v, want NotFound", err)
	}
	if reader.sandboxGets != 2 {
		t.Fatalf("Sandbox reads during authorization = %d, want initial read and exact-UID recheck", reader.sandboxGets)
	}
}

func TestGetRechecksExecutionAccountAfterPodProjection(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: "runner", UID: "pod-uid-current"}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: discoveryNamespace, Name: "runner", UID: "pod-uid-current",
	}}
	base := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID), pod)
	reader := &removeOwnerAfterPodRead{WithWatch: base}
	rpc := serveDiscovery(t, reader, runtimeconfig.Catalog{})
	_, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if status.Code(err) != codes.NotFound {
		t.Fatalf("GetEnvironment after owner changed during Pod projection = %v, want NotFound", err)
	}
	if !reader.removed {
		t.Fatal("test did not remove the execution account owner during projection")
	}
}

func TestGetUsesRevalidatedIdentityAfterPodProjection(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: "before-pod-read"}
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: "runner", UID: "pod-uid-current"}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Namespace: discoveryNamespace, Name: "runner", UID: "pod-uid-current",
	}}
	base := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID), pod)
	reader := &changeIdentityAfterPodRead{
		WithWatch: base,
		identity:  &sandboxapi.IdentityRef{Name: "after-pod-read"},
	}
	rpc := serveDiscovery(t, reader, runtimeconfig.Catalog{})
	descriptor, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
		Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !reader.changed || descriptor.GetIdentityName() != "after-pod-read" {
		t.Fatalf("GetEnvironment identity_name = %q, changed=%t; want after-pod-read from revalidation", descriptor.GetIdentityName(), reader.changed)
	}
}

type replaceSandboxOnAuthorizationRead struct {
	client.WithWatch
	replacement *sandboxapi.Sandbox
	sandboxGets int
}

// Simulate a deletion between the collection's initial read and the lifecycle manager's
// independent eligibility read. The watch must deliver a complete snapshot instead of failing.
type missingDuringEligibility struct {
	client.WithWatch
	sandboxGets int
}

func (r *missingDuringEligibility) Get(ctx context.Context, key types.NamespacedName, object client.Object, options ...client.GetOption) error {
	if _, ok := object.(*sandboxapi.Sandbox); ok {
		r.sandboxGets++
		if r.sandboxGets == 4 {
			return apierrors.NewNotFound(sandboxapi.GroupVersion.WithResource("sandboxes").GroupResource(), key.Name)
		}
	}
	return r.WithWatch.Get(ctx, key, object, options...)
}

func (r *replaceSandboxOnAuthorizationRead) Get(ctx context.Context, key types.NamespacedName, object client.Object, options ...client.GetOption) error {
	if sandbox, ok := object.(*sandboxapi.Sandbox); ok {
		r.sandboxGets++
		if r.sandboxGets == 2 {
			r.replacement.DeepCopyInto(sandbox)
			return nil
		}
	}
	return r.WithWatch.Get(ctx, key, object, options...)
}

type removeOwnerAfterPodRead struct {
	client.WithWatch
	removed bool
}

type changeIdentityAfterPodRead struct {
	client.WithWatch
	identity *sandboxapi.IdentityRef
	changed  bool
}

func (r *changeIdentityAfterPodRead) Get(ctx context.Context, key types.NamespacedName, object client.Object, options ...client.GetOption) error {
	if _, ok := object.(*corev1.Pod); ok {
		if err := r.WithWatch.Get(ctx, key, object, options...); err != nil {
			return err
		}
		if !r.changed {
			r.changed = true
			var sandbox sandboxapi.Sandbox
			if err := r.WithWatch.Get(ctx, types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, &sandbox); err != nil {
				return err
			}
			sandbox.Spec.Identity = r.identity
			if err := r.WithWatch.Update(ctx, &sandbox); err != nil {
				return err
			}
		}
		return nil
	}
	return r.WithWatch.Get(ctx, key, object, options...)
}

func (r *removeOwnerAfterPodRead) Get(ctx context.Context, key types.NamespacedName, object client.Object, options ...client.GetOption) error {
	if _, ok := object.(*corev1.Pod); ok {
		if err := r.WithWatch.Get(ctx, key, object, options...); err != nil {
			return err
		}
		if !r.removed {
			r.removed = true
			var account corev1.ServiceAccount
			if err := r.WithWatch.Get(ctx, types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, &account); err != nil {
				return err
			}
			account.OwnerReferences = nil
			if err := r.WithWatch.Update(ctx, &account); err != nil {
				return err
			}
		}
		return nil
	}
	return r.WithWatch.Get(ctx, key, object, options...)
}
