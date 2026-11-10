package dispatcher

import (
	"testing"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

const sandboxTestUID = "00000000-0000-4000-8000-000000000001"

func sandboxObject() *sandboxapi.Sandbox {
	suspendedAt := int64(4)
	suspendedBy := sandboxapi.SuspendedByIdle
	return &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{
			Name: "box", Namespace: "agentplane", UID: types.UID(sandboxTestUID), ResourceVersion: "1",
		},
		Spec: sandboxapi.SandboxSpec{Running: true, WakeOnSend: true, WakeGeneration: 4},
		Status: sandboxapi.SandboxStatus{
			OperatingMode:             sandboxapi.OperatingModeSuspended,
			SuspendedBy:               &suspendedBy,
			SuspendedAtWakeGeneration: &suspendedAt,
			StopStage:                 sandboxapi.StopStageStopped,
			Conditions: []metav1.Condition{{
				Type: sandboxapi.ConditionReady, Status: metav1.ConditionFalse, Reason: "Stopped", Message: "idle",
			}},
		},
	}
}

func TestSandboxSnapshotPreservesWakeAndStopFacts(t *testing.T) {
	object := sandboxObject()
	view, err := sandboxSnapshot(object)
	if err != nil || view.Blocker != "sandbox_suspended" || view.UID != sandboxTestUID ||
		view.Namespace != "agentplane" || view.ReadyDetail != "Ready=False/Stopped: idle" ||
		!view.WakeOnSend || view.WakeGeneration != 4 || view.ManualStop {
		t.Fatalf("wrong live Sandbox view: %+v %v", view, err)
	}
	requested := sandboxapi.SuspendedByRequested
	object.Status.SuspendedBy = &requested
	view, err = sandboxSnapshot(object)
	if err != nil || !view.ManualStop {
		t.Fatalf("requested stop was not preserved: %+v %v", view, err)
	}
}
