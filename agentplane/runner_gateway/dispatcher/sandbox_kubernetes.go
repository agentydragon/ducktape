package dispatcher

import (
	"context"
	"errors"
	"fmt"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	toolscache "k8s.io/client-go/tools/cache"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// KubernetesSandboxes provides a complete live namespace view after the first
// list and watches later changes. Guarded wake updates use the API client; neither
// History nor the runner process list supplies Sandbox identity.
type KubernetesSandboxes struct {
	objects   client.Client
	inventory cache.Cache
	informer  cache.Informer
	namespace string
	changed   chan struct{}
	reader    *sandboxreader.Reader
}

func NewKubernetesSandboxes(inventory cache.Cache, objects client.Client, namespace string) (*KubernetesSandboxes, error) {
	if inventory == nil || objects == nil || namespace == "" {
		return nil, errors.New("Sandbox cache, client and namespace are required")
	}
	informer, err := inventory.GetInformer(context.Background(), &sandboxapi.Sandbox{})
	if err != nil {
		return nil, err
	}
	reader, err := sandboxreader.New(namespace, objects)
	if err != nil {
		return nil, err
	}
	result := &KubernetesSandboxes{
		objects: objects, inventory: inventory, informer: informer,
		namespace: namespace, changed: make(chan struct{}, 1), reader: reader,
	}
	_, err = informer.AddEventHandler(toolscache.ResourceEventHandlerFuncs{
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

func (k *KubernetesSandboxes) Changes() <-chan struct{} { return k.changed }
func (k *KubernetesSandboxes) WaitSynced(ctx context.Context) bool {
	return k.inventory.WaitForCacheSync(ctx)
}

func (k *KubernetesSandboxes) GetSandbox(ctx context.Context, name string) (SandboxSnapshot, bool, error) {
	if !k.informer.HasSynced() {
		return SandboxSnapshot{}, false, errors.New("Sandbox inventory is not synchronized")
	}
	var object sandboxapi.Sandbox
	err := k.inventory.Get(ctx, types.NamespacedName{Namespace: k.namespace, Name: name}, &object)
	if apierrors.IsNotFound(err) {
		return SandboxSnapshot{}, false, nil
	}
	if err != nil {
		return SandboxSnapshot{}, false, err
	}
	view, err := sandboxSnapshot(&object)
	return view, err == nil, err
}

// GetExecutionAccount returns only the ServiceAccount controlled by the exact live Sandbox UID.
// The uncached object client is deliberate: writes and queued delivery must not authorize from an
// old informer snapshot after the managed account has been replaced.
func (k *KubernetesSandboxes) GetExecutionAccount(ctx context.Context, sandbox SandboxSnapshot) (ServiceAccountIdentity, bool, error) {
	account, found, err := k.reader.ExecutionAccount(ctx, sandboxreader.Identity{
		Namespace: sandbox.Namespace, Name: sandbox.Name, UID: sandbox.UID,
	})
	if err != nil || !found {
		return ServiceAccountIdentity{}, found, err
	}
	return ServiceAccountIdentity{Account: Account{Namespace: account.Namespace, Name: account.Name}, UID: account.UID}, true, nil
}

func (k *KubernetesSandboxes) ListSandboxes(ctx context.Context) ([]SandboxSnapshot, error) {
	if !k.informer.HasSynced() {
		return nil, errors.New("Sandbox inventory is not synchronized")
	}
	var objects sandboxapi.SandboxList
	if err := k.inventory.List(ctx, &objects, client.InNamespace(k.namespace)); err != nil {
		return nil, err
	}
	result := make([]SandboxSnapshot, 0, len(objects.Items))
	for i := range objects.Items {
		view, err := sandboxSnapshot(&objects.Items[i])
		if err != nil {
			return nil, err
		}
		result = append(result, view)
	}
	return result, nil
}

func sandboxSnapshot(object *sandboxapi.Sandbox) (SandboxSnapshot, error) {
	if object.Name == "" || object.UID == "" {
		return SandboxSnapshot{}, errors.New("Sandbox has no name or UID")
	}
	if object.Namespace == "" {
		return SandboxSnapshot{}, errors.New("Sandbox has no namespace")
	}
	view := SandboxSnapshot{
		Name: object.Name, Namespace: object.Namespace, UID: string(object.UID),
		Deleting:       object.DeletionTimestamp != nil,
		WakeOnSend:     object.Spec.WakeOnSend,
		WakeGeneration: object.Spec.WakeGeneration,
		ManualStop:     object.Status.SuspendedBy != nil && *object.Status.SuspendedBy == sandboxapi.SuspendedByRequested,
		ReadyDetail:    "Ready condition is unavailable",
	}
	if object.Status.Incarnation != nil {
		view.Incarnation = object.Status.Incarnation.UID
	}
	ready := false
	if condition := meta.FindStatusCondition(object.Status.Conditions, sandboxapi.ConditionReady); condition != nil {
		view.ReadyDetail = fmt.Sprintf("Ready=%s/%s: %s", condition.Status, condition.Reason, condition.Message)
		ready = condition.Status == metav1.ConditionTrue
	}
	if !ready {
		view.Blocker = "sandbox_not_ready"
		if object.Status.OperatingMode == sandboxapi.OperatingModeSuspended && object.Status.StopStage == sandboxapi.StopStageStopped {
			view.Blocker = "sandbox_suspended"
		}
	}
	return view, nil
}
