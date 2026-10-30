// Package liveinventory keeps the Kubernetes inputs to the Agentplane live views in a
// controller-runtime cache and exposes copied snapshots plus watch health.
package liveinventory

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/rest"
	toolscache "k8s.io/client-go/tools/cache"
	ctrlcache "sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var (
	ErrAlreadyStarted = errors.New("live inventory has already been started")
	ErrNotSynced      = errors.New("live inventory has not completed its initial list")
)

// watched is each object kind the index caches, with its plural: the last segment of its requests' paths, which is
// the kind its health is recorded as.
var watched = []struct {
	object client.Object
	kind   watchhealth.Kind
	policy bool
}{
	{&sandboxapi.Sandbox{}, "sandboxes", false},
	{&corev1.Pod{}, "pods", false},
	{&policyapi.PolicyBinding{}, "policybindings", true},
	{&policyapi.PolicySet{}, "policysets", true},
	{&egressapi.EgressCredential{}, "egresscredentials", false},
}

// Index is a namespace-scoped controller-runtime cache of the objects that feed the /live views.
// Run owns the cache lifecycle; all returned Kubernetes objects are deep copies.
type Index struct {
	cache     ctrlcache.Cache
	namespace string
	health    *watchhealth.Health

	mu       sync.RWMutex
	synced   bool
	started  bool
	syncDone chan struct{}
	runDone  chan struct{}
	runErr   error

	policyGeneration atomic.Uint64
	changes          changeHub
}

// Snapshot is the read-only Kubernetes state consumed by live routes. Each object is copied from
// the cache so callers may safely project it after this method returns.
type Snapshot struct {
	Sandboxes         []sandboxapi.Sandbox
	Pods              []corev1.Pod
	PolicyBindings    []policyapi.PolicyBinding
	PolicySets        []policyapi.PolicySet
	EgressCredentials []egressapi.EgressCredential
	PolicyGeneration  uint64
	// Watch is the cache's health: whether it has synced and which kinds are failing to list or watch. It is reported
	// beside the last complete snapshot rather than hiding it.
	Watch watchhealth.Status
}

// New builds the namespace-scoped cache and registers generated Sandbox, Policy, Egress, and core
// Pod informers. It does not start any API requests until Run is called.
func New(config *rest.Config, namespace string) (*Index, error) {
	if config == nil || namespace == "" {
		return nil, errors.New("Kubernetes config and Sandbox namespace are required")
	}
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{
		corev1.AddToScheme, sandboxapi.AddToScheme, policyapi.AddToScheme, egressapi.AddToScheme,
	} {
		if err := add(scheme); err != nil {
			return nil, fmt.Errorf("register live inventory Kubernetes API: %w", err)
		}
	}
	index := &Index{
		namespace: namespace, health: watchhealth.New(), syncDone: make(chan struct{}), runDone: make(chan struct{}),
		changes: newChangeHub(),
	}
	byObject := make(map[client.Object]ctrlcache.ByObject, len(watched))
	// The reflector describes the type it watches as its Go type, which the watch error handler maps back to a kind.
	kindByType := make(map[string]watchhealth.Kind, len(watched))
	for _, w := range watched {
		byObject[w.object] = ctrlcache.ByObject{}
		kindByType[fmt.Sprintf("%T", w.object)] = w.kind
	}
	inventory, err := ctrlcache.New(index.health.Config(config, watchhealth.KindByPath), ctrlcache.Options{
		Scheme:            scheme,
		DefaultNamespaces: map[string]ctrlcache.Config{namespace: {}},
		ByObject:          byObject,
		DefaultWatchErrorHandler: func(ctx context.Context, r *toolscache.Reflector, err error) {
			kind, ok := kindByType[r.TypeDescription()]
			if !ok {
				kind = watchhealth.Kind(r.TypeDescription())
			}
			index.health.WatchErrorHandler(kind)(ctx, r, err)
		},
	})
	if err != nil {
		return nil, fmt.Errorf("create live inventory cache: %w", err)
	}
	index.cache = inventory
	for _, w := range watched {
		if err := index.registerInformer(context.Background(), w.object, w.policy); err != nil {
			return nil, err
		}
	}
	return index, nil
}

func (i *Index) registerInformer(ctx context.Context, object client.Object, policy bool) error {
	informer, err := i.cache.GetInformer(ctx, object)
	if err != nil {
		return fmt.Errorf("get live inventory informer for %T: %w", object, err)
	}
	_, err = informer.AddEventHandler(toolscache.ResourceEventHandlerFuncs{
		AddFunc:    func(any) { i.changed(policy) },
		UpdateFunc: func(any, any) { i.changed(policy) },
		DeleteFunc: func(any) { i.changed(policy) },
	})
	if err != nil {
		return fmt.Errorf("register live inventory handler for %T: %w", object, err)
	}
	return nil
}

func (i *Index) changed(policy bool) {
	if policy {
		i.policyGeneration.Add(1)
	}
	i.changes.notify()
}

// Run starts and owns the controller-runtime cache until ctx is canceled. It returns after cache
// shutdown; callers should run it in their service lifecycle group.
func (i *Index) Run(ctx context.Context) (result error) {
	i.mu.Lock()
	if i.started {
		i.mu.Unlock()
		return ErrAlreadyStarted
	}
	i.started = true
	i.mu.Unlock()
	defer func() {
		i.mu.Lock()
		i.runErr = result
		close(i.runDone)
		i.mu.Unlock()
	}()

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	cacheErr := make(chan error, 1)
	go func() { cacheErr <- i.cache.Start(runCtx) }()
	syncResult := make(chan bool, 1)
	go func() { syncResult <- i.cache.WaitForCacheSync(runCtx) }()
	select {
	case synced := <-syncResult:
		if !synced {
			cancel()
			result = ctx.Err()
			if result == nil {
				result = errors.New("live inventory cache stopped before initial sync")
			}
			<-cacheErr
			return result
		}
		i.mu.Lock()
		i.synced = true
		close(i.syncDone)
		i.mu.Unlock()
		i.health.MarkSynced()
		select {
		case result = <-cacheErr:
			if ctx.Err() != nil {
				return nil
			}
			return result
		case <-ctx.Done():
			result = <-cacheErr
			if errors.Is(result, context.Canceled) || result == nil {
				return nil
			}
			return result
		}
	case result = <-cacheErr:
		cancel()
		if result == nil {
			result = errors.New("live inventory cache stopped before initial sync")
		}
		return result
	case <-ctx.Done():
		cancel()
		<-cacheErr
		return ctx.Err()
	}
}

// WaitForSync waits until the initial lists have populated all registered informers.
func (i *Index) WaitForSync(ctx context.Context) error {
	select {
	case <-i.syncDone:
		return nil
	case <-i.runDone:
		i.mu.RLock()
		err := i.runErr
		synced := i.synced
		i.mu.RUnlock()
		if synced {
			return nil
		}
		if err == nil {
			err = errors.New("live inventory stopped before initial sync")
		}
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Snapshot returns all watched objects from the cache at the time of each List call. It reports
// unhealthy watches in Watch rather than hiding the last complete snapshot.
func (i *Index) Snapshot(ctx context.Context) (Snapshot, error) {
	var result Snapshot
	var sandboxes sandboxapi.SandboxList
	if err := i.cache.List(ctx, &sandboxes, client.InNamespace(i.namespace)); err != nil {
		return Snapshot{}, fmt.Errorf("list cached Sandboxes: %w", err)
	}
	result.Sandboxes = make([]sandboxapi.Sandbox, len(sandboxes.Items))
	for n := range sandboxes.Items {
		result.Sandboxes[n] = *sandboxes.Items[n].DeepCopy()
	}
	var pods corev1.PodList
	if err := i.cache.List(ctx, &pods, client.InNamespace(i.namespace)); err != nil {
		return Snapshot{}, fmt.Errorf("list cached Pods: %w", err)
	}
	result.Pods = make([]corev1.Pod, len(pods.Items))
	for n := range pods.Items {
		result.Pods[n] = *pods.Items[n].DeepCopy()
	}
	var bindings policyapi.PolicyBindingList
	if err := i.cache.List(ctx, &bindings, client.InNamespace(i.namespace)); err != nil {
		return Snapshot{}, fmt.Errorf("list cached PolicyBindings: %w", err)
	}
	result.PolicyBindings = make([]policyapi.PolicyBinding, len(bindings.Items))
	for n := range bindings.Items {
		result.PolicyBindings[n] = *bindings.Items[n].DeepCopy()
	}
	var sets policyapi.PolicySetList
	if err := i.cache.List(ctx, &sets, client.InNamespace(i.namespace)); err != nil {
		return Snapshot{}, fmt.Errorf("list cached PolicySets: %w", err)
	}
	result.PolicySets = make([]policyapi.PolicySet, len(sets.Items))
	for n := range sets.Items {
		result.PolicySets[n] = *sets.Items[n].DeepCopy()
	}
	var credentials egressapi.EgressCredentialList
	if err := i.cache.List(ctx, &credentials, client.InNamespace(i.namespace)); err != nil {
		return Snapshot{}, fmt.Errorf("list cached EgressCredentials: %w", err)
	}
	result.EgressCredentials = make([]egressapi.EgressCredential, len(credentials.Items))
	for n := range credentials.Items {
		result.EgressCredentials[n] = *credentials.Items[n].DeepCopy()
	}
	result.PolicyGeneration = i.policyGeneration.Load()
	result.Watch = i.health.Status()
	return result, nil
}

// GetSandbox returns one cached Sandbox and the Pod only when it is that Sandbox's current
// incarnation. A nil Sandbox means the name is absent.
func (i *Index) GetSandbox(ctx context.Context, name string) (*sandboxapi.Sandbox, *corev1.Pod, error) {
	var sandbox sandboxapi.Sandbox
	if err := i.cache.Get(ctx, types.NamespacedName{Namespace: i.namespace, Name: name}, &sandbox); apierrors.IsNotFound(err) {
		return nil, nil, nil
	} else if err != nil {
		return nil, nil, fmt.Errorf("read cached Sandbox %q: %w", name, err)
	}
	sandboxCopy := sandbox.DeepCopy()
	incarnation := sandbox.Status.Incarnation
	if incarnation == nil || incarnation.Name == "" || incarnation.UID == "" {
		return sandboxCopy, nil, nil
	}
	var pod corev1.Pod
	if err := i.cache.Get(ctx, types.NamespacedName{Namespace: i.namespace, Name: incarnation.Name}, &pod); apierrors.IsNotFound(err) {
		return sandboxCopy, nil, nil
	} else if err != nil {
		return nil, nil, fmt.Errorf("read cached Pod %s for Sandbox %q: %w", incarnation.Name, name, err)
	}
	if string(pod.UID) != incarnation.UID {
		return sandboxCopy, nil, nil
	}
	return sandboxCopy, pod.DeepCopy(), nil
}

// Health returns the cache's watch health, as Snapshot reports it.
func (i *Index) Health() watchhealth.Status { return i.health.Status() }

// PolicyGeneration returns the monotonic trigger changed by every PolicyBinding or PolicySet event.
func (i *Index) PolicyGeneration() uint64 { return i.policyGeneration.Load() }

// Subscribe adds an independent coalescing change channel. Unsubscribe is safe to call more than once.
func (i *Index) Subscribe() (<-chan struct{}, func()) { return i.changes.subscribe() }
