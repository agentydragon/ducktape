package dispatcher

import (
	"context"
	"errors"
	"testing"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
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

// sandboxClient writes as the Runner Gateway's ServiceAccount does: its Role grants `patch` on
// Sandboxes and not `update`, which the fake client would otherwise accept.
func sandboxClient(t *testing.T, objects ...client.Object) client.Client {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	return clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(objects...).
		WithInterceptorFuncs(interceptor.Funcs{Update: func(_ context.Context, _ client.WithWatch, obj client.Object, _ ...client.UpdateOption) error {
			return apierrors.NewForbidden(
				schema.GroupResource{Group: sandboxapi.GroupVersion.Group, Resource: "sandboxes"}, obj.GetName(),
				errors.New("the runner-gateway Role does not grant update"),
			)
		}}).Build()
}

func TestSandboxSnapshotAndGuardedIdleWake(t *testing.T) {
	ctx := context.Background()
	object := sandboxObject()
	view, err := sandboxSnapshot(object)
	if err != nil || view.Blocker != "sandbox_suspended" || view.UID != sandboxTestUID ||
		view.Namespace != "agentplane" || view.ReadyDetail != "Ready=False/Stopped: idle" {
		t.Fatalf("wrong live Sandbox view: %+v %v", view, err)
	}
	objects := sandboxClient(t, object)
	k := &KubernetesSandboxes{objects: objects, namespace: "agentplane"}
	var actual sandboxapi.Sandbox
	if err := objects.Get(ctx, types.NamespacedName{Namespace: "agentplane", Name: "box"}, &actual); err != nil || string(actual.UID) != sandboxTestUID {
		t.Fatalf("fake Sandbox UID = %q, error = %v", actual.UID, err)
	}
	called := 0
	requested, err := k.WakeForSend(ctx, "box", sandboxTestUID, func(generation int64) error {
		called++
		if generation != 5 {
			t.Fatalf("wake generation = %d", generation)
		}
		return nil
	})
	if err != nil || !requested || called != 1 {
		t.Fatalf("wake: requested=%v callback=%d err=%v", requested, called, err)
	}
	if err := objects.Get(ctx, types.NamespacedName{Namespace: "agentplane", Name: "box"}, &actual); err != nil {
		t.Fatal(err)
	}
	if actual.Spec.WakeGeneration != 5 || !actual.Spec.Running {
		t.Fatalf("wake generation or running state was not updated: %+v", actual.Spec)
	}
	if _, err := k.WakeForSend(ctx, "box", "another-uid", nil); !errors.Is(err, ErrWakeDeleted) {
		t.Fatalf("stale UID was not rejected: %v", err)
	}
}

func TestWakeDisabledDoesNotCallReservation(t *testing.T) {
	ctx := context.Background()
	object := sandboxObject()
	object.Spec.WakeOnSend = false
	k := &KubernetesSandboxes{objects: sandboxClient(t, object), namespace: "agentplane"}
	requested, err := k.WakeForSend(ctx, "box", sandboxTestUID, func(int64) error {
		t.Fatal("disabled wake reserved a budget")
		return nil
	})
	if err != nil || requested {
		t.Fatalf("disabled wake: requested=%v err=%v", requested, err)
	}
}

func TestWakeRequiresSuspendedGenerationToMatchForIdle(t *testing.T) {
	ctx := context.Background()
	object := sandboxObject()
	object.Status.SuspendedAtWakeGeneration = nil
	objects := sandboxClient(t, object)
	k := &KubernetesSandboxes{objects: objects, namespace: "agentplane"}
	requested, err := k.WakeForSend(ctx, "box", sandboxTestUID, func(int64) error {
		t.Fatal("a running Sandbox without the suspension generation was treated as idle")
		return nil
	})
	if err != nil || !requested {
		t.Fatalf("running Sandbox with absent suspension generation: requested=%v err=%v", requested, err)
	}
	var actual sandboxapi.Sandbox
	if err := objects.Get(ctx, types.NamespacedName{Namespace: "agentplane", Name: "box"}, &actual); err != nil {
		t.Fatal(err)
	}
	if actual.Spec.WakeGeneration != 4 {
		t.Fatalf("absent suspension generation changed wake generation: %d", actual.Spec.WakeGeneration)
	}
}
