package view

import (
	"testing"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/environmentpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

const namespace = "test-sandboxes"

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

func TestSandboxPodViewIncludesEmptyStatus(t *testing.T) {
	resource := testSandbox("empty", "sandbox-uid", sandboxapi.OperatingModeRunning)
	resource.Status.Incarnation = &sandboxapi.Incarnation{Name: "empty", UID: "pod-uid"}
	snapshot, err := View(resource, &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "empty", Namespace: namespace, UID: "pod-uid"}})
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

	view, err := View(resource, current)
	if err != nil || view.GetPod() == nil || view.GetPod().GetUid() != "current-pod" {
		t.Fatalf("View(current Pod) = %#v, %v", view, err)
	}
	view, err = View(resource, stale)
	if err != nil || view.GetPod() != nil {
		t.Fatalf("View(stale Pod) = %#v, %v; stale incarnation must not be joined", view, err)
	}
}

func TestPhaseFollowsControllerModeIncarnationAndDeletionProgress(t *testing.T) {
	for _, test := range []struct {
		name       string
		mode       sandboxapi.OperatingMode
		incarnated bool
		deleting   bool
		stopStage  sandboxapi.StopStage
		sealed     bool
		want       environmentpb.SandboxPhase
	}{
		{name: "awaiting controller", want: environmentpb.SandboxPhase_SANDBOX_PHASE_UNSPECIFIED},
		{name: "creating", mode: sandboxapi.OperatingModeRunning, want: environmentpb.SandboxPhase_SANDBOX_PHASE_CREATING},
		{name: "running", mode: sandboxapi.OperatingModeRunning, incarnated: true, want: environmentpb.SandboxPhase_SANDBOX_PHASE_RUNNING},
		{name: "suspending", mode: sandboxapi.OperatingModeSuspended, incarnated: true, want: environmentpb.SandboxPhase_SANDBOX_PHASE_SUSPENDING},
		{name: "suspended", mode: sandboxapi.OperatingModeSuspended, want: environmentpb.SandboxPhase_SANDBOX_PHASE_SUSPENDED},
		{name: "quiescing suspension", mode: sandboxapi.OperatingModeSuspended, incarnated: true, stopStage: sandboxapi.StopStageQuiescing, want: environmentpb.SandboxPhase_SANDBOX_PHASE_QUIESCING},
		{name: "quiescing deletion", deleting: true, want: environmentpb.SandboxPhase_SANDBOX_PHASE_QUIESCING},
		{name: "sealed deletion", deleting: true, sealed: true, want: environmentpb.SandboxPhase_SANDBOX_PHASE_TEARING_DOWN},
		{name: "deleting workload", deleting: true, stopStage: sandboxapi.StopStageDeleting, want: environmentpb.SandboxPhase_SANDBOX_PHASE_TEARING_DOWN},
	} {
		t.Run(test.name, func(t *testing.T) {
			resource := testSandbox("worker", "sandbox-uid", test.mode)
			resource.Status.StopStage = test.stopStage
			if test.incarnated {
				resource.Status.Incarnation = &sandboxapi.Incarnation{Name: "worker", UID: "pod-uid"}
			}
			if test.deleting {
				now := metav1.Now()
				resource.DeletionTimestamp = &now
			}
			if test.sealed {
				resource.Status.Conditions = []metav1.Condition{{Type: sandboxapi.ConditionQuiesced, Status: metav1.ConditionTrue}}
			}
			descriptor, err := View(resource, nil)
			if err != nil || descriptor.GetPhase() != test.want {
				t.Fatalf("phase = %v, %v; want %v", descriptor.GetPhase(), err, test.want)
			}
		})
	}
}
