package lifecycle

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	discoverypb "github.com/agentydragon/ducktape/agentplane/sandboxes/discovery/discoverypb"
	lifecyclepb "github.com/agentydragon/ducktape/agentplane/sandboxes/lifecycle/lifecyclepb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
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
	testNamespace   = "agentplane"
	testSandbox     = "workspace"
	testSandboxUID  = "sandbox-uid"
	executionUID    = "execution-uid"
	operatorUID     = "operator-uid"
	actorUID        = "actor-uid"
	notificationUID = "notifications-uid"
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
	caller ServiceAccount
	target string
	gen    int64
}

func (b *testWakeBudget) ReserveWakeAttempt(_ context.Context, caller ServiceAccount, target string, generation int64) (time.Duration, error) {
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
	client.Client
	patch func(context.Context, client.Object, client.Patch, ...client.PatchOption) error
}

func (c testPatchClient) Patch(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
	if c.patch != nil {
		return c.patch(ctx, object, patch, options...)
	}
	return c.Client.Patch(ctx, object, patch, options...)
}

type lifecycleHarness struct {
	client  client.Client
	service *Service
	budget  *testWakeBudget
	conn    *grpc.ClientConn
}

func newHarness(t *testing.T, object client.Client, operatorCallers []auth.Account) lifecycleHarness {
	t.Helper()
	budget := &testWakeBudget{}
	service, err := NewService(Options{
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
	methods, err := Methods(resolver).WithDelegation(
		[]string{lifecyclepb.EnvironmentLifecycle_ResumeEnvironment_FullMethodName},
		[]auth.Delegator{{Account: actorAccount, CallerNamespaces: []string{testNamespace}}},
		auth.ServiceAccountUIDReaderFunc(func(_ context.Context, account auth.Account) (string, bool, error) {
			switch account {
			case actorAccount:
				return actorUID, true, nil
			case executionAccount:
				return executionUID, true, nil
			default:
				return "", false, nil
			}
		}),
	)
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.Serve(t, methods, func(server *grpc.Server) {
		lifecyclepb.RegisterEnvironmentLifecycleServer(server, service)
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

func testObjects(t *testing.T, sandbox *sandboxapi.Sandbox, executionUID string) client.Client {
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

func targetHandle(uid string) *discoverypb.TargetHandle {
	return &discoverypb.TargetHandle{Namespace: testNamespace, Name: testSandbox, Uid: uid}
}

func targetToken(uid string, generation int64) string {
	return NewResumeToken(Target{Namespace: testNamespace, Name: testSandbox, UID: uid}, generation)
}

func directContext(t *testing.T, token string) context.Context {
	t.Helper()
	return metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer "+token)
}

func delegatedContext(t *testing.T, effectiveUID string) context.Context {
	t.Helper()
	envelope := &delegationpb.Delegation{
		Caller: &delegationpb.ServiceAccountIdentity{
			Account: &sandboxpb.ServiceAccount{Namespace: executionAccount.Namespace, Name: executionAccount.Name}, Uid: effectiveUID,
		},
		ActionRequestId: uuid.NewString(), ActionExecutionId: uuid.NewString(),
	}
	encoded, err := auth.MarshalDelegation(envelope)
	if err != nil {
		t.Fatal(err)
	}
	return metadata.NewOutgoingContext(t.Context(), metadata.Pairs(
		"authorization", "Agentplane-Delegated actor", auth.DelegationMetadataKey, string(encoded),
	))
}

func resumeRequest(uid string, generation int64) *lifecyclepb.ResumeEnvironmentRequest {
	return &lifecyclepb.ResumeEnvironmentRequest{Target: targetHandle(uid), ResumeToken: targetToken(uid, generation)}
}

func TestOwnExecutionAccountCanResumeIdleSuspendedSandboxWithOptIn(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 4), executionUID)
	h := newHarness(t, objects, nil)
	response, err := lifecyclepb.NewEnvironmentLifecycleClient(h.conn).ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 4))
	if err != nil {
		t.Fatal(err)
	}
	if response.GetOutcome() != lifecyclepb.ResumeOutcome_RESUME_OUTCOME_REQUESTED || response.GetWakeGeneration() != 5 {
		t.Fatalf("ResumeEnvironment response = %+v", response)
	}
	if response.GetEligibility().GetReason() != environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_OWN_SERVICE_ACCOUNT {
		t.Fatalf("eligibility = %+v", response.GetEligibility())
	}
	var got sandboxapi.Sandbox
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: testNamespace, Name: testSandbox}, &got); err != nil {
		t.Fatal(err)
	}
	if !got.Spec.Running || got.Spec.WakeGeneration != 5 {
		t.Fatalf("patched Sandbox spec = %+v", got.Spec)
	}
}

func TestDelegatedActorDoesNotInheritOperatorAuthority(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 2), executionUID)
	h := newHarness(t, objects, []auth.Account{actorAccount})
	_, err := lifecyclepb.NewEnvironmentLifecycleClient(h.conn).ResumeEnvironment(
		delegatedContext(t, executionUID), resumeRequest(testSandboxUID, 2),
	)
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("delegated actor operator grant returned %v, want PermissionDenied", err)
	}
	st, ok := status.FromError(err)
	if !ok || len(st.Details()) != 1 {
		t.Fatalf("denial details = %v", err)
	}
	eligibility, ok := st.Details()[0].(*environmentpb.ResumeEligibility)
	if !ok || eligibility.GetReason() != environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED {
		t.Fatalf("delegated caller eligibility = %v", st.Details())
	}
}

func TestDelegatedCallerCannotOmitResumeToken(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 1), executionUID)
	h := newHarness(t, objects, []auth.Account{actorAccount})
	request := &lifecyclepb.ResumeEnvironmentRequest{Target: targetHandle(testSandboxUID)}
	_, err := lifecyclepb.NewEnvironmentLifecycleClient(h.conn).ResumeEnvironment(delegatedContext(t, executionUID), request)
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("delegated token omission returned %v, want InvalidArgument", err)
	}
}

func TestLiveExecutionAccountReplacementDeniesOldCallerUID(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 1), "replacement-uid")
	h := newHarness(t, objects, nil)
	_, err := lifecyclepb.NewEnvironmentLifecycleClient(h.conn).ResumeEnvironment(directContext(t, "stale-execution"), resumeRequest(testSandboxUID, 1))
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("old execution identity returned %v, want PermissionDenied", err)
	}
	st, _ := status.FromError(err)
	eligibility, ok := st.Details()[0].(*environmentpb.ResumeEligibility)
	if !ok || eligibility.GetReason() != environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_EXECUTION_ACCOUNT_CHANGED {
		t.Fatalf("replacement denial details = %v", st.Details())
	}
}

func TestTargetReplacementAndRequestedStopAreNotOrdinaryResumeTargets(t *testing.T) {
	t.Run("replacement UID", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 1)
		sandbox.UID = "replacement-sandbox-uid"
		objects := testObjects(t, sandbox, executionUID)
		h := newHarness(t, objects, nil)
		_, err := lifecyclepb.NewEnvironmentLifecycleClient(h.conn).ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 1))
		if status.Code(err) != codes.NotFound {
			t.Fatalf("replacement target returned %v, want NotFound", err)
		}
	})
	t.Run("requested stop is sticky", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, false, sandboxapi.SuspendedByRequested, 1)
		objects := testObjects(t, sandbox, executionUID)
		h := newHarness(t, objects, nil)
		_, err := lifecyclepb.NewEnvironmentLifecycleClient(h.conn).ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 1))
		if status.Code(err) != codes.PermissionDenied {
			t.Fatalf("ordinary requested-stop resume returned %v, want PermissionDenied", err)
		}
		st, _ := status.FromError(err)
		eligibility, ok := st.Details()[0].(*environmentpb.ResumeEligibility)
		if !ok || eligibility.GetReason() != environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP {
			t.Fatalf("requested-stop denial details = %v", st.Details())
		}
	})
	t.Run("recorded Requested status stays sticky during reconciliation", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByRequested, 1)
		objects := testObjects(t, sandbox, executionUID)
		h := newHarness(t, objects, nil)
		_, err := lifecyclepb.NewEnvironmentLifecycleClient(h.conn).ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 1))
		if status.Code(err) != codes.PermissionDenied {
			t.Fatalf("recorded requested-stop resume returned %v, want PermissionDenied", err)
		}
		st, _ := status.FromError(err)
		eligibility, ok := st.Details()[0].(*environmentpb.ResumeEligibility)
		if !ok || eligibility.GetReason() != environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP {
			t.Fatalf("recorded requested-stop denial details = %v", st.Details())
		}
	})
}

func TestDirectPodBoundOperatorCanResumeRequestedStopWithoutToken(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{}, false, sandboxapi.SuspendedByRequested, 7), executionUID)
	h := newHarness(t, objects, []auth.Account{operatorAccount})
	response, err := lifecyclepb.NewEnvironmentLifecycleClient(h.conn).ResumeEnvironment(directContext(t, "operator"), &lifecyclepb.ResumeEnvironmentRequest{Target: targetHandle(testSandboxUID)})
	if err != nil {
		t.Fatal(err)
	}
	if response.GetOutcome() != lifecyclepb.ResumeOutcome_RESUME_OUTCOME_REQUESTED || response.GetWakeGeneration() != 8 {
		t.Fatalf("operator resume response = %+v", response)
	}
	if response.GetEligibility().GetReason() != environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_TRUSTED_OPERATOR {
		t.Fatalf("operator was misreported as own-SA eligible: %+v", response.GetEligibility())
	}
}

func TestOldGenerationDoesNotAdvanceASecondWakeAndConcurrentConflictRereads(t *testing.T) {
	t.Run("old token is state changed", func(t *testing.T) {
		objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 3), executionUID)
		h := newHarness(t, objects, nil)
		client := lifecyclepb.NewEnvironmentLifecycleClient(h.conn)
		first, err := client.ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 3))
		if err != nil || first.GetWakeGeneration() != 4 {
			t.Fatalf("first resume = %+v, %v", first, err)
		}
		second, err := client.ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 3))
		if err != nil || second.GetOutcome() != lifecyclepb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED || second.GetWakeGeneration() != 4 {
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
		h := newHarness(t, objects, nil)
		h.budget.barrier = 2
		h.budget.release = make(chan struct{})
		client := lifecyclepb.NewEnvironmentLifecycleClient(h.conn)
		type result struct {
			response *lifecyclepb.ResumeEnvironmentResponse
			err      error
		}
		results := make(chan result, 2)
		for range 2 {
			go func() {
				response, err := client.ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 8))
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
			case lifecyclepb.ResumeOutcome_RESUME_OUTCOME_REQUESTED:
				requested++
			case lifecyclepb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED:
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
		objects := testPatchClient{Client: base, patch: func(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
			if conflicts == 0 {
				conflicts++
				return apierrors.NewConflict(schema.GroupResource{Group: sandboxapi.GroupVersion.Group, Resource: "sandboxes"}, testSandbox, errors.New("resource version changed"))
			}
			return base.Patch(ctx, object, patch, options...)
		}}
		h := newHarness(t, objects, nil)
		response, err := lifecyclepb.NewEnvironmentLifecycleClient(h.conn).ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 5))
		if err != nil || response.GetOutcome() != lifecyclepb.ResumeOutcome_RESUME_OUTCOME_REQUESTED || response.GetWakeGeneration() != 6 {
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
	objects := testPatchClient{Client: base, patch: func(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
		patches++
		if err := base.Patch(ctx, object, patch, options...); err != nil {
			return err
		}
		return errors.New("connection closed after patch may have committed")
	}}
	h := newHarness(t, objects, nil)
	client := lifecyclepb.NewEnvironmentLifecycleClient(h.conn)
	_, err := client.ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 9))
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("lost patch reply returned %v, want Unavailable", err)
	}
	st, _ := status.FromError(err)
	wake, ok := st.Details()[0].(*environmentpb.WakeAssessment)
	if !ok || wake.GetState() != environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN || wake.GetTargetGeneration() != 9 {
		t.Fatalf("unknown outcome details = %v", st.Details())
	}
	response, err := client.ResumeEnvironment(directContext(t, "execution"), resumeRequest(testSandboxUID, 9))
	if err != nil || response.GetOutcome() != lifecyclepb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED {
		t.Fatalf("retry of uncertain old token = %+v, %v", response, err)
	}
	if patches != 1 {
		t.Fatalf("uncertain patch replayed %d times, want one", patches)
	}
}

func TestAutomaticWakeSharesPolicyBudgetAndReportsRetryFacts(t *testing.T) {
	caller := ServiceAccount{Namespace: testNamespace, Name: testSandbox, UID: executionUID}
	t.Run("own account opt in", func(t *testing.T) {
		objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 1), executionUID)
		h := newHarness(t, objects, nil)
		assessment, err := h.service.AssessAutomaticWake(t.Context(), Target{Namespace: testNamespace, Name: testSandbox, UID: testSandboxUID}, caller)
		if err != nil || assessment.GetState() != environmentpb.WakeState_WAKE_STATE_ELIGIBLE {
			t.Fatalf("AssessAutomaticWake = %+v, %v", assessment, err)
		}
		if len(h.budget.calls) != 0 {
			t.Fatalf("read-only assessment reserved wake budget: %+v", h.budget.calls)
		}
		assessment, err = h.service.RequestWake(t.Context(), Target{Namespace: testNamespace, Name: testSandbox, UID: testSandboxUID}, caller)
		if err != nil || assessment.GetState() != environmentpb.WakeState_WAKE_STATE_REQUESTED || assessment.GetTargetGeneration() != 2 {
			t.Fatalf("RequestWake = %+v, %v", assessment, err)
		}
		if len(h.budget.calls) != 1 || h.budget.calls[0].caller.UID != executionUID || h.budget.calls[0].target != testSandboxUID || h.budget.calls[0].gen != 2 {
			t.Fatalf("wake budget key = %+v", h.budget.calls)
		}
	})
	t.Run("budget exhausted", func(t *testing.T) {
		objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 2), executionUID)
		h := newHarness(t, objects, nil)
		h.budget.delay = 30 * time.Second
		assessment, err := h.service.RequestWake(t.Context(), Target{Namespace: testNamespace, Name: testSandbox, UID: testSandboxUID}, caller)
		if err != nil || assessment.GetState() != environmentpb.WakeState_WAKE_STATE_RATE_LIMITED || assessment.GetRetryAt() == nil {
			t.Fatalf("rate limited RequestWake = %+v, %v", assessment, err)
		}
	})
	t.Run("budget backend error", func(t *testing.T) {
		objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 2), executionUID)
		h := newHarness(t, objects, nil)
		h.budget.err = errors.New("storage unavailable")
		assessment, err := h.service.RequestWake(t.Context(), Target{Namespace: testNamespace, Name: testSandbox, UID: testSandboxUID}, caller)
		if err != nil || assessment.GetState() != environmentpb.WakeState_WAKE_STATE_FAILED || assessment.GetError().GetCode() != int32(codes.Unavailable) {
			t.Fatalf("budget error RequestWake = %+v, %v", assessment, err)
		}
	})
}

func TestAutomaticWakeBrokerDoesNotOverrideRequestedStop(t *testing.T) {
	caller := ServiceAccount{Namespace: testNamespace, Name: "notifications", UID: "notifications-uid"}
	sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, false, sandboxapi.SuspendedByRequested, 1)
	objects := testObjects(t, sandbox, executionUID)
	h := newHarness(t, objects, nil)
	assessment, err := h.service.RequestWake(t.Context(), Target{Namespace: testNamespace, Name: testSandbox, UID: testSandboxUID}, caller)
	if err != nil || assessment.GetState() != environmentpb.WakeState_WAKE_STATE_DENIED || assessment.GetReason() != environmentpb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP {
		t.Fatalf("broker wake of requested stop = %+v, %v", assessment, err)
	}
}

func TestWakeBrokerGrantRemainsAutomaticOnly(t *testing.T) {
	sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 2)
	objects := testObjects(t, sandbox, executionUID)
	h := newHarness(t, objects, nil)
	caller := ServiceAccount{Namespace: testNamespace, Name: "notifications", UID: notificationUID}
	target := Target{Namespace: testNamespace, Name: testSandbox, UID: testSandboxUID}
	assessment, err := h.service.AssessAutomaticWake(t.Context(), target, caller)
	if err != nil || assessment.GetState() != environmentpb.WakeState_WAKE_STATE_ELIGIBLE || assessment.GetReason() != environmentpb.WakeReason_WAKE_REASON_ALLOWED_WAKE_BROKER {
		t.Fatalf("broker automatic assessment = %+v, %v", assessment, err)
	}
	_, err = lifecyclepb.NewEnvironmentLifecycleClient(h.conn).ResumeEnvironment(
		directContext(t, "notification"), &lifecyclepb.ResumeEnvironmentRequest{Target: targetHandle(testSandboxUID)},
	)
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("automatic wake broker explicit resume returned %v, want PermissionDenied", err)
	}
}

var _ lifecyclepb.EnvironmentLifecycleServer = (*Service)(nil)

func TestResumeTokenBindsTargetAndGeneration(t *testing.T) {
	target := Target{Namespace: testNamespace, Name: testSandbox, UID: testSandboxUID}
	if NewResumeToken(target, 3) == NewResumeToken(target, 4) || NewResumeToken(target, 3) == NewResumeToken(Target{Namespace: testNamespace, Name: testSandbox, UID: "other"}, 3) {
		t.Fatal("resume token did not bind target UID and generation")
	}
}
