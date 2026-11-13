package lifecycle_test

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const (
	testNamespace         = "agentplane"
	testSandbox           = "workspace"
	testSandboxUID        = "sandbox-uid"
	executionUID          = "execution-uid"
	executionIdentityName = "workspace-identity"
	executionIdentityUID  = "workspace-identity-uid"
	operatorUID           = "operator-uid"
	actorUID              = "actor-uid"
	notificationUID       = "notifications-uid"
)

var (
	executionAccount = auth.Account{Namespace: testNamespace, Name: testSandbox}
	operatorAccount  = auth.Account{Namespace: testNamespace, Name: "app"}
	actorAccount     = auth.Account{Namespace: testNamespace, Name: "actions"}
)

type tokenIdentity struct {
	account auth.Account
	uid     string
	podName string
	podUID  string
}

type testReviews map[string]tokenIdentity

func (r testReviews) Create(_ context.Context, request *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	identity, ok := r[request.Spec.Token]
	if !ok {
		return &authenticationv1.TokenReview{}, nil
	}
	extra := map[string]authenticationv1.ExtraValue{}
	if identity.podName != "" {
		extra["authentication.kubernetes.io/pod-name"] = authenticationv1.ExtraValue{identity.podName}
		extra["authentication.kubernetes.io/pod-uid"] = authenticationv1.ExtraValue{identity.podUID}
	}
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true, Audiences: request.Spec.Audiences,
		User: authenticationv1.UserInfo{
			Username: fmt.Sprintf("system:serviceaccount:%s:%s", identity.account.Namespace, identity.account.Name),
			UID:      identity.uid, Extra: extra,
		},
	}}, nil
}

type testWakeBudget struct {
	mu      sync.Mutex
	delay   time.Duration
	err     error
	calls   []budgetCall
	barrier int
	arrived int
	release chan struct{}
}

type budgetCall struct {
	caller lifecycle.WakeBudgetKey
	target string
	gen    int64
}

func (b *testWakeBudget) ReserveWakeAttempt(_ context.Context, caller lifecycle.WakeBudgetKey, target string, generation int64) (time.Duration, error) {
	b.mu.Lock()
	b.calls = append(b.calls, budgetCall{caller: caller, target: target, gen: generation})
	delay, err := b.delay, b.err
	if b.barrier == 0 {
		b.mu.Unlock()
		return delay, err
	}
	b.arrived++
	if b.arrived == b.barrier {
		close(b.release)
	}
	release := b.release
	b.mu.Unlock()
	<-release
	return delay, err
}

type testPatchClient struct {
	client.WithWatch
	patch func(context.Context, client.Object, client.Patch, ...client.PatchOption) error
}

func (c testPatchClient) Patch(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
	if c.patch != nil {
		return c.patch(ctx, object, patch, options...)
	}
	return c.WithWatch.Patch(ctx, object, patch, options...)
}

type lifecycleHarness struct {
	client  client.WithWatch
	service *lifecycle.Manager
	budget  *testWakeBudget
	conn    *grpc.ClientConn
}

func newHarness(t *testing.T, object client.WithWatch, operatorCallers []auth.Account) lifecycleHarness {
	t.Helper()
	budget := &testWakeBudget{}
	service, err := lifecycle.NewManager(lifecycle.Options{
		Namespace: testNamespace, Objects: object, WakeBudget: budget,
		WakeCallers:     []auth.Account{{Namespace: testNamespace, Name: "notifications"}},
		OperatorCallers: operatorCallers,
	})
	if err != nil {
		t.Fatal(err)
	}
	reviews := testReviews{
		"execution":       {account: executionAccount, uid: executionUID, podName: "runner", podUID: "runner-pod"},
		"stale-execution": {account: executionAccount, uid: "old-execution-uid", podName: "runner", podUID: "old-runner-pod"},
		"operator":        {account: operatorAccount, uid: operatorUID, podName: "app", podUID: "app-pod"},
		"actor":           {account: actorAccount, uid: actorUID, podName: "actions", podUID: "actions-pod"},
		"notification":    {account: auth.Account{Namespace: testNamespace, Name: "notifications"}, uid: notificationUID, podName: "notifications", podUID: "notifications-pod"},
	}
	resolver, err := auth.NewResolver(reviews, "lifecycle-test", []string{testNamespace}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	methods := environments.Methods(resolver)
	apiService, err := environments.New(testNamespace, object, runtimeconfig.Catalog{}, service, nil, environments.ManagementOptions{})
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.Serve(t, methods, func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, apiService)
	})
	return lifecycleHarness{client: object, service: service, budget: budget, conn: connection}
}

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := policyapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	return scheme
}

func suspendedSandbox(policy *sandboxapi.ResumePolicy, running bool, by sandboxapi.SuspendedBy, generation int64) *sandboxapi.Sandbox {
	sandbox := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: testSandbox, UID: types.UID(testSandboxUID)},
		Spec:       sandboxapi.SandboxSpec{Template: "runner", Running: running, WakeOnSend: true, ResumePolicy: policy, WakeGeneration: generation},
		Status: sandboxapi.SandboxStatus{
			OperatingMode: sandboxapi.OperatingModeSuspended,
			StopStage:     sandboxapi.StopStageStopped,
			SuspendedBy:   &by,
		},
	}
	if by == sandboxapi.SuspendedByIdle {
		sandbox.Status.SuspendedAtWakeGeneration = proto.Int64(generation)
	}
	return sandbox
}

func testObjects(t *testing.T, sandbox *sandboxapi.Sandbox, executionUID string) client.WithWatch {
	t.Helper()
	controller := true
	account := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: testNamespace, Name: testSandbox, UID: types.UID(executionUID),
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: sandboxapi.GroupVersion.String(), Kind: "Sandbox", Name: testSandbox,
			UID: sandbox.UID, Controller: &controller,
		}},
	}}
	operator := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: testNamespace, Name: operatorAccount.Name, UID: types.UID(operatorUID),
	}}
	notifications := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: testNamespace, Name: "notifications", UID: types.UID(notificationUID),
	}}
	return clientfake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(sandbox, account, operator, notifications).Build()
}

func testIdentityObjects(t *testing.T, sandbox *sandboxapi.Sandbox, executionUID string) client.WithWatch {
	t.Helper()
	sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: executionIdentityName}
	objects := testObjects(t, sandbox, executionUID)
	identity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{
		Namespace: testNamespace, Name: executionIdentityName, UID: types.UID(executionIdentityUID),
	}}
	if err := objects.Create(t.Context(), identity); err != nil {
		t.Fatal(err)
	}
	return objects
}

func targetHandle(uid string) *identitypb.EnvironmentTarget {
	return &identitypb.EnvironmentTarget{Namespace: testNamespace, Name: testSandbox, Uid: uid}
}

func targetToken(uid string, generation int64) string {
	return lifecycle.NewResumeToken(&lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: uid}, generation)
}

func directContext(t *testing.T, token string) context.Context {
	t.Helper()
	return metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer "+token)
}

func delegatedContext(t *testing.T) context.Context {
	t.Helper()
	envelope := &delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: executionIdentityName, Uid: executionIdentityUID}},
		Provenance:      &delegationpb.Delegation_Action{Action: &delegationpb.ActionProvenance{RequestId: uuid.NewString(), ExecutionId: uuid.NewString()}},
	}
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		t.Fatal(err)
	}
	return metadata.NewOutgoingContext(t.Context(), metadata.Pairs(
		"authorization", "Agentplane-Delegated actor", auth.DelegationMetadataKey, string(encoded),
	))
}

func resumeRequest(uid string, generation int64) *environmentpb.ResumeEnvironmentRequest {
	return &environmentpb.ResumeEnvironmentRequest{Target: targetHandle(uid), ResumeToken: targetToken(uid, generation)}
}

func TestOwnExecutionAccountCannotResumeIdleSuspendedSandbox(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 4), executionUID)
	h := newHarness(t, objects, nil)
	response, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 4))
	if status.Code(err) != codes.PermissionDenied || response != nil {
		t.Fatalf("same-name ServiceAccount resume = %+v, %v; want PermissionDenied", response, err)
	}
	var got sandboxapi.Sandbox
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: testNamespace, Name: testSandbox}, &got); err != nil {
		t.Fatal(err)
	}
	if !got.Spec.Running || got.Spec.WakeGeneration != 4 {
		t.Fatalf("denied same-name ServiceAccount changed Sandbox spec = %+v", got.Spec)
	}
}

func TestDelegatedActorDoesNotInheritOperatorAuthority(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 2), executionUID)
	h := newHarness(t, objects, []auth.Account{actorAccount})
	_, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(
		delegatedContext(t), resumeRequest(testSandboxUID, 2),
	)
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("Identity delegation without a lifecycle grant returned %v, want Unauthenticated", err)
	}
}

func TestDelegatedCallerCannotOmitResumeToken(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 1), executionUID)
	h := newHarness(t, objects, []auth.Account{actorAccount})
	request := &environmentpb.ResumeEnvironmentRequest{Target: targetHandle(testSandboxUID)}
	_, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(delegatedContext(t), request)
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("ungranted Identity delegation returned %v, want Unauthenticated", err)
	}
}

func TestLiveExecutionAccountReplacementDeniesOldCallerUID(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 1), "replacement-uid")
	h := newHarness(t, objects, nil)
	_, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(directContext(t, "stale-execution"), resumeRequest(testSandboxUID, 1))
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("old execution identity returned %v, want PermissionDenied", err)
	}
	st, _ := status.FromError(err)
	eligibility, ok := st.Details()[0].(*environmentlifecyclepb.ResumeEligibility)
	if !ok || eligibility.GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_EXECUTION_ACCOUNT_CHANGED {
		t.Fatalf("replacement denial details = %v", st.Details())
	}
}

func TestTargetReplacementAndRequestedStopAreNotOrdinaryResumeTargets(t *testing.T) {
	t.Run("replacement UID", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 1)
		sandbox.UID = "replacement-sandbox-uid"
		objects := testObjects(t, sandbox, executionUID)
		h := newHarness(t, objects, nil)
		_, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 1))
		if status.Code(err) != codes.NotFound {
			t.Fatalf("replacement target returned %v, want NotFound", err)
		}
	})
	t.Run("requested stop is sticky", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, false, sandboxapi.SuspendedByRequested, 1)
		objects := testObjects(t, sandbox, executionUID)
		h := newHarness(t, objects, nil)
		_, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 1))
		if status.Code(err) != codes.PermissionDenied {
			t.Fatalf("ordinary requested-stop resume returned %v, want PermissionDenied", err)
		}
		st, _ := status.FromError(err)
		eligibility, ok := st.Details()[0].(*environmentlifecyclepb.ResumeEligibility)
		if !ok || eligibility.GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP {
			t.Fatalf("requested-stop denial details = %v", st.Details())
		}
	})
	t.Run("recorded Requested status stays sticky during reconciliation", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByRequested, 1)
		objects := testObjects(t, sandbox, executionUID)
		h := newHarness(t, objects, nil)
		_, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 1))
		if status.Code(err) != codes.PermissionDenied {
			t.Fatalf("recorded requested-stop resume returned %v, want PermissionDenied", err)
		}
		st, _ := status.FromError(err)
		eligibility, ok := st.Details()[0].(*environmentlifecyclepb.ResumeEligibility)
		if !ok || eligibility.GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP {
			t.Fatalf("recorded requested-stop denial details = %v", st.Details())
		}
	})
}

func TestDirectPodBoundOperatorCanResumeRequestedStopWithoutToken(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{}, false, sandboxapi.SuspendedByRequested, 7), executionUID)
	h := newHarness(t, objects, []auth.Account{operatorAccount})
	response, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(directContext(t, "operator"), &environmentpb.ResumeEnvironmentRequest{Target: targetHandle(testSandboxUID)})
	if err != nil {
		t.Fatal(err)
	}
	if response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED || response.GetWakeGeneration() != 8 {
		t.Fatalf("operator resume response = %+v", response)
	}
	if response.GetEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_TRUSTED_OPERATOR {
		t.Fatalf("operator was misreported as own-SA eligible: %+v", response.GetEligibility())
	}
}

func TestOldGenerationDoesNotAdvanceASecondWakeAndConcurrentConflictRereads(t *testing.T) {
	t.Run("old token is state changed", func(t *testing.T) {
		objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 3), executionUID)
		h := newHarness(t, objects, []auth.Account{operatorAccount})
		client := environmentpb.NewEnvironmentServiceClient(h.conn)
		first, err := client.ResumeEnvironment(directContext(t, "operator"), resumeRequest(testSandboxUID, 3))
		if err != nil || first.GetWakeGeneration() != 4 {
			t.Fatalf("first resume = %+v, %v", first, err)
		}
		second, err := client.ResumeEnvironment(directContext(t, "operator"), resumeRequest(testSandboxUID, 3))
		if err != nil || second.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED || second.GetWakeGeneration() != 4 {
			t.Fatalf("stale resume = %+v, %v", second, err)
		}
		if len(h.budget.calls) != 1 {
			t.Fatalf("old token consumed %d budget reservations, want 1", len(h.budget.calls))
		}
	})
	t.Run("two simultaneous requests converge on one generation", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 8)
		sandbox.ResourceVersion = "1"
		objects := testObjects(t, sandbox, executionUID)
		h := newHarness(t, objects, []auth.Account{operatorAccount})
		h.budget.barrier = 2
		h.budget.release = make(chan struct{})
		client := environmentpb.NewEnvironmentServiceClient(h.conn)
		type result struct {
			response *environmentpb.ResumeEnvironmentResponse
			err      error
		}
		results := make(chan result, 2)
		for range 2 {
			go func() {
				response, err := client.ResumeEnvironment(directContext(t, "operator"), resumeRequest(testSandboxUID, 8))
				results <- result{response: response, err: err}
			}()
		}
		requested, changed := 0, 0
		for range 2 {
			got := <-results
			if got.err != nil {
				t.Fatalf("concurrent resume returned %v", got.err)
			}
			switch got.response.GetOutcome() {
			case environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED:
				requested++
			case environmentpb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED:
				changed++
			default:
				t.Fatalf("concurrent outcome = %+v", got.response)
			}
		}
		var current sandboxapi.Sandbox
		if err := objects.Get(t.Context(), types.NamespacedName{Namespace: testNamespace, Name: testSandbox}, &current); err != nil {
			t.Fatal(err)
		}
		if requested != 1 || changed != 1 || current.Spec.WakeGeneration != 9 {
			t.Fatalf("concurrent requests requested=%d changed=%d generation=%d", requested, changed, current.Spec.WakeGeneration)
		}
	})
	t.Run("resource version conflict retries against same token", func(t *testing.T) {
		base := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 5), executionUID)
		conflicts := 0
		objects := testPatchClient{WithWatch: base, patch: func(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
			if conflicts == 0 {
				conflicts++
				return apierrors.NewConflict(schema.GroupResource{Group: sandboxapi.GroupVersion.Group, Resource: "sandboxes"}, testSandbox, errors.New("resource version changed"))
			}
			return base.Patch(ctx, object, patch, options...)
		}}
		h := newHarness(t, objects, []auth.Account{operatorAccount})
		response, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(directContext(t, "operator"), resumeRequest(testSandboxUID, 5))
		if err != nil || response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED || response.GetWakeGeneration() != 6 {
			t.Fatalf("resume after conflict = %+v, %v", response, err)
		}
		if conflicts != 1 || len(h.budget.calls) != 2 || h.budget.calls[0].gen != 6 || h.budget.calls[1].gen != 6 {
			t.Fatalf("conflict retries=%d, budget calls=%+v; want two reservations for generation 6", conflicts, h.budget.calls)
		}
	})
}

func TestLostPatchReplyIsUnknownAndNeverReplayed(t *testing.T) {
	base := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 9), executionUID)
	patches := 0
	objects := testPatchClient{WithWatch: base, patch: func(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
		patches++
		if err := base.Patch(ctx, object, patch, options...); err != nil {
			return err
		}
		return errors.New("connection closed after patch may have committed")
	}}
	h := newHarness(t, objects, []auth.Account{operatorAccount})
	client := environmentpb.NewEnvironmentServiceClient(h.conn)
	_, err := client.ResumeEnvironment(directContext(t, "operator"), resumeRequest(testSandboxUID, 9))
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("lost patch reply returned %v, want Unavailable", err)
	}
	st, _ := status.FromError(err)
	wake, ok := st.Details()[0].(*environmentlifecyclepb.WakeAssessment)
	if !ok || wake.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN || wake.GetTargetGeneration() != 9 {
		t.Fatalf("unknown outcome details = %v", st.Details())
	}
	response, err := client.ResumeEnvironment(directContext(t, "operator"), resumeRequest(testSandboxUID, 9))
	if err != nil || response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED {
		t.Fatalf("retry of uncertain old token = %+v, %v", response, err)
	}
	if patches != 1 {
		t.Fatalf("uncertain patch replayed %d times, want one", patches)
	}
}

func TestAutomaticWakeSharesPolicyBudgetAndReportsRetryFacts(t *testing.T) {
	caller := &lifecycle.ServiceAccountIdentity{Account: &identitypb.ServiceAccount{Namespace: testNamespace, Name: testSandbox}, Uid: executionUID}
	t.Run("same-name ServiceAccount self-wake is retired", func(t *testing.T) {
		objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 1), executionUID)
		h := newHarness(t, objects, nil)
		assessment, err := h.service.AssessAutomaticWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, lifecycle.AccountWakeCaller{Account: caller})
		if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_DENIED {
			t.Fatalf("AssessAutomaticWake = %+v, %v", assessment, err)
		}
		assessment, err = h.service.RequestWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, lifecycle.AccountWakeCaller{Account: caller})
		if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_DENIED || len(h.budget.calls) != 0 {
			t.Fatalf("RequestWake = %+v, %v, budget %+v; want denied without reservation", assessment, err, h.budget.calls)
		}
	})
	wakeIdentity := lifecycle.IdentityWakeCaller{Identity: &identitypb.Identity{Name: executionIdentityName, Uid: executionIdentityUID}}
	t.Run("budget exhausted", func(t *testing.T) {
		objects := testIdentityObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 2), executionUID)
		h := newHarness(t, objects, nil)
		h.budget.delay = 30 * time.Second
		assessment, err := h.service.RequestWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, wakeIdentity)
		if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_RATE_LIMITED || assessment.GetRetryAt() == nil {
			t.Fatalf("rate limited RequestWake = %+v, %v", assessment, err)
		}
	})
	t.Run("budget backend error", func(t *testing.T) {
		objects := testIdentityObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 2), executionUID)
		h := newHarness(t, objects, nil)
		h.budget.err = errors.New("storage unavailable")
		assessment, err := h.service.RequestWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, wakeIdentity)
		if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_FAILED || assessment.GetError().GetCode() != int32(codes.Unavailable) {
			t.Fatalf("budget error RequestWake = %+v, %v", assessment, err)
		}
	})
}

func TestAutomaticWakeBrokerDoesNotOverrideRequestedStop(t *testing.T) {
	caller := &lifecycle.ServiceAccountIdentity{Account: &identitypb.ServiceAccount{Namespace: testNamespace, Name: "notifications"}, Uid: "notifications-uid"}
	sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, false, sandboxapi.SuspendedByRequested, 1)
	objects := testObjects(t, sandbox, executionUID)
	h := newHarness(t, objects, nil)
	assessment, err := h.service.RequestWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, lifecycle.AccountWakeCaller{Account: caller})
	if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_DENIED || assessment.GetReason() != environmentlifecyclepb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP {
		t.Fatalf("broker wake of requested stop = %+v, %v", assessment, err)
	}
}

func TestWakeBrokerGrantRemainsAutomaticOnly(t *testing.T) {
	sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 2)
	objects := testObjects(t, sandbox, executionUID)
	h := newHarness(t, objects, nil)
	caller := &lifecycle.ServiceAccountIdentity{Account: &identitypb.ServiceAccount{Namespace: testNamespace, Name: "notifications"}, Uid: notificationUID}
	target := &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}
	assessment, err := h.service.AssessAutomaticWake(t.Context(), target, lifecycle.AccountWakeCaller{Account: caller})
	if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE || assessment.GetReason() != environmentlifecyclepb.WakeReason_WAKE_REASON_ALLOWED_WAKE_BROKER {
		t.Fatalf("broker automatic assessment = %+v, %v", assessment, err)
	}
	_, err = environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(
		directContext(t, "notification"), &environmentpb.ResumeEnvironmentRequest{Target: targetHandle(testSandboxUID)},
	)
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("automatic wake broker explicit resume returned %v, want PermissionDenied", err)
	}
}

func TestResumeTokenBindsTargetAndGeneration(t *testing.T) {
	target := &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}
	if lifecycle.NewResumeToken(target, 3) == lifecycle.NewResumeToken(target, 4) || lifecycle.NewResumeToken(target, 3) == lifecycle.NewResumeToken(&lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: "other"}, 3) {
		t.Fatal("resume token did not bind target UID and generation")
	}
}

func TestAutomaticWakeDeletionRacesGuardedPatch(t *testing.T) {
	sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 5)
	sandbox.Finalizers = []string{"test.allegedly.works/hold-deletion"}
	base := testIdentityObjects(t, sandbox, executionUID)
	patches := 0
	objects := testPatchClient{WithWatch: base, patch: func(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
		patches++
		// All policy checks and the budget reservation have completed. Deletion
		// advances resourceVersion before the wake's optimistic-lock patch.
		var current sandboxapi.Sandbox
		if err := base.Get(ctx, client.ObjectKeyFromObject(object), &current); err != nil {
			return err
		}
		if err := base.Delete(ctx, &current); err != nil {
			return err
		}
		return base.Patch(ctx, object, patch, options...)
	}}
	h := newHarness(t, objects, nil)
	caller := lifecycle.IdentityWakeCaller{Identity: &identitypb.Identity{Name: executionIdentityName, Uid: executionIdentityUID}}
	assessment, err := h.service.RequestWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, caller)
	if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_FAILED || assessment.GetReason() != environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED {
		t.Fatalf("wake racing deletion = %+v, %v", assessment, err)
	}
	var current sandboxapi.Sandbox
	if err := base.Get(t.Context(), client.ObjectKeyFromObject(sandbox), &current); err != nil {
		t.Fatal(err)
	}
	if current.DeletionTimestamp == nil || current.Spec.WakeGeneration != 5 || patches != 1 {
		t.Fatalf("deletion wake mutation: timestamp=%v generation=%d patches=%d", current.DeletionTimestamp, current.Spec.WakeGeneration, patches)
	}
}

// A Sandbox acting as an Identity wakes for that Identity's queued work under the existing
// allowOwnServiceAccount opt-in; another Identity, a replaced incarnation, a Sandbox acting as none
// and a disabled policy are refused.
func TestAutomaticWakeAuthorizesTheSandboxesOwnIdentity(t *testing.T) {
	const identityUID = "identity-uid"
	allowOwn := &sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}
	own := lifecycle.IdentityWakeCaller{Identity: &identitypb.Identity{Name: "agent", Uid: identityUID}}
	target := &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}
	for name, tc := range map[string]struct {
		policy *sandboxapi.ResumePolicy
		named  string
		caller lifecycle.IdentityWakeCaller
		state  environmentlifecyclepb.WakeState
		reason environmentlifecyclepb.WakeReason
	}{
		"own Identity":         {allowOwn, "agent", own, environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE, environmentlifecyclepb.WakeReason_WAKE_REASON_UNSPECIFIED},
		"policy disabled":      {nil, "agent", own, environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_RESUME_POLICY_DISABLED},
		"another Identity":     {allowOwn, "agent", lifecycle.IdentityWakeCaller{Identity: &identitypb.Identity{Name: "other", Uid: "other-uid"}}, environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED},
		"replaced incarnation": {allowOwn, "agent", lifecycle.IdentityWakeCaller{Identity: &identitypb.Identity{Name: "agent", Uid: "old-identity-uid"}}, environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_EXECUTION_ACCOUNT_CHANGED},
		"Sandbox acts as none": {allowOwn, "", own, environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED},
	} {
		t.Run(name, func(t *testing.T) {
			sandbox := suspendedSandbox(tc.policy, true, sandboxapi.SuspendedByIdle, 1)
			if tc.named != "" {
				sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: tc.named}
			}
			identity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "agent", UID: identityUID}}
			objects := clientfake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(sandbox, identity).Build()
			h := newHarness(t, objects, nil)
			assessment, err := h.service.AssessAutomaticWake(t.Context(), target, tc.caller)
			if err != nil || assessment.GetState() != tc.state || assessment.GetReason() != tc.reason {
				t.Fatalf("AssessAutomaticWake = %+v, %v; want %v %v", assessment, err, tc.state, tc.reason)
			}
			assessment, err = h.service.RequestWake(t.Context(), target, tc.caller)
			if tc.state != environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE {
				if err != nil || assessment.GetState() != tc.state || assessment.GetReason() != tc.reason || len(h.budget.calls) != 0 {
					t.Fatalf("RequestWake = %+v, %v, budget %+v; want the same refusal and no reservation", assessment, err, h.budget.calls)
				}
				return
			}
			if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_REQUESTED || assessment.GetTargetGeneration() != 2 {
				t.Fatalf("RequestWake = %+v, %v", assessment, err)
			}
			want := lifecycle.WakeBudgetKey{Namespace: testNamespace, Name: "agent", UID: identityUID}
			if len(h.budget.calls) != 1 || h.budget.calls[0].caller != want || h.budget.calls[0].target != testSandboxUID || h.budget.calls[0].gen != 2 {
				t.Fatalf("wake budget calls = %+v, want one for %+v", h.budget.calls, want)
			}
		})
	}
}
