package dispatcher

import (
	"context"
	"errors"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic/fake"
)

const sandboxTestUID = "00000000-0000-4000-8000-000000000001"

func sandboxObject() *unstructured.Unstructured {
	return &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": "agentplane.allegedly.works/v1alpha1", "kind": "Sandbox",
		"metadata": map[string]any{
			"name": "box", "namespace": "agentplane", "uid": sandboxTestUID,
			"resourceVersion": "1",
		},
		"spec": map[string]any{"running": true, "wakeOnSend": true, "wakeGeneration": int64(4)},
		"status": map[string]any{
			"operatingMode": "Suspended", "suspendedBy": "Idle",
			"suspendedAtWakeGeneration": int64(4), "conditions": []any{
				map[string]any{"type": "Ready", "status": "False", "reason": "Stopped", "message": "idle"},
			},
		},
	}}
}

func TestSandboxSnapshotAndGuardedIdleWake(t *testing.T) {
	ctx := context.Background()
	object := sandboxObject()
	view, err := sandboxSnapshot(object)
	if err != nil || view.Blocker != "sandbox_suspended" || view.UID != sandboxTestUID ||
		view.Namespace != "agentplane" || view.ReadyDetail != "Ready=False/Stopped: idle" {
		t.Fatalf("wrong live Sandbox view: %+v %v", view, err)
	}
	client := fake.NewSimpleDynamicClientWithCustomListKinds(runtime.NewScheme(), map[schema.GroupVersionResource]string{
		sandboxResource: "SandboxList",
	})
	k := &KubernetesSandboxes{objects: client.Resource(sandboxResource).Namespace("agentplane")}
	if _, err := k.objects.Create(ctx, object, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	if actual, err := k.objects.Get(ctx, "box", metav1.GetOptions{}); err != nil {
		t.Fatalf("fake Sandbox missing: %v", err)
	} else if string(actual.GetUID()) != sandboxTestUID {
		t.Fatalf("fake Sandbox UID = %q, object = %+v", actual.GetUID(), actual.Object)
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
	updated, err := k.objects.Get(ctx, "box", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	generation, _, err := unstructured.NestedInt64(updated.Object, "spec", "wakeGeneration")
	if err != nil || generation != 5 {
		t.Fatalf("wake generation was not patched: %d %v", generation, err)
	}
	if _, err := k.WakeForSend(ctx, "box", "another-uid", nil); !errors.Is(err, ErrWakeDeleted) {
		t.Fatalf("stale UID was not rejected: %v", err)
	}
}

func TestWakeDisabledDoesNotCallReservation(t *testing.T) {
	ctx := context.Background()
	object := sandboxObject()
	if err := unstructured.SetNestedField(object.Object, false, "spec", "wakeOnSend"); err != nil {
		t.Fatal(err)
	}
	client := fake.NewSimpleDynamicClientWithCustomListKinds(runtime.NewScheme(), map[schema.GroupVersionResource]string{
		sandboxResource: "SandboxList",
	})
	k := &KubernetesSandboxes{objects: client.Resource(sandboxResource).Namespace("agentplane")}
	if _, err := k.objects.Create(ctx, object, metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	if actual, err := k.objects.Get(ctx, "box", metav1.GetOptions{}); err != nil || string(actual.GetUID()) != sandboxTestUID {
		t.Fatalf("fake Sandbox = %+v, error = %v", actual, err)
	}
	requested, err := k.WakeForSend(ctx, "box", sandboxTestUID, func(int64) error {
		t.Fatal("disabled wake reserved a budget")
		return nil
	})
	if err != nil || requested {
		t.Fatalf("disabled wake: requested=%v err=%v", requested, err)
	}
}
