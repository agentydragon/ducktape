package inventory

import (
	"context"
	"errors"
	"testing"
	"time"

	sandboxview "github.com/agentydragon/ducktape/agentplane/sandboxes/view"

	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const namespace = "test-sandboxes"

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{
		corev1.AddToScheme, sandboxapi.AddToScheme,
	} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	return scheme
}

func newAccess(t *testing.T, objects ...client.Object) (*Access, client.Client) {
	t.Helper()
	underlying := clientfake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(objects...).Build()
	access, err := New(namespace, underlying)
	if err != nil {
		t.Fatal(err)
	}
	return access, underlying
}

func testSandbox(name, uid string, mode sandboxapi.OperatingMode) *sandboxapi.Sandbox {
	return &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: namespace, Name: name, UID: types.UID(uid),
			CreationTimestamp: metav1.NewTime(time.Date(2026, 10, 20, 1, 2, 3, 0, time.UTC)),
		},
		Spec:   sandboxapi.SandboxSpec{Template: "pod", Running: mode == sandboxapi.OperatingModeRunning},
		Status: sandboxapi.SandboxStatus{OperatingMode: mode},
	}
}

func TestListGetUseCRDsAndOnlyJoinCurrentPodUID(t *testing.T) {
	first := testSandbox("alpha", "sandbox-alpha", sandboxapi.OperatingModeRunning)
	first.Spec.Bootstrap = "echo boot"
	first.Spec.WakeOnSend = true
	first.Spec.IdleSuspendAfter = &metav1.Duration{Duration: 2 * time.Minute}
	deadline := metav1.NewTime(time.Date(2026, 10, 20, 1, 4, 3, 0, time.UTC))
	first.Status.IdleSuspendAt = &deadline
	activeSession := "active-session"
	first.Status.IdleSuspendBlockers = []sandboxapi.AutosuspendBlocker{
		{Reason: sandboxapi.AutosuspendBlockerActiveTurn, SessionID: &activeSession},
		{Reason: sandboxapi.AutosuspendBlockerBootstrap},
	}
	first.Labels = map[string]string{sandboxview.PresetLabel: "preset-a"}
	first.Status.Incarnation = &sandboxapi.Incarnation{Name: "alpha", UID: "pod-alpha"}
	first.Status.Conditions = []metav1.Condition{{Type: sandboxapi.ConditionReady, Status: metav1.ConditionTrue, Reason: "PodReady"}}
	missingPod := testSandbox("beta", "sandbox-beta", sandboxapi.OperatingModeRunning)
	missingPod.Status.Incarnation = &sandboxapi.Incarnation{Name: "beta", UID: "old-pod"}
	paused := testSandbox("paused", "sandbox-paused", sandboxapi.OperatingModeSuspended)
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "alpha", Namespace: namespace, UID: "pod-alpha"},
		Spec:       corev1.PodSpec{NodeName: "node-a"},
		Status:     corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "10.0.0.7"},
	}
	wrongIncarnation := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "beta", Namespace: namespace, UID: "new-pod"},
		Status:     corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "10.0.0.8"},
	}
	access, _ := newAccess(t, first, missingPod, paused, pod, wrongIncarnation)
	views, err := access.ListSandboxes(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(views) != 3 || views[0].Name != "alpha" || views[1].Name != "beta" || views[2].Name != "paused" {
		t.Fatalf("sandbox views = %#v", views)
	}
	if views[0].GetPod() == nil || views[0].GetPod().GetUid() != "pod-alpha" || views[0].GetPod().GetStatus().GetFields()["podIP"].GetStringValue() != "10.0.0.7" {
		t.Fatalf("alpha Pod view = %#v", views[0].GetPod())
	}
	if views[0].GetIdleSuspendAt() == nil || !views[0].GetIdleSuspendAt().AsTime().Equal(deadline.Time) || views[1].GetIdleSuspendAt() != nil {
		t.Fatalf("Sandbox idle deadlines = %v, %v", views[0].GetIdleSuspendAt(), views[1].GetIdleSuspendAt())
	}
	blockers := views[0].GetAutosuspendBlockers()
	if len(blockers) != 2 || blockers[0].GetReason() != string(sandboxapi.AutosuspendBlockerActiveTurn) ||
		blockers[0].GetSessionId() != activeSession || blockers[1].GetReason() != string(sandboxapi.AutosuspendBlockerBootstrap) ||
		blockers[1].SessionId != nil {
		t.Fatalf("alpha autosuspend blockers = %#v", blockers)
	}
	if views[1].AutosuspendBlockers == nil || len(views[1].AutosuspendBlockers) != 0 {
		t.Fatalf("Sandbox without blockers has %#v, want an initialized empty list", views[1].AutosuspendBlockers)
	}
	if views[1].GetPod() != nil {
		t.Fatalf("stale Pod UID was joined: %#v", views[1].GetPod())
	}
	if views[0].GetPhase() != sandboxespb.SandboxPhase_SANDBOX_PHASE_RUNNING ||
		views[0].GetServiceAccount().GetName() != "alpha" || views[0].GetBinding().GetBootstrap() != "echo boot" ||
		views[0].GetPreset() != "preset-a" || !views[0].GetWakeOnSend() || views[0].GetIdleSuspendAfter().AsDuration() != 2*time.Minute {
		t.Fatalf("alpha Sandbox view = %#v", views[0])
	}
	if views[2].GetPhase() != sandboxespb.SandboxPhase_SANDBOX_PHASE_SUSPENDED {
		t.Fatalf("paused phase = %v", views[2].GetPhase())
	}
	got, err := access.Get(context.Background(), "alpha")
	if err != nil {
		t.Fatal(err)
	}
	if got.GetUid() != "sandbox-alpha" || got.GetPod().GetUid() != "pod-alpha" || got.GetIdleSuspendAt() == nil || !got.GetIdleSuspendAt().AsTime().Equal(deadline.Time) {
		t.Fatalf("Get(alpha) = %#v", got)
	}
	if _, err := access.Get(context.Background(), "absent"); !isNotFound(err) {
		t.Fatalf("Get(absent) = %v, want SandboxNotFoundError", err)
	}
}

func TestSandboxPodViewIncludesEmptyStatus(t *testing.T) {
	resource := testSandbox("empty", "sandbox-uid", sandboxapi.OperatingModeRunning)
	resource.Status.Incarnation = &sandboxapi.Incarnation{Name: "empty", UID: "pod-uid"}
	snapshot, err := sandboxview.View(resource, &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "empty", Namespace: namespace, UID: "pod-uid"}})
	view := snapshot.GetPod()
	if err != nil {
		t.Fatal(err)
	}
	if view.Status == nil {
		t.Fatal("empty Kubernetes Pod status lost its Struct presence")
	}
}

func TestViewUsesOnlyTheReportedPodIncarnation(t *testing.T) {
	resource := testSandbox("worker", "sandbox-uid", sandboxapi.OperatingModeRunning)
	resource.Status.Incarnation = &sandboxapi.Incarnation{Name: "worker", UID: "current-pod"}
	current := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "worker", Namespace: namespace, UID: "current-pod"}}
	stale := current.DeepCopy()
	stale.UID = "stale-pod"

	view, err := sandboxview.View(resource, current)
	if err != nil || view.GetPod() == nil || view.GetPod().GetUid() != "current-pod" {
		t.Fatalf("View(current Pod) = %#v, %v", view, err)
	}
	view, err = sandboxview.View(resource, stale)
	if err != nil || view.GetPod() != nil {
		t.Fatalf("View(stale Pod) = %#v, %v; stale incarnation must not be joined", view, err)
	}
}

func isNotFound(err error) bool {
	var target *SandboxNotFoundError
	return errors.As(err, &target)
}
