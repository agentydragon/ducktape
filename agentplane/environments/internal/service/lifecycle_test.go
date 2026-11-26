package service_test

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/access"
	"github.com/agentydragon/ducktape/agentplane/environments/internal/resume"
	environmentservice "github.com/agentydragon/ducktape/agentplane/environments/internal/service"
	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/wake"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/testing/testscheme"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
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
)

type testWakeBudget struct {
	mu    sync.Mutex
	delay time.Duration
	err   error
	calls []budgetCall
}

type budgetCall struct {
	caller wake.WakeBudgetKey
	target string
	gen    int64
}

func (b *testWakeBudget) ReserveWakeAttempt(_ context.Context, caller wake.WakeBudgetKey, target string, generation int64) (time.Duration, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.calls = append(b.calls, budgetCall{caller: caller, target: target, gen: generation})
	return b.delay, b.err
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
	service *resume.Manager
	wake    *wake.Manager
	budget  *testWakeBudget
	conn    grpc.ClientConnInterface
}

func newHarness(t *testing.T, object client.WithWatch, operatorServiceCallers []string) lifecycleHarness {
	return newConfiguredHarness(t, object, operatorServiceCallers, nil, nil, false)
}

func newIdentityHarness(
	t *testing.T,
	object client.WithWatch,
	operatorServiceCallers []string,
	rules *reader.Index[access.Rule],
	policyReady func() error,
) lifecycleHarness {
	return newConfiguredHarness(t, object, operatorServiceCallers, rules, policyReady, true)
}

func newConfiguredHarness(
	t *testing.T,
	object client.WithWatch,
	operatorServiceCallers []string,
	rules *reader.Index[access.Rule],
	policyReady func() error,
	identityDelegation bool,
) lifecycleHarness {
	t.Helper()
	budget := &testWakeBudget{}
	service, err := resume.NewManager(lifecycle.Options{
		Namespace: testNamespace, Objects: object, Identities: kubernetesIdentities(t, object, testNamespace),
		WakeServiceCallers:     []string{peertls.NotificationsURI},
		OperatorServiceCallers: operatorServiceCallers,
		EnvironmentRules:       rules, PolicyReady: policyReady,
	})
	if err != nil {
		t.Fatal(err)
	}
	automatic, err := wake.NewManager(wake.Options{
		Namespace: testNamespace, Objects: object, Identities: kubernetesIdentities(t, object, testNamespace), WakeBudget: budget,
		WakeServiceCallers: []string{peertls.NotificationsURI},
	})
	if err != nil {
		t.Fatal(err)
	}
	connection := serveEnvironmentService(t, object, service, identityDelegation)
	return lifecycleHarness{client: object, service: service, wake: automatic, budget: budget, conn: connection}
}

func serveEnvironmentService(
	t *testing.T,
	object client.WithWatch,
	lifecycleManager *resume.Manager,
	identityDelegation bool,
) grpc.ClientConnInterface {
	t.Helper()
	methods, err := environmentservice.WithServiceCallers(environmentservice.Methods(nil), nil, nil, nil, []string{peertls.AppURI, peertls.ActionsURI, peertls.NotificationsURI})
	if err != nil {
		t.Fatal(err)
	}
	if identityDelegation {
		identities, err := auth.KubernetesIdentityUIDs(object, testNamespace)
		if err != nil {
			t.Fatal(err)
		}
		methods, err = methods.WithServiceDelegation(
			[]string{environmentpb.EnvironmentService_ResumeEnvironment_FullMethodName},
			[]auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}}, identities,
		)
		if err != nil {
			t.Fatal(err)
		}
	}
	apiService, err := environmentservice.New(testNamespace, object, kubernetesIdentities(t, object, testNamespace), runtimeconfig.Catalog{}, lifecycleManager, environmentservice.Options{})
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.ServeTLS(t, peertls.SandboxControllerURI, methods, func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, apiService)
	}, peertls.AppURI, peertls.ActionsURI, peertls.NotificationsURI)
	return connection
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
	return clientfake.NewClientBuilder().WithScheme(testscheme.New(t)).WithObjects(sandbox, account).Build()
}

func testIdentityObjects(t *testing.T, sandbox *sandboxapi.Sandbox, executionUID string) client.WithWatch {
	t.Helper()
	sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: executionIdentityName}
	objects := testObjects(t, sandbox, executionUID)
	if err := objects.Create(t.Context(), executionIdentity()); err != nil {
		t.Fatal(err)
	}
	return objects
}

func executionIdentity() *policyapi.Identity {
	return &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: executionIdentityName, UID: types.UID(executionIdentityUID)}}
}

// selfResumeRules lets the execution Identity resume the Sandboxes bound to itself until expiresAt, or
// without expiry when it is nil.
func selfResumeRules(expiresAt *metav1.Time) *reader.Index[access.Rule] {
	return identityResumeRules(executionIdentity(), []policyapi.EnvironmentSelector{{BoundIdentity: &policyapi.BoundIdentitySelector{Self: true}}}, expiresAt)
}

func identityResumeRules(identity *policyapi.Identity, selectors []policyapi.EnvironmentSelector, expiresAt *metav1.Time) *reader.Index[access.Rule] {
	rules := access.NewIndex()
	rules.PutIdentity(identity)
	rules.PutPolicySet(&policyapi.PolicySet{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "resume-targets"},
		Spec: policyapi.PolicySetSpec{Rules: []policyapi.Rule{{
			Type: "environment", Operations: []string{"resume"}, Environments: selectors,
		}}},
	})
	rules.PutBinding(&policyapi.PolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "resume-grant"},
		Spec: policyapi.PolicyBindingSpec{
			Subject:    policyapi.Subject{Identity: &policyapi.IdentityRef{Name: identity.Name}},
			PolicySets: []string{"resume-targets"}, ExpiresAt: expiresAt,
		},
	})
	return rules
}

func targetHandle(uid string) *identitypb.EnvironmentTarget {
	return &identitypb.EnvironmentTarget{Namespace: testNamespace, Name: testSandbox, Uid: uid}
}

func targetToken(uid string, generation int64) string {
	return lifecycle.NewResumeToken(&lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: uid}, generation)
}

func directContext(t *testing.T, uri string) context.Context {
	t.Helper()
	return grpcmethods.Caller(t.Context(), uri)
}

func delegatedContext(t *testing.T) context.Context {
	return delegatedContextFor(t, auth.Identity{Name: executionIdentityName, UID: executionIdentityUID})
}

func delegatedContextFor(t *testing.T, identity auth.Identity) context.Context {
	t.Helper()
	return grpcmethods.WithActionDelegation(t, grpcmethods.Caller(t.Context(), peertls.ActionsURI), identity)
}

func resumeRequest(uid string, generation int64) *environmentpb.ResumeEnvironmentRequest {
	return &environmentpb.ResumeEnvironmentRequest{Target: targetHandle(uid), ResumeToken: targetToken(uid, generation)}
}

// delegatedResume has the Actions service resume the Sandbox on behalf of the execution Identity.
func (h lifecycleHarness) delegatedResume(t *testing.T, generation int64) error {
	t.Helper()
	_, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(delegatedContext(t), resumeRequest(testSandboxUID, generation))
	return err
}

// requireRequestedStopDetails fails unless the denial err carries the requested-stop eligibility.
func requireRequestedStopDetails(t *testing.T, err error) {
	t.Helper()
	st, _ := status.FromError(err)
	eligibility, ok := st.Details()[0].(*environmentlifecyclepb.ResumeEligibility)
	if !ok || eligibility.GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP {
		t.Fatalf("requested-stop denial details = %v", st.Details())
	}
}

func TestServiceWithoutOperatorRoleCannotResumeIdleSuspendedSandbox(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 4), executionUID)
	h := newHarness(t, objects, nil)
	response, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(directContext(t, peertls.NotificationsURI), resumeRequest(testSandboxUID, 4))
	if status.Code(err) != codes.PermissionDenied || response != nil {
		t.Fatalf("non-operator service resume = %+v, %v; want PermissionDenied", response, err)
	}
	var got sandboxapi.Sandbox
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: testNamespace, Name: testSandbox}, &got); err != nil {
		t.Fatal(err)
	}
	if !got.Spec.Running || got.Spec.WakeGeneration != 4 {
		t.Fatalf("denied non-operator service changed Sandbox spec = %+v", got.Spec)
	}
}

func TestIdentityGrantCanResumeForeignSandboxWithoutWakeBudget(t *testing.T) {
	sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 4)
	sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: "target-agent"}
	objects := testObjects(t, sandbox, executionUID)
	callerIdentity := executionIdentity()
	targetIdentity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "target-agent", UID: "target-agent-uid"}}
	for _, identity := range []*policyapi.Identity{callerIdentity, targetIdentity} {
		if err := objects.Create(t.Context(), identity); err != nil {
			t.Fatal(err)
		}
	}
	rules := identityResumeRules(callerIdentity, []policyapi.EnvironmentSelector{{Sandbox: &policyapi.SandboxTarget{Name: testSandbox}}}, nil)
	h := newIdentityHarness(t, objects, nil, rules, func() error { return nil })
	response, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(
		delegatedContext(t), resumeRequest(testSandboxUID, 4),
	)
	if err != nil || response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED ||
		response.GetEligibility().GetReason() != environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_IDENTITY_GRANT {
		t.Fatalf("granted foreign resume = %+v, %v", response, err)
	}
	if len(h.budget.calls) != 0 {
		t.Fatalf("explicit Identity resume reserved wake budget: %+v", h.budget.calls)
	}
}

func TestIdentityResumeHidesForeignTargetWithoutGrant(t *testing.T) {
	sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 3)
	sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: "another-agent"}
	objects := testObjects(t, sandbox, executionUID)
	callerIdentity := executionIdentity()
	targetIdentity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "another-agent", UID: "another-agent-uid"}}
	for _, identity := range []*policyapi.Identity{callerIdentity, targetIdentity} {
		if err := objects.Create(t.Context(), identity); err != nil {
			t.Fatal(err)
		}
	}
	h := newIdentityHarness(t, objects, nil, access.NewIndex(), func() error { return nil })
	err := h.delegatedResume(t, 3)
	if status.Code(err) != codes.NotFound {
		t.Fatalf("ungranted foreign resume returned %v, want NotFound", err)
	}
}

func TestIdentityGrantSubjectUIDAndExpiryAreChecked(t *testing.T) {
	t.Run("stale indexed Identity UID", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 2)
		sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: executionIdentityName}
		objects := testObjects(t, sandbox, executionUID)
		current := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: executionIdentityName, UID: types.UID("current-identity-uid")}}
		if err := objects.Create(t.Context(), current); err != nil {
			t.Fatal(err)
		}
		indexed := current.DeepCopy()
		indexed.UID = "old-identity-uid"
		rules := identityResumeRules(indexed, []policyapi.EnvironmentSelector{{BoundIdentity: &policyapi.BoundIdentitySelector{Self: true}}}, nil)
		h := newIdentityHarness(t, objects, nil, rules, func() error { return nil })
		ctx := delegatedContextFor(t, auth.Identity{Name: executionIdentityName, UID: string(current.UID)})
		_, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(ctx, resumeRequest(testSandboxUID, 2))
		if status.Code(err) != codes.PermissionDenied {
			t.Fatalf("stale indexed Identity grant returned %v, want PermissionDenied", err)
		}
	})

	t.Run("expired binding", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 2)
		objects := testIdentityObjects(t, sandbox, executionUID)
		expiresAt := metav1.NewTime(time.Now().Add(-time.Second))
		rules := selfResumeRules(&expiresAt)
		h := newIdentityHarness(t, objects, nil, rules, func() error { return nil })
		err := h.delegatedResume(t, 2)
		if status.Code(err) != codes.PermissionDenied {
			t.Fatalf("expired grant returned %v, want PermissionDenied", err)
		}
	})
}

func TestIdentityGrantNeverOverridesRequestedStop(t *testing.T) {
	sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, false, sandboxapi.SuspendedByRequested, 6)
	objects := testIdentityObjects(t, sandbox, executionUID)
	rules := selfResumeRules(nil)
	h := newIdentityHarness(t, objects, nil, rules, func() error { return nil })
	err := h.delegatedResume(t, 6)
	if status.Code(err) != codes.PermissionDenied || len(h.budget.calls) != 0 {
		t.Fatalf("requested-stop resume = %v, budget %+v; want PermissionDenied without reservation", err, h.budget.calls)
	}
	requireRequestedStopDetails(t, err)
}

func TestIdentityResumeFailsClosedWithoutPolicyReadiness(t *testing.T) {
	sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 6)
	objects := testIdentityObjects(t, sandbox, executionUID)
	rules := selfResumeRules(nil)
	h := newIdentityHarness(t, objects, nil, rules, nil)
	err := h.delegatedResume(t, 6)
	if status.Code(err) != codes.Unavailable || len(h.budget.calls) != 0 {
		t.Fatalf("resume without policy readiness = %v, budget %+v; want Unavailable without reservation", err, h.budget.calls)
	}
}

func TestDelegatedActorDoesNotAuthorizeIdentityResume(t *testing.T) {
	sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 2)
	objects := testIdentityObjects(t, sandbox, executionUID)
	h := newIdentityHarness(t, objects, []string{peertls.ActionsURI}, access.NewIndex(), func() error { return nil })
	err := h.delegatedResume(t, 2)
	if status.Code(err) != codes.PermissionDenied || len(h.budget.calls) != 0 {
		t.Fatalf("delegated actor authorization = %v, budget %+v; want effective Identity denial", err, h.budget.calls)
	}
}

func TestIdentityResumeRechecksPolicyAndTargetIdentityAfterConflict(t *testing.T) {
	t.Run("grant revocation", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 5)
		objects := testIdentityObjects(t, sandbox, executionUID)
		rules := selfResumeRules(nil)
		patches := 0
		object := testPatchClient{WithWatch: objects, patch: func(_ context.Context, object client.Object, _ client.Patch, _ ...client.PatchOption) error {
			patches++
			rules.DeleteBinding("resume-grant")
			return apierrors.NewConflict(schema.GroupResource{Resource: "sandboxes"}, object.GetName(), errors.New("policy changed during resume"))
		}}
		h := newIdentityHarness(t, object, nil, rules, func() error { return nil })
		err := h.delegatedResume(t, 5)
		if status.Code(err) != codes.PermissionDenied || patches != 1 {
			t.Fatalf("revoked resume = %v after %d patches, want one attempt and denial", err, patches)
		}
	})

	t.Run("bound target Identity is deleted", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 5)
		sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: "target-agent"}
		objects := testObjects(t, sandbox, executionUID)
		callerIdentity := executionIdentity()
		targetIdentity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "target-agent", UID: "target-agent-uid"}}
		for _, identity := range []*policyapi.Identity{callerIdentity, targetIdentity} {
			if err := objects.Create(t.Context(), identity); err != nil {
				t.Fatal(err)
			}
		}
		rules := identityResumeRules(callerIdentity, []policyapi.EnvironmentSelector{{BoundIdentity: &policyapi.BoundIdentitySelector{Name: "target-agent"}}}, nil)
		patches := 0
		object := testPatchClient{WithWatch: objects, patch: func(ctx context.Context, object client.Object, _ client.Patch, _ ...client.PatchOption) error {
			patches++
			if err := objects.Delete(ctx, targetIdentity); err != nil {
				t.Fatal(err)
			}
			return apierrors.NewConflict(schema.GroupResource{Resource: "sandboxes"}, object.GetName(), errors.New("target Identity changed during resume"))
		}}
		h := newIdentityHarness(t, object, nil, rules, func() error { return nil })
		err := h.delegatedResume(t, 5)
		if status.Code(err) != codes.NotFound || patches != 1 {
			t.Fatalf("stale target Identity resume = %v after %d patches, want NotFound after one attempt", err, patches)
		}
	})

	t.Run("bound target is rebound", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 5)
		sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: "target-agent"}
		objects := testObjects(t, sandbox, executionUID)
		callerIdentity := executionIdentity()
		targetIdentity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "target-agent", UID: "target-agent-uid"}}
		replacement := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Namespace: testNamespace, Name: "replacement-agent", UID: "replacement-agent-uid"}}
		for _, identity := range []*policyapi.Identity{callerIdentity, targetIdentity, replacement} {
			if err := objects.Create(t.Context(), identity); err != nil {
				t.Fatal(err)
			}
		}
		rules := identityResumeRules(callerIdentity, []policyapi.EnvironmentSelector{{BoundIdentity: &policyapi.BoundIdentitySelector{Name: "target-agent"}}}, nil)
		patches := 0
		object := testPatchClient{WithWatch: objects, patch: func(ctx context.Context, object client.Object, _ client.Patch, _ ...client.PatchOption) error {
			patches++
			var current sandboxapi.Sandbox
			if err := objects.Get(ctx, types.NamespacedName{Namespace: testNamespace, Name: testSandbox}, &current); err != nil {
				t.Fatal(err)
			}
			current.Spec.Identity = &sandboxapi.IdentityRef{Name: replacement.Name}
			if err := objects.Update(ctx, &current); err != nil {
				t.Fatal(err)
			}
			return apierrors.NewConflict(schema.GroupResource{Resource: "sandboxes"}, object.GetName(), errors.New("target rebound during resume"))
		}}
		h := newIdentityHarness(t, object, nil, rules, func() error { return nil })
		err := h.delegatedResume(t, 5)
		if status.Code(err) != codes.NotFound || patches != 1 {
			t.Fatalf("rebound target resume = %v after %d patches, want NotFound after one attempt", err, patches)
		}
	})

	t.Run("caller Identity is deleted", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 5)
		objects := testIdentityObjects(t, sandbox, executionUID)
		rules := selfResumeRules(nil)
		patches := 0
		object := testPatchClient{WithWatch: objects, patch: func(ctx context.Context, object client.Object, _ client.Patch, _ ...client.PatchOption) error {
			patches++
			if err := objects.Delete(ctx, executionIdentity()); err != nil {
				t.Fatal(err)
			}
			return apierrors.NewConflict(schema.GroupResource{Resource: "sandboxes"}, object.GetName(), errors.New("caller Identity changed during resume"))
		}}
		h := newIdentityHarness(t, object, nil, rules, func() error { return nil })
		err := h.delegatedResume(t, 5)
		if status.Code(err) != codes.NotFound || patches != 1 {
			t.Fatalf("deleted caller Identity resume = %v after %d patches, want NotFound after one attempt", err, patches)
		}
	})

	t.Run("policy readiness changes", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 5)
		objects := testIdentityObjects(t, sandbox, executionUID)
		rules := selfResumeRules(nil)
		var policyErr error
		patches := 0
		object := testPatchClient{WithWatch: objects, patch: func(_ context.Context, object client.Object, _ client.Patch, _ ...client.PatchOption) error {
			patches++
			policyErr = errors.New("policy reader became unavailable")
			return apierrors.NewConflict(schema.GroupResource{Resource: "sandboxes"}, object.GetName(), errors.New("Sandbox changed during resume"))
		}}
		h := newIdentityHarness(t, object, nil, rules, func() error { return policyErr })
		err := h.delegatedResume(t, 5)
		if status.Code(err) != codes.Unavailable || patches != 1 {
			t.Fatalf("unready policy retry = %v after %d patches, want Unavailable after one attempt", err, patches)
		}
	})
}

func TestDelegatedActorDoesNotInheritOperatorAuthority(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 2), executionUID)
	h := newHarness(t, objects, []string{peertls.ActionsURI})
	err := h.delegatedResume(t, 2)
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("Identity delegation without a lifecycle grant returned %v, want PermissionDenied", err)
	}
}

func TestServiceWithoutDelegationGrantCannotDelegateTokenlessResume(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 1), executionUID)
	h := newHarness(t, objects, []string{peertls.ActionsURI})
	request := &environmentpb.ResumeEnvironmentRequest{Target: targetHandle(testSandboxUID)}
	_, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(delegatedContext(t), request)
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("ungranted Identity delegation returned %v, want PermissionDenied", err)
	}
}

func TestTargetReplacementAndRequestedStopAreNotOrdinaryResumeTargets(t *testing.T) {
	t.Run("replacement UID", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 1)
		sandbox.UID = "replacement-sandbox-uid"
		objects := testIdentityObjects(t, sandbox, executionUID)
		rules := selfResumeRules(nil)
		h := newIdentityHarness(t, objects, nil, rules, func() error { return nil })
		err := h.delegatedResume(t, 1)
		if status.Code(err) != codes.NotFound {
			t.Fatalf("replacement target returned %v, want NotFound", err)
		}
	})
	t.Run("requested stop is sticky", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, false, sandboxapi.SuspendedByRequested, 1)
		objects := testIdentityObjects(t, sandbox, executionUID)
		rules := selfResumeRules(nil)
		h := newIdentityHarness(t, objects, nil, rules, func() error { return nil })
		err := h.delegatedResume(t, 1)
		if status.Code(err) != codes.PermissionDenied {
			t.Fatalf("ordinary requested-stop resume returned %v, want PermissionDenied", err)
		}
		requireRequestedStopDetails(t, err)
	})
	t.Run("recorded Requested status stays sticky during reconciliation", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByRequested, 1)
		objects := testIdentityObjects(t, sandbox, executionUID)
		rules := selfResumeRules(nil)
		h := newIdentityHarness(t, objects, nil, rules, func() error { return nil })
		err := h.delegatedResume(t, 1)
		if status.Code(err) != codes.PermissionDenied {
			t.Fatalf("recorded requested-stop resume returned %v, want PermissionDenied", err)
		}
		requireRequestedStopDetails(t, err)
	})
}

func TestDirectServiceOperatorCanResumeRequestedStopWithoutToken(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{}, false, sandboxapi.SuspendedByRequested, 7), executionUID)
	h := newHarness(t, objects, []string{peertls.AppURI})
	response, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(directContext(t, peertls.AppURI), &environmentpb.ResumeEnvironmentRequest{Target: targetHandle(testSandboxUID)})
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

func TestExplicitResumeDoesNotReserveWakeBudget(t *testing.T) {
	for _, operator := range []bool{true, false} {
		for _, unavailable := range []bool{false, true} {
			t.Run(fmt.Sprintf("operator=%t/budgetUnavailable=%t", operator, unavailable), func(t *testing.T) {
				objects := testIdentityObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 4), executionUID)
				rules := selfResumeRules(nil)
				h := newIdentityHarness(t, objects, []string{peertls.AppURI}, rules, func() error { return nil })
				if unavailable {
					h.budget.err = errors.New("budget storage unavailable")
				} else {
					h.budget.delay = 30 * time.Second
				}
				ctx := delegatedContext(t)
				if operator {
					ctx = directContext(t, peertls.AppURI)
				}
				response, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(ctx, resumeRequest(testSandboxUID, 4))
				if err != nil || response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED || response.GetWakeGeneration() != 5 {
					t.Fatalf("explicit resume with exhausted/unavailable budget = %+v, %v", response, err)
				}
				if len(h.budget.calls) != 0 {
					t.Fatalf("explicit resume reserved wake budget: %+v", h.budget.calls)
				}
			})
		}
	}
}

func TestNewManagerRequiresWakeBudget(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 4), executionUID)
	if _, err := wake.NewManager(wake.Options{
		Namespace: testNamespace, Objects: objects, Identities: kubernetesIdentities(t, objects, testNamespace),
	}); err == nil {
		t.Fatal("automatic-wake manager was constructed without a shared wake budget")
	}
}

func TestEnvironmentServiceManagerResumesWithoutWakeBudget(t *testing.T) {
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 4), executionUID)
	manager, err := resume.NewManager(lifecycle.Options{
		Namespace: testNamespace, Objects: objects, Identities: kubernetesIdentities(t, objects, testNamespace),
		WakeServiceCallers:     []string{peertls.NotificationsURI},
		OperatorServiceCallers: []string{peertls.AppURI},
	})
	if err != nil {
		t.Fatal(err)
	}
	target := &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}
	assessment, err := manager.AssessAutomaticWake(t.Context(), target, lifecycle.ServiceWakeCaller{SPIFFEID: peertls.NotificationsURI})
	if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE || assessment.GetReason() != environmentlifecyclepb.WakeReason_WAKE_REASON_ALLOWED_WAKE_BROKER {
		t.Fatalf("budgetless manager automatic-wake assessment = %+v, %v", assessment, err)
	}

	connection := serveEnvironmentService(t, objects, manager, false)
	response, err := environmentpb.NewEnvironmentServiceClient(connection).ResumeEnvironment(
		directContext(t, peertls.AppURI), resumeRequest(testSandboxUID, 4),
	)
	if err != nil || response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED || response.GetWakeGeneration() != 5 {
		t.Fatalf("budgetless manager guarded resume = %+v, %v", response, err)
	}
	var current sandboxapi.Sandbox
	if err := objects.Get(t.Context(), client.ObjectKey{Namespace: testNamespace, Name: testSandbox}, &current); err != nil {
		t.Fatal(err)
	}
	if !current.Spec.Running || current.Spec.WakeGeneration != 5 {
		t.Fatalf("guarded resume wrote running=%t generation=%d, want true and 5", current.Spec.Running, current.Spec.WakeGeneration)
	}
}

func TestIncompleteCurrentGenerationIdleSuspensionIsStillStopping(t *testing.T) {
	t.Run("current generation is still stopping", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 4)
		sandbox.Status.StopStage = ""
		objects := testObjects(t, sandbox, executionUID)
		h := newHarness(t, objects, []string{peertls.AppURI})
		response, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(
			directContext(t, peertls.AppURI), resumeRequest(testSandboxUID, 4),
		)
		if status.Code(err) != codes.FailedPrecondition || response != nil {
			t.Fatalf("resume before idle stop completed = %+v, %v; want FailedPrecondition", response, err)
		}
		st, ok := status.FromError(err)
		if !ok || len(st.Details()) != 1 {
			t.Fatalf("resume assessment details = %v", st.Details())
		}
		assessment, ok := st.Details()[0].(*environmentlifecyclepb.WakeAssessment)
		if !ok || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_FAILED ||
			assessment.GetReason() != environmentlifecyclepb.WakeReason_WAKE_REASON_QUIESCING || assessment.GetTargetGeneration() != 4 {
			t.Fatalf("resume assessment = %v; want failed quiescing at generation 4", st.Details()[0])
		}
		var current sandboxapi.Sandbox
		if err := objects.Get(t.Context(), types.NamespacedName{Namespace: testNamespace, Name: testSandbox}, &current); err != nil {
			t.Fatal(err)
		}
		if !current.Spec.Running || current.Spec.WakeGeneration != 4 || len(h.budget.calls) != 0 {
			t.Fatalf("incomplete idle resume mutated state or reserved budget: spec=%+v calls=%+v", current.Spec, h.budget.calls)
		}
	})
	t.Run("stale idle marker after an accepted wake is not blocking", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 4)
		sandbox.Spec.WakeGeneration = 5
		sandbox.Status.StopStage = ""
		objects := testObjects(t, sandbox, executionUID)
		h := newHarness(t, objects, []string{peertls.AppURI})
		response, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(
			directContext(t, peertls.AppURI), resumeRequest(testSandboxUID, 5),
		)
		if err != nil || response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_ALREADY_RUNNING || response.GetWakeGeneration() != 5 {
			t.Fatalf("resume after wake generation advanced = %+v, %v; want ALREADY_RUNNING at generation 5", response, err)
		}
		if len(h.budget.calls) != 0 {
			t.Fatalf("stale idle marker reserved wake budget: %+v", h.budget.calls)
		}
	})
}

func TestOldGenerationDoesNotAdvanceASecondWakeAndConcurrentConflictRereads(t *testing.T) {
	t.Run("old token is state changed", func(t *testing.T) {
		objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 3), executionUID)
		h := newHarness(t, objects, []string{peertls.AppURI})
		client := environmentpb.NewEnvironmentServiceClient(h.conn)
		first, err := client.ResumeEnvironment(directContext(t, peertls.AppURI), resumeRequest(testSandboxUID, 3))
		if err != nil || first.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED || first.GetWakeGeneration() != 4 {
			t.Fatalf("first resume = %+v, %v", first, err)
		}
		second, err := client.ResumeEnvironment(directContext(t, peertls.AppURI), resumeRequest(testSandboxUID, 3))
		if err != nil || second.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED || second.GetWakeGeneration() != 4 {
			t.Fatalf("stale resume = %+v, %v", second, err)
		}
		if len(h.budget.calls) != 0 {
			t.Fatalf("explicit resume reserved wake budget: %+v", h.budget.calls)
		}
	})
	t.Run("two simultaneous requests converge on one generation", func(t *testing.T) {
		sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 8)
		sandbox.ResourceVersion = "1"
		base := testObjects(t, sandbox, executionUID)
		var mu sync.Mutex
		arrived := 0
		release := make(chan struct{})
		objects := testPatchClient{WithWatch: base, patch: func(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
			mu.Lock()
			arrived++
			if arrived == 2 {
				close(release)
			}
			mu.Unlock()
			<-release
			return base.Patch(ctx, object, patch, options...)
		}}
		h := newHarness(t, objects, []string{peertls.AppURI})
		client := environmentpb.NewEnvironmentServiceClient(h.conn)
		type result struct {
			response *environmentpb.ResumeEnvironmentResponse
			err      error
		}
		results := make(chan result, 2)
		for range 2 {
			go func() {
				response, err := client.ResumeEnvironment(directContext(t, peertls.AppURI), resumeRequest(testSandboxUID, 8))
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
		h := newHarness(t, objects, []string{peertls.AppURI})
		response, err := environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(directContext(t, peertls.AppURI), resumeRequest(testSandboxUID, 5))
		if err != nil || response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED || response.GetWakeGeneration() != 6 {
			t.Fatalf("resume after conflict = %+v, %v", response, err)
		}
		if conflicts != 1 || len(h.budget.calls) != 0 {
			t.Fatalf("conflict retries=%d, budget calls=%+v; want one conflict and no reservations", conflicts, h.budget.calls)
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
	h := newHarness(t, objects, []string{peertls.AppURI})
	client := environmentpb.NewEnvironmentServiceClient(h.conn)
	_, err := client.ResumeEnvironment(directContext(t, peertls.AppURI), resumeRequest(testSandboxUID, 9))
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("lost patch reply returned %v, want Unavailable", err)
	}
	st, _ := status.FromError(err)
	wake, ok := st.Details()[0].(*environmentlifecyclepb.WakeAssessment)
	if !ok || wake.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN || wake.GetTargetGeneration() != 9 {
		t.Fatalf("unknown outcome details = %v", st.Details())
	}
	response, err := client.ResumeEnvironment(directContext(t, peertls.AppURI), resumeRequest(testSandboxUID, 9))
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
		assessment, err = h.wake.RequestWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, lifecycle.AccountWakeCaller{Account: caller})
		if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_DENIED || len(h.budget.calls) != 0 {
			t.Fatalf("RequestWake = %+v, %v, budget %+v; want denied without reservation", assessment, err, h.budget.calls)
		}
	})
	wakeIdentity := lifecycle.IdentityWakeCaller{Identity: &identitypb.Identity{Name: executionIdentityName, Uid: executionIdentityUID}}
	t.Run("budget exhausted", func(t *testing.T) {
		objects := testIdentityObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 2), executionUID)
		h := newHarness(t, objects, nil)
		h.budget.delay = 30 * time.Second
		assessment, err := h.wake.RequestWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, wakeIdentity)
		if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_RATE_LIMITED || assessment.GetRetryAt() == nil {
			t.Fatalf("rate limited RequestWake = %+v, %v", assessment, err)
		}
	})
	t.Run("budget backend error", func(t *testing.T) {
		objects := testIdentityObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{AllowOwnServiceAccount: true}, true, sandboxapi.SuspendedByIdle, 2), executionUID)
		h := newHarness(t, objects, nil)
		h.budget.err = errors.New("storage unavailable")
		assessment, err := h.wake.RequestWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, wakeIdentity)
		if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_FAILED || assessment.GetError().GetCode() != int32(codes.Unavailable) {
			t.Fatalf("budget error RequestWake = %+v, %v", assessment, err)
		}
	})
}

func TestConfiguredServiceWakeUsesItsSPIFFEIDBudget(t *testing.T) {
	serviceID := "spiffe://agentplane/notifications"
	objects := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 7), executionUID)
	budget := &testWakeBudget{}
	manager, err := wake.NewManager(wake.Options{
		Namespace: testNamespace, Objects: objects, Identities: kubernetesIdentities(t, objects, testNamespace),
		WakeBudget: budget, WakeServiceCallers: []string{serviceID},
	})
	if err != nil {
		t.Fatal(err)
	}
	target := &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}
	assessment, err := manager.RequestWake(t.Context(), target, lifecycle.ServiceWakeCaller{SPIFFEID: serviceID})
	if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_REQUESTED {
		t.Fatalf("configured service RequestWake = %+v, %v", assessment, err)
	}
	if len(budget.calls) != 1 || budget.calls[0].caller != (wake.WakeBudgetKey{ServiceSPIFFEID: serviceID}) {
		t.Fatalf("service wake budget keys = %+v, want exact SPIFFE caller", budget.calls)
	}
	assessment, err = manager.RequestWake(t.Context(), target, lifecycle.ServiceWakeCaller{SPIFFEID: "spiffe://agentplane/actions"})
	if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_DENIED || len(budget.calls) != 1 {
		t.Fatalf("unlisted service wake = %+v, %v, budget calls=%+v", assessment, err, budget.calls)
	}
}

func TestAutomaticWakeBrokerDoesNotOverrideRequestedStop(t *testing.T) {
	sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, false, sandboxapi.SuspendedByRequested, 1)
	objects := testObjects(t, sandbox, executionUID)
	h := newHarness(t, objects, nil)
	assessment, err := h.wake.RequestWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, lifecycle.ServiceWakeCaller{SPIFFEID: peertls.NotificationsURI})
	if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_DENIED || assessment.GetReason() != environmentlifecyclepb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP {
		t.Fatalf("broker wake of requested stop = %+v, %v", assessment, err)
	}
}

func TestWakeBrokerGrantRemainsAutomaticOnly(t *testing.T) {
	sandbox := suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 2)
	objects := testObjects(t, sandbox, executionUID)
	h := newHarness(t, objects, nil)
	target := &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}
	assessment, err := h.service.AssessAutomaticWake(t.Context(), target, lifecycle.ServiceWakeCaller{SPIFFEID: peertls.NotificationsURI})
	if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE || assessment.GetReason() != environmentlifecyclepb.WakeReason_WAKE_REASON_ALLOWED_WAKE_BROKER {
		t.Fatalf("broker automatic assessment = %+v, %v", assessment, err)
	}
	_, err = environmentpb.NewEnvironmentServiceClient(h.conn).ResumeEnvironment(
		directContext(t, peertls.NotificationsURI), &environmentpb.ResumeEnvironmentRequest{Target: targetHandle(testSandboxUID)},
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
	assessment, err := h.wake.RequestWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, caller)
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
			objects := clientfake.NewClientBuilder().WithScheme(testscheme.New(t)).WithObjects(sandbox, identity).Build()
			h := newHarness(t, objects, nil)
			assessment, err := h.service.AssessAutomaticWake(t.Context(), target, tc.caller)
			if err != nil || assessment.GetState() != tc.state || assessment.GetReason() != tc.reason {
				t.Fatalf("AssessAutomaticWake = %+v, %v; want %v %v", assessment, err, tc.state, tc.reason)
			}
			assessment, err = h.wake.RequestWake(t.Context(), target, tc.caller)
			if tc.state != environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE {
				if err != nil || assessment.GetState() != tc.state || assessment.GetReason() != tc.reason || len(h.budget.calls) != 0 {
					t.Fatalf("RequestWake = %+v, %v, budget %+v; want the same refusal and no reservation", assessment, err, h.budget.calls)
				}
				return
			}
			if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_REQUESTED || assessment.GetTargetGeneration() != 2 {
				t.Fatalf("RequestWake = %+v, %v", assessment, err)
			}
			want := wake.WakeBudgetKey{Namespace: testNamespace, Name: "agent", UID: identityUID}
			if len(h.budget.calls) != 1 || h.budget.calls[0].caller != want || h.budget.calls[0].target != testSandboxUID || h.budget.calls[0].gen != 2 {
				t.Fatalf("wake budget calls = %+v, want one for %+v", h.budget.calls, want)
			}
		})
	}
}

func TestAutomaticWakeConflictWithExplicitResumeAdvancesOneGeneration(t *testing.T) {
	base := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 9), executionUID)
	patches := 0
	var rpc environmentpb.EnvironmentServiceClient
	objects := testPatchClient{WithWatch: base, patch: func(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
		patches++
		if patches == 1 {
			// The automatic path has authorized and reserved generation 10. Let the separately
			// constructed explicit owner win before its patch reaches the API server.
			response, err := rpc.ResumeEnvironment(directContext(t, peertls.AppURI), resumeRequest(testSandboxUID, 9))
			if err != nil || response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED {
				return fmt.Errorf("explicit contender = %+v, %v", response, err)
			}
		}
		return base.Patch(ctx, object, patch, options...)
	}}
	h := newHarness(t, objects, []string{peertls.AppURI})
	rpc = environmentpb.NewEnvironmentServiceClient(h.conn)
	caller := lifecycle.ServiceWakeCaller{SPIFFEID: peertls.NotificationsURI}
	assessment, err := h.wake.RequestWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, caller)
	if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_NOT_NEEDED || assessment.GetTargetGeneration() != 10 {
		t.Fatalf("automatic contender = %+v, %v", assessment, err)
	}
	if patches != 2 || len(h.budget.calls) != 1 || h.budget.calls[0].gen != 10 {
		t.Fatalf("patches=%d budget=%+v, want one patch per owner and one automatic reservation", patches, h.budget.calls)
	}
}

func TestAutomaticWakeLostPatchReplyIsUnknownAndNeverReplayed(t *testing.T) {
	base := testObjects(t, suspendedSandbox(&sandboxapi.ResumePolicy{}, true, sandboxapi.SuspendedByIdle, 9), executionUID)
	patches := 0
	objects := testPatchClient{WithWatch: base, patch: func(ctx context.Context, object client.Object, patch client.Patch, options ...client.PatchOption) error {
		patches++
		if err := base.Patch(ctx, object, patch, options...); err != nil {
			return err
		}
		return errors.New("reply lost after automatic patch committed")
	}}
	h := newHarness(t, objects, nil)
	caller := lifecycle.ServiceWakeCaller{SPIFFEID: peertls.NotificationsURI}
	assessment, err := h.wake.RequestWake(t.Context(), &lifecycle.Target{Namespace: testNamespace, Name: testSandbox, Uid: testSandboxUID}, caller)
	if err != nil || assessment.GetState() != environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN || assessment.GetTargetGeneration() != 9 {
		t.Fatalf("lost automatic reply = %+v, %v", assessment, err)
	}
	var current sandboxapi.Sandbox
	if err := base.Get(t.Context(), client.ObjectKey{Namespace: testNamespace, Name: testSandbox}, &current); err != nil {
		t.Fatal(err)
	}
	if patches != 1 || len(h.budget.calls) != 1 || current.Spec.WakeGeneration != 10 {
		t.Fatalf("patches=%d budget=%+v generation=%d, want one attempt with committed generation 10", patches, h.budget.calls, current.Spec.WakeGeneration)
	}
}

// kubernetesIdentities reads namespace's Identities through objects, as the Kubernetes-backed production wiring does.
func kubernetesIdentities(t *testing.T, objects client.Reader, namespace string) auth.IdentityUIDReader {
	t.Helper()
	identities, err := auth.KubernetesIdentityUIDs(objects, namespace)
	if err != nil {
		t.Fatal(err)
	}
	return identities
}
