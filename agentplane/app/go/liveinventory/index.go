// Package liveinventory keeps the Kubernetes inputs to the Agentplane live views in a
// controller-runtime cache and exposes copied snapshots plus watch health.
package liveinventory

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/rest"
	toolscache "k8s.io/client-go/tools/cache"
	ctrlcache "sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	SandboxesKind         = "sandboxes"
	PodsKind              = "pods"
	PolicyBindingsKind    = "policybindings.agentplane.allegedly.works"
	PolicySetsKind        = "policysets.agentplane.allegedly.works"
	EgressCredentialsKind = "egresscredentials"
	defaultWatchPeriod    = 5 * time.Minute
)

var (
	ErrAlreadyStarted = errors.New("live inventory has already been started")
	ErrNotSynced      = errors.New("live inventory has not completed its initial list")
)

var watchedKinds = []string{SandboxesKind, PodsKind, PolicyBindingsKind, PolicySetsKind, EgressCredentialsKind}

// Option configures the watch cycle and health clock.
type Option func(*options)

type options struct {
	watchPeriod time.Duration
	now         func() time.Time
}

// WithWatchPeriod bounds each watch request. client-go owns list, watch, reconnect, and relist;
// this period is carried as the API server's timeoutSeconds watch option.
func WithWatchPeriod(period time.Duration) Option { return func(o *options) { o.watchPeriod = period } }

// WithClock injects the health clock. It is useful for deterministic tests.
func WithClock(now func() time.Time) Option { return func(o *options) { o.now = now } }

// Index is a namespace-scoped controller-runtime cache of the objects that feed the /live views.
// Run owns the cache lifecycle; all returned Kubernetes objects are deep copies.
type Index struct {
	cache      ctrlcache.Cache
	namespace  string
	period     time.Duration
	staleAfter time.Duration
	now        func() time.Time

	mu        sync.RWMutex
	cycles    map[string]time.Time
	listRuns  map[string]uint64
	cycleRuns map[string]uint64
	synced    bool
	started   bool
	syncDone  chan struct{}
	runDone   chan struct{}
	runErr    error

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
	Watch             WatchHealth
}

// WatchHealth reports whether the watched cache is fresh and the last completed cycle age for
// each resource kind. Synced remains true after the initial list even if a later watch wedges.
type WatchHealth struct {
	Fresh               bool               `json:"fresh"`
	Synced              bool               `json:"synced"`
	StaleAfterSeconds   float64            `json:"stale_after_seconds"`
	RefreshedSecondsAgo map[string]float64 `json:"refreshed_seconds_ago"`
}

// New builds the namespace-scoped cache and registers generated Sandbox, Policy, Egress, and core
// Pod informers. It does not start any API requests until Run is called.
func New(config *rest.Config, namespace string, opts ...Option) (*Index, error) {
	if config == nil || namespace == "" {
		return nil, errors.New("Kubernetes config and Sandbox namespace are required")
	}
	settings := options{watchPeriod: defaultWatchPeriod, now: time.Now}
	for _, option := range opts {
		if option != nil {
			option(&settings)
		}
	}
	if settings.watchPeriod < 2*time.Second || settings.watchPeriod%time.Second != 0 {
		return nil, fmt.Errorf("watch period must be a whole number of seconds and at least 2s: %s", settings.watchPeriod)
	}
	if settings.now == nil {
		return nil, errors.New("live inventory clock is required")
	}
	staleAfter := 3 * settings.watchPeriod
	if staleAfter < time.Second {
		return nil, errors.New("stale-after duration must be at least 1s")
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
		namespace: namespace, period: settings.watchPeriod, staleAfter: staleAfter, now: settings.now,
		cycles: make(map[string]time.Time, len(watchedKinds)), listRuns: make(map[string]uint64, len(watchedKinds)),
		cycleRuns: make(map[string]uint64, len(watchedKinds)), syncDone: make(chan struct{}), runDone: make(chan struct{}),
		changes: newChangeHub(),
	}
	config = rest.CopyConfig(config)
	config.Wrap(cycleRecorder{index: index}.wrap)
	inventory, err := ctrlcache.New(config, ctrlcache.Options{
		Scheme:            scheme,
		DefaultNamespaces: map[string]ctrlcache.Config{namespace: {}},
		ByObject: map[client.Object]ctrlcache.ByObject{
			&sandboxapi.Sandbox{}:         {},
			&corev1.Pod{}:                 {},
			&policyapi.PolicyBinding{}:    {},
			&policyapi.PolicySet{}:        {},
			&egressapi.EgressCredential{}: {},
		},
	})
	if err != nil {
		return nil, fmt.Errorf("create live inventory cache: %w", err)
	}
	index.cache = inventory
	if err := index.registerInformer(context.Background(), &sandboxapi.Sandbox{}, false); err != nil {
		return nil, err
	}
	if err := index.registerInformer(context.Background(), &corev1.Pod{}, false); err != nil {
		return nil, err
	}
	if err := index.registerInformer(context.Background(), &policyapi.PolicyBinding{}, true); err != nil {
		return nil, err
	}
	if err := index.registerInformer(context.Background(), &policyapi.PolicySet{}, true); err != nil {
		return nil, err
	}
	if err := index.registerInformer(context.Background(), &egressapi.EgressCredential{}, false); err != nil {
		return nil, err
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
	started := i.now()
	for _, kind := range watchedKinds {
		i.cycles[kind] = started
	}
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
// stale state in Watch rather than hiding the last complete snapshot.
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
	result.Watch = i.health(i.now())
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

func (i *Index) health(now time.Time) WatchHealth {
	i.mu.RLock()
	defer i.mu.RUnlock()
	ages := make(map[string]float64, len(i.cycles))
	fresh := len(i.cycles) == len(watchedKinds)
	for _, kind := range watchedKinds {
		completed, ok := i.cycles[kind]
		if !ok {
			fresh = false
			continue
		}
		age := now.Sub(completed).Seconds()
		ages[kind] = age
		if age < 0 || time.Duration(age*float64(time.Second)) > i.staleAfter {
			fresh = false
		}
	}
	for kind, age := range ages {
		ages[kind] = float64(int(age*10+0.5)) / 10
	}
	return WatchHealth{
		Fresh: fresh, Synced: i.synced, StaleAfterSeconds: i.staleAfter.Seconds(), RefreshedSecondsAgo: ages,
	}
}

// PolicyGeneration returns the monotonic trigger changed by every PolicyBinding or PolicySet event.
func (i *Index) PolicyGeneration() uint64 { return i.policyGeneration.Load() }

// Subscribe adds an independent coalescing change channel. Unsubscribe is safe to call more than once.
func (i *Index) Subscribe() (<-chan struct{}, func()) { return i.changes.subscribe() }

// Names returns resource-kind health keys in stable order.
func (h WatchHealth) Names() []string {
	names := make([]string, 0, len(h.RefreshedSecondsAgo))
	for name := range h.RefreshedSecondsAgo {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}
