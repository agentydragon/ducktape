package view

import (
	"testing"
	"time"

	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	api "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

func TestPhaseFollowsControllerStatus(t *testing.T) {
	tests := []struct {
		name       string
		mode       api.OperatingMode
		incarnated bool
		deleting   bool
		sealed     bool
		stopStage  api.StopStage
		want       sandboxpb.SandboxPhase
	}{
		{"unknown", "", false, false, false, "", sandboxpb.SandboxPhase_SANDBOX_PHASE_UNSPECIFIED},
		{"starting", api.OperatingModeRunning, false, false, false, "", sandboxpb.SandboxPhase_SANDBOX_PHASE_CREATING},
		{"running", api.OperatingModeRunning, true, false, false, "", sandboxpb.SandboxPhase_SANDBOX_PHASE_RUNNING},
		{"suspending", api.OperatingModeSuspended, true, false, false, "", sandboxpb.SandboxPhase_SANDBOX_PHASE_SUSPENDING},
		{"suspended", api.OperatingModeSuspended, false, false, false, api.StopStageStopped, sandboxpb.SandboxPhase_SANDBOX_PHASE_SUSPENDED},
		{"quiescing", api.OperatingModeSuspended, true, false, false, api.StopStageQuiescing, sandboxpb.SandboxPhase_SANDBOX_PHASE_QUIESCING},
		{"deleting before seal", api.OperatingModeSuspended, true, true, false, "", sandboxpb.SandboxPhase_SANDBOX_PHASE_QUIESCING},
		{"deleting after seal", api.OperatingModeSuspended, false, true, true, "", sandboxpb.SandboxPhase_SANDBOX_PHASE_TEARING_DOWN},
		{"deleting after stage", api.OperatingModeSuspended, true, true, false, api.StopStageDeleting, sandboxpb.SandboxPhase_SANDBOX_PHASE_TEARING_DOWN},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			resource := &api.Sandbox{
				ObjectMeta: metav1.ObjectMeta{Namespace: "test", Name: "worker", UID: types.UID("sandbox-uid")},
				// Desired state deliberately disagrees with the effective mode.
				Spec:   api.SandboxSpec{Running: tc.mode != api.OperatingModeRunning},
				Status: api.SandboxStatus{OperatingMode: tc.mode, StopStage: tc.stopStage},
			}
			if tc.incarnated {
				resource.Status.Incarnation = &api.Incarnation{Name: "pod", UID: "pod-uid"}
			}
			if tc.deleting {
				timestamp := metav1.NewTime(time.Unix(100, 0))
				resource.DeletionTimestamp = &timestamp
			}
			if tc.sealed {
				resource.Status.Conditions = []metav1.Condition{{Type: api.ConditionQuiesced, Status: metav1.ConditionTrue}}
			}
			got, err := View(resource, nil)
			if err != nil {
				t.Fatal(err)
			}
			if got.GetPhase() != tc.want {
				t.Fatalf("phase = %v, want %v", got.GetPhase(), tc.want)
			}
			if tc.mode == "" && got.GetOperatingMode() != environmentpb.OperatingMode_OPERATING_MODE_UNSPECIFIED {
				t.Fatalf("unknown controller mode = %v", got.GetOperatingMode())
			}
		})
	}
}

func TestViewIncludesOnlyTheCurrentPodIncarnation(t *testing.T) {
	resource := &api.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: "test", Name: "worker", UID: types.UID("sandbox-uid")},
		Status:     api.SandboxStatus{Incarnation: &api.Incarnation{Name: "pod", UID: "pod-uid"}},
	}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Namespace: "test", Name: "pod", UID: types.UID("pod-uid")}}
	for _, tc := range []struct {
		name string
		pod  *corev1.Pod
		want bool
	}{
		{"current", pod, true},
		{"replacement", &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Namespace: "test", Name: "pod", UID: types.UID("new-uid")}}, false},
		{"absent", nil, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := View(resource, tc.pod)
			if err != nil {
				t.Fatal(err)
			}
			if (got.GetPod() != nil) != tc.want {
				t.Fatalf("pod = %v, want present %v", got.GetPod(), tc.want)
			}
		})
	}
}
