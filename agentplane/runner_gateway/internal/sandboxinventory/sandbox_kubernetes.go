package sandboxinventory

import (
	"context"
	"errors"
	"fmt"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
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

func (k *KubernetesSandboxes) GetSandbox(ctx context.Context, name string) (dispatcher.SandboxSnapshot, bool, error) {
	if !k.informer.HasSynced() {
		return dispatcher.SandboxSnapshot{}, false, errors.New("Sandbox inventory is not synchronized")
	}
	var object sandboxapi.Sandbox
	err := k.inventory.Get(ctx, types.NamespacedName{Namespace: k.namespace, Name: name}, &object)
	if apierrors.IsNotFound(err) {
		return dispatcher.SandboxSnapshot{}, false, nil
	}
	if err != nil {
		return dispatcher.SandboxSnapshot{}, false, err
	}
	view, err := sandboxSnapshot(&object)
	return view, err == nil, err
}

// GetExecutionIdentity returns the Identity the exact live Sandbox UID names, read uncached so
// delivery never authorizes from a stale informer snapshot.
func (k *KubernetesSandboxes) GetExecutionIdentity(ctx context.Context, sandbox dispatcher.SandboxSnapshot) (*identitypb.Identity, bool, error) {
	return k.reader.ExecutionIdentity(ctx, &sandboxreader.Identity{Namespace: sandbox.Namespace, Name: sandbox.Name, Uid: sandbox.UID})
}

// GetEnvironment reads the exact live Sandbox used to evaluate an Identity's bind_session grant.
// The shared informer remains the inventory for ordinary Dispatcher work; authorization is based
// on an uncached read and the selected UID.
func (k *KubernetesSandboxes) GetEnvironment(ctx context.Context, sandbox dispatcher.SandboxSnapshot) (*sandboxapi.Sandbox, bool, error) {
	if sandbox.Namespace != k.namespace || sandbox.Name == "" || sandbox.UID == "" {
		return nil, false, errors.New("exact Sandbox identity is required")
	}
	view, err := k.reader.Get(ctx, sandbox.Name)
	if status.Code(err) == codes.NotFound {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	if view.Identity.Namespace != sandbox.Namespace || view.Identity.Uid != sandbox.UID {
		return nil, false, nil
	}
	return view.Resource, true, nil
}

func (k *KubernetesSandboxes) ListSandboxes(ctx context.Context) ([]dispatcher.SandboxSnapshot, error) {
	if !k.informer.HasSynced() {
		return nil, errors.New("Sandbox inventory is not synchronized")
	}
	var objects sandboxapi.SandboxList
	if err := k.inventory.List(ctx, &objects, client.InNamespace(k.namespace)); err != nil {
		return nil, err
	}
	result := make([]dispatcher.SandboxSnapshot, 0, len(objects.Items))
	for i := range objects.Items {
		view, err := sandboxSnapshot(&objects.Items[i])
		if err != nil {
			return nil, err
		}
		result = append(result, view)
	}
	return result, nil
}

// CurrentSandboxUIDs confirms cleanup candidates against the API server, never
// the informer: an older list cannot justify a permanent deletion fence.
func (k *KubernetesSandboxes) CurrentSandboxUIDs(ctx context.Context) (map[string]struct{}, error) {
	var objects sandboxapi.SandboxList
	if err := k.objects.List(ctx, &objects, client.InNamespace(k.namespace)); err != nil {
		return nil, err
	}
	result := make(map[string]struct{}, len(objects.Items))
	for _, object := range objects.Items {
		if object.UID == "" {
			return nil, errors.New("Sandbox has no UID")
		}
		result[string(object.UID)] = struct{}{}
	}
	return result, nil
}

func sandboxSnapshot(object *sandboxapi.Sandbox) (dispatcher.SandboxSnapshot, error) {
	if object.Name == "" || object.UID == "" {
		return dispatcher.SandboxSnapshot{}, errors.New("Sandbox has no name or UID")
	}
	if object.Namespace == "" {
		return dispatcher.SandboxSnapshot{}, errors.New("Sandbox has no namespace")
	}
	view := dispatcher.SandboxSnapshot{
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
