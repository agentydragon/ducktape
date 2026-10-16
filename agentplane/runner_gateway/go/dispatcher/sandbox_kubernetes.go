package dispatcher

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/tools/cache"
)

var sandboxResource = schema.GroupVersionResource{Group: "agentplane.allegedly.works", Version: "v1alpha1", Resource: "sandboxes"}

// KubernetesSandboxes provides a complete live namespace view after the first
// list and watches later changes. The same namespaced client performs guarded
// wake patches; neither History nor the runner process list supplies identity.
type KubernetesSandboxes struct {
	objects  dynamic.ResourceInterface
	informer cache.SharedIndexInformer
	changed  chan struct{}
}

func NewKubernetesSandboxes(client dynamic.Interface, namespace string) (*KubernetesSandboxes, error) {
	if client == nil || namespace == "" {
		return nil, errors.New("Sandbox client and namespace are required")
	}
	objects := client.Resource(sandboxResource).Namespace(namespace)
	listWatch := &cache.ListWatch{
		ListWithContextFunc: func(ctx context.Context, options metav1.ListOptions) (runtime.Object, error) {
			return objects.List(ctx, options)
		},
		WatchFuncWithContext: func(ctx context.Context, options metav1.ListOptions) (watch.Interface, error) {
			return objects.Watch(ctx, options)
		},
	}
	informer := cache.NewSharedIndexInformer(listWatch, &unstructured.Unstructured{}, 60*time.Second, cache.Indexers{})
	result := &KubernetesSandboxes{objects: objects, informer: informer, changed: make(chan struct{}, 1)}
	_, err := informer.AddEventHandler(cache.ResourceEventHandlerFuncs{
		AddFunc:    func(any) { result.signal() },
		UpdateFunc: func(any, any) { result.signal() },
		DeleteFunc: func(any) { result.signal() },
	})
	if err != nil {
		return nil, err
	}
	return result, nil
}

func (k *KubernetesSandboxes) signal() {
	select {
	case k.changed <- struct{}{}:
	default:
	}
}

func (k *KubernetesSandboxes) Run(ctx context.Context) { k.informer.Run(ctx.Done()) }
func (k *KubernetesSandboxes) WaitSynced(ctx context.Context) bool {
	return cache.WaitForCacheSync(ctx.Done(), k.informer.HasSynced)
}
func (k *KubernetesSandboxes) Changes() <-chan struct{} { return k.changed }

func (k *KubernetesSandboxes) GetSandbox(_ context.Context, name string) (SandboxSnapshot, bool, error) {
	if !k.informer.HasSynced() {
		return SandboxSnapshot{}, false, errors.New("Sandbox inventory is not synchronized")
	}
	for _, item := range k.informer.GetStore().List() {
		object, okay := item.(*unstructured.Unstructured)
		if !okay {
			return SandboxSnapshot{}, false, fmt.Errorf("unexpected Sandbox cache object %T", item)
		}
		if object.GetName() == name {
			view, err := sandboxSnapshot(object)
			return view, err == nil, err
		}
	}
	return SandboxSnapshot{}, false, nil
}

func (k *KubernetesSandboxes) ListSandboxes(context.Context) ([]SandboxSnapshot, error) {
	if !k.informer.HasSynced() {
		return nil, errors.New("Sandbox inventory is not synchronized")
	}
	items := k.informer.GetStore().List()
	result := make([]SandboxSnapshot, 0, len(items))
	for _, item := range items {
		object, okay := item.(*unstructured.Unstructured)
		if !okay {
			return nil, fmt.Errorf("unexpected Sandbox cache object %T", item)
		}
		view, err := sandboxSnapshot(object)
		if err != nil {
			return nil, err
		}
		result = append(result, view)
	}
	return result, nil
}

func nestedString(object *unstructured.Unstructured, path ...string) (string, error) {
	value, _, err := unstructured.NestedString(object.Object, path...)
	return value, err
}

func sandboxSnapshot(object *unstructured.Unstructured) (SandboxSnapshot, error) {
	if object.GetName() == "" || object.GetUID() == "" {
		return SandboxSnapshot{}, errors.New("Sandbox has no name or UID")
	}
	if object.GetNamespace() == "" {
		return SandboxSnapshot{}, errors.New("Sandbox has no namespace")
	}
	view := SandboxSnapshot{Name: object.GetName(), Namespace: object.GetNamespace(), UID: string(object.GetUID()), Deleting: object.GetDeletionTimestamp() != nil}
	var err error
	view.WakeOnSend, _, err = unstructured.NestedBool(object.Object, "spec", "wakeOnSend")
	if err != nil {
		return SandboxSnapshot{}, err
	}
	view.Incarnation, err = nestedString(object, "status", "incarnation", "uid")
	if err != nil {
		return SandboxSnapshot{}, err
	}
	mode, err := nestedString(object, "status", "operatingMode")
	if err != nil {
		return SandboxSnapshot{}, err
	}
	conditions, _, err := unstructured.NestedSlice(object.Object, "status", "conditions")
	if err != nil {
		return SandboxSnapshot{}, err
	}
	view.ReadyDetail = "Ready condition is unavailable"
	ready, stopped := false, false
	for _, raw := range conditions {
		condition, okay := raw.(map[string]any)
		if !okay {
			return SandboxSnapshot{}, errors.New("malformed Sandbox condition")
		}
		if condition["type"] != "Ready" {
			continue
		}
		state, _ := condition["status"].(string)
		reason, _ := condition["reason"].(string)
		message, _ := condition["message"].(string)
		view.ReadyDetail = fmt.Sprintf("Ready=%s/%s: %s", state, reason, message)
		ready, stopped = state == "True", reason == "Stopped"
		break
	}
	if !ready {
		view.Blocker = "sandbox_not_ready"
		if mode == "Suspended" && stopped {
			view.Blocker = "sandbox_suspended"
		}
	}
	return view, nil
}

// WakeForSend always rereads the exact Sandbox UID immediately before deciding
// to patch. A conflict repeats that entire read/decision, including the durable
// callback; the callback is idempotent for one wake generation.
func (k *KubernetesSandboxes) WakeForSend(ctx context.Context, name, uid string, beforeResume func(int64) error) (bool, error) {
	for attempt := 0; attempt < 5; attempt++ {
		object, err := k.objects.Get(ctx, name, metav1.GetOptions{})
		if apierrors.IsNotFound(err) {
			return false, ErrWakeDeleted
		}
		if err != nil {
			return false, err
		}
		if string(object.GetUID()) != uid {
			return false, ErrWakeDeleted
		}
		if object.GetDeletionTimestamp() != nil {
			return false, ErrWakeDeleted
		}
		allowed, _, err := unstructured.NestedBool(object.Object, "spec", "wakeOnSend")
		if err != nil {
			return false, err
		}
		if !allowed {
			return false, nil
		}
		conditions, _, err := unstructured.NestedSlice(object.Object, "status", "conditions")
		if err != nil {
			return false, err
		}
		for _, raw := range conditions {
			condition, okay := raw.(map[string]any)
			if !okay {
				return false, errors.New("malformed Sandbox condition")
			}
			if condition["type"] == "Running" && condition["reason"] == "Quiescing" {
				return false, ErrWakeQuiescing
			}
		}
		mode, err := nestedString(object, "status", "operatingMode")
		if err != nil {
			return false, err
		}
		if mode == "" {
			return false, ErrWakeModeUnknown
		}
		if mode == "Running" {
			return true, nil
		}
		if mode != "Suspended" {
			return false, ErrWakeModeUnknown
		}
		running, _, err := unstructured.NestedBool(object.Object, "spec", "running")
		if err != nil {
			return false, err
		}
		generation, _, err := unstructured.NestedInt64(object.Object, "spec", "wakeGeneration")
		if err != nil {
			return false, err
		}
		suspendedBy, err := nestedString(object, "status", "suspendedBy")
		if err != nil {
			return false, err
		}
		atGeneration, found, err := unstructured.NestedInt64(object.Object, "status", "suspendedAtWakeGeneration")
		if err != nil {
			return false, err
		}
		idle := suspendedBy == "Idle" && found && atGeneration == generation
		if running && !idle {
			return true, nil
		}
		if beforeResume != nil {
			if err := beforeResume(generation + 1); err != nil {
				return false, err
			}
		}
		patch, err := json.Marshal(map[string]any{"metadata": map[string]any{
			"uid": uid, "resourceVersion": object.GetResourceVersion(),
		}, "spec": map[string]any{"running": true, "wakeGeneration": generation + 1}})
		if err != nil {
			return false, err
		}
		_, err = k.objects.Patch(ctx, name, types.MergePatchType, patch, metav1.PatchOptions{})
		if apierrors.IsConflict(err) {
			continue
		}
		if err != nil {
			return false, err
		}
		return true, nil
	}
	return false, fmt.Errorf("Sandbox wake could not pass UID/resourceVersion guard after 5 attempts")
}
