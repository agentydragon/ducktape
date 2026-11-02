package environments

import (
	"context"
	"errors"
	"maps"
	"testing"
	"time"

	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
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

func (unusedWakeBudget) ReserveWakeAttempt(context.Context, lifecycle.ServiceAccount, string, int64) (time.Duration, error) {
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
	return serveDiscoveryWithOptions(t, objects, catalog, delegatedMethods, nil)
}

func serveDiscoveryWithOptions(
	t *testing.T,
	objects client.WithWatch,
	catalog runtimeconfig.Catalog,
	delegatedMethods []string,
	wakeCallers []auth.Account,
) environmentpb.EnvironmentServiceClient {
	t.Helper()
	service, err := New(discoveryNamespace, objects, catalog, discoveryLifecycle(t, objects, wakeCallers), nil, ManagementOptions{
		Managers:  map[auth.Account]struct{}{{Namespace: discoveryNamespace, Name: "actions"}: {}},
		Launchers: map[auth.Account]struct{}{{Namespace: discoveryNamespace, Name: "launcher"}: {}},
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
		}))
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

func bearing(t *testing.T, token string) context.Context {
	t.Helper()
	return metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer "+token)
}

func delegatedBearing(t *testing.T, token string, account auth.Account, uid string) context.Context {
	t.Helper()
	envelope := &delegationpb.Delegation{
		Caller: &delegationpb.ServiceAccountIdentity{
			Account: &sandboxpb.ServiceAccount{Namespace: account.Namespace, Name: account.Name}, Uid: uid,
		},
		ActionRequestId: actionRequestID, ActionExecutionId: actionExecutionID,
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
	target := &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
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
	origin := &sandboxpb.SandboxLaunchOrigin{Caller: &sandboxpb.ServiceAccount{Namespace: discoveryNamespace, Name: sandboxName}, ActionGroup: "orchestration_v1"}
	request := &sandboxpb.CreateSandboxRequest{Name: "created", Template: "test", LaunchOrigin: origin}
	if _, err := rpc.CreateEnvironment(bearing(t, actionsToken), request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("manager supplied broker origin: %v", err)
	}
	if _, err := rpc.CreateEnvironment(bearing(t, launcherToken), &sandboxpb.CreateSandboxRequest{Name: "created", Template: "test"}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("launcher omitted broker origin: %v", err)
	}
	if _, err := rpc.SuspendEnvironment(bearing(t, launcherToken), &environmentpb.EnvironmentMutationRequest{Target: &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("launcher suspended environment: %v", err)
	}
}

func TestManagementRejectsUnrepresentableDuration(t *testing.T) {
	rpc := serveDiscovery(t, baseReader(t), runtimeconfig.Catalog{})
	target := &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	tooLarge := &durationpb.Duration{Seconds: 315576000000}
	_, err := rpc.SetEnvironmentIdleSuspend(bearing(t, actionsToken), &environmentpb.SetEnvironmentIdleSuspendRequest{Target: target, IdleSuspendAfter: tooLarge})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("unrepresentable duration: got %v", err)
	}
	_, err = rpc.CreateEnvironment(bearing(t, actionsToken), &sandboxpb.CreateSandboxRequest{Name: "overflow", Template: "runner-vm", IdleSuspendAfter: tooLarge})
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
	if item.ExecutionAccount.GetNamespace() != discoveryNamespace || item.ExecutionAccount.GetName() != sandboxName || item.ExecutionAccount.GetUid() != executionUID {
		t.Fatalf("execution account = %+v", item.ExecutionAccount)
	}
	got, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{Target: item.Target})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetTarget().GetUid() != sandboxUID || got.GetExecutionAccount().GetUid() != executionUID {
		t.Fatalf("GetEnvironment returned target/account %+v / %+v", got.GetTarget(), got.GetExecutionAccount())
	}
	if got.GetResumeEligibility().GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE ||
		got.GetResumeEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_OWN_SERVICE_ACCOUNT {
		t.Fatalf("explicit resume eligibility = %+v", got.GetResumeEligibility())
	}
	if got.GetWakeGeneration() != 0 || got.GetResumeToken() != lifecycle.NewResumeToken(lifecycle.Target{
		Namespace: discoveryNamespace, Name: sandboxName, UID: sandboxUID,
	}, 0) {
		t.Fatalf("generation-bound resume precondition = generation %d, token %q", got.GetWakeGeneration(), got.GetResumeToken())
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
		Target: descriptor.Target, ResumeToken: descriptor.ResumeToken,
	})
	if err != nil || resumed.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED || resumed.GetWakeGeneration() != 8 {
		t.Fatalf("ResumeEnvironment = %+v, %v", resumed, err)
	}
	current, err := rpc.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: descriptor.Target})
	if err != nil || current.GetWakeGeneration() != 8 || current.GetResumeToken() == descriptor.GetResumeToken() {
		t.Fatalf("GetEnvironment after resume = %+v, %v", current, err)
	}
	if current.GetReadiness().GetState() != environmentpb.ReadinessState_READINESS_STATE_NOT_READY {
		t.Fatalf("resume acceptance invented readiness: %+v", current.GetReadiness())
	}
}

func TestDiscoveryReportsResumeDenialAndReadinessFactsWithoutEffects(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Spec.ResumePolicy = &sandboxapi.ResumePolicy{AllowOwnServiceAccount: false}
	objects := readerWithObjects(t, sandbox, executionAccountObject(sandbox.UID))
	rpc := serveDiscovery(t, objects, runtimeconfig.Catalog{})
	target := &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	got, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
		Target: target,
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetReadiness().GetState() != environmentpb.ReadinessState_READINESS_STATE_NOT_READY ||
		got.GetReadiness().GetReason() != "ReadyConditionUnavailable" {
		t.Fatalf("current readiness descriptor = %+v", got.GetReadiness())
	}
	if got.GetResumeEligibility().GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED ||
		got.GetResumeEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED || got.GetResumeToken() != "" {
		t.Fatalf("denied resume facts = eligibility %+v token=%q", got.GetResumeEligibility(), got.GetResumeToken())
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
	}})
	got, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
		Target: &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
	})
	if err != nil {
		t.Fatal(err)
	}
	if got.GetResume() != environmentpb.ResumeState_RESUME_STATE_AVAILABLE {
		t.Fatalf("automatic wake projection = %s, want broker-enabled availability", got.GetResume())
	}
	if got.GetResumeEligibility().GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED ||
		got.GetResumeEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED ||
		got.GetResumeToken() != "" {
		t.Fatalf("explicit resume projection = %+v token=%q, want policy denial and no token", got.GetResumeEligibility(), got.GetResumeToken())
	}
	var unchanged sandboxapi.Sandbox
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: discoveryNamespace, Name: sandboxName}, &unchanged); err != nil {
		t.Fatal(err)
	}
	if !unchanged.Spec.Running || unchanged.Spec.WakeGeneration != 0 {
		t.Fatalf("wake assessment changed Sandbox desired state: running=%t generation=%d", unchanged.Spec.Running, unchanged.Spec.WakeGeneration)
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
	if got.GetResumeEligibility().GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE ||
		got.GetResumeEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_OWN_SERVICE_ACCOUNT {
		t.Fatalf("delegated resume eligibility used the actor instead of effective caller: %+v", got.GetResumeEligibility())
	}

	for _, target := range []*environmentpb.TargetHandle{
		{Namespace: foreignNamespace, Name: sandboxName, Uid: sandboxUID},
		{Namespace: discoveryNamespace, Name: sandboxName, Uid: "replaced-sandbox-uid"},
	} {
		if _, err := rpc.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: target}); status.Code(err) != codes.NotFound {
			t.Errorf("delegated GetEnvironment target %+v error = %v, want NotFound", target, err)
		}
	}

	// Existing projected ServiceAccount callers keep the direct mode on the same RPC methods.
	direct, err := rpc.GetEnvironment(bearing(t, matchingToken), &environmentpb.GetEnvironmentRequest{
		Target: &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
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
	target := &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
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
	target := &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
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
	target := &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
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
		target *environmentpb.TargetHandle
	}{
		{name: "missing name", token: matchingToken, target: &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: "missing", Uid: "missing-uid"}},
		{name: "wrong ServiceAccount UID", token: recreatedToken, target: &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}},
		{name: "foreign caller", token: foreignToken, target: &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}},
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
				Target: &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: test.requested},
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
				Target: &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
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
	for _, target := range []*environmentpb.TargetHandle{
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
							Target: &environmentpb.TargetHandle{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID},
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
				environmentpb.EnvironmentService_SetEnvironmentIdleSuspend_FullMethodName:
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
	service, err := New(discoveryNamespace, objects, runtimeconfig.Catalog{}, discoveryLifecycle(t, objects, nil), map[auth.Account]struct{}{reader: {}}, ManagementOptions{})
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
	for _, target := range []*environmentpb.TargetHandle{
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
		if target.Name == "deleting" && (got.ResumeEligibility != nil || got.ResumeToken != "" || got.Resume != environmentpb.ResumeState_RESUME_STATE_NOT_AVAILABLE) {
			t.Fatalf("deleting environment advertises resume: %v", got)
		}
	}
}

func TestDelegatedActorDoesNotLendInventoryReaderGrant(t *testing.T) {
	actor := auth.Account{Namespace: discoveryNamespace, Name: "actions"}
	other := sandboxObject("other-uid")
	other.Name = "other"
	objects := readerWithObjects(t, sandboxObject(sandboxUID), executionAccountObject(sandboxUID), other)
	service, err := New(discoveryNamespace, objects, runtimeconfig.Catalog{}, discoveryLifecycle(t, objects, nil), map[auth.Account]struct{}{actor: {}}, ManagementOptions{})
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := auth.NewResolver(testTokenReviews(), dispatcherAudience, []string{discoveryNamespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	methods, err := Methods(resolver).WithDelegation([]string{environmentpb.EnvironmentService_ListEnvironments_FullMethodName}, []auth.Delegator{{Account: actor, CallerNamespaces: []string{discoveryNamespace}}}, auth.ServiceAccountUIDReaderFunc(func(_ context.Context, _ auth.Account) (string, bool, error) { return executionUID, true, nil }))
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
