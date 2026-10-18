// Package informer keeps a policy.Index equal to the API server and reports the EgressReady
// condition this proxy keeps on each PolicyBinding: the one write the proxy makes to the API server.
package informer

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/gentype"
	corev1client "k8s.io/client-go/kubernetes/typed/core/v1"
	"k8s.io/client-go/listers"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
	"k8s.io/client-go/util/workqueue"
)

// StaleAfterCycles is how many watch periods a kind may miss before the index calls itself stale:
// one late cycle is a slow API server, three in a row is a wedge. The proxy builds its Index with
// StaleAfterCycles of the WatchPeriod it configures here.
const StaleAfterCycles = 3

// minWatchPeriod is the shortest period client-go does not take for a failing watch: it relists after
// one that closes in under a second without an event.
const minWatchPeriod = 2 * time.Second

// Config says what to watch and for how long.
type Config struct {
	Index *policy.Index
	// RulesNamespace holds the PolicySets, PolicyBindings and EgressCredentials this proxy enforces.
	RulesNamespace string
	// CredentialsNamespace holds the Secrets those credentials read.
	CredentialsNamespace string
	// WatchPeriod is how long the server holds each watch open, in whole seconds of at least two. A kind has completed
	// a cycle when a watch it held for the whole period ends cleanly, so the index's staleness bound
	// is StaleAfterCycles of these.
	WatchPeriod time.Duration
}

// Informer fills the Index from the API server and keeps EgressReady on the bindings.
type Informer struct {
	index     *policy.Index
	informers []cache.SharedIndexInformer
	synced    []cache.InformerSynced

	bindings       listers.ResourceIndexer[*policyapi.PolicyBinding]
	bindingsClient *gentype.ClientWithList[*policyapi.PolicyBinding, *policyapi.PolicyBindingList]
	// queue holds the names of bindings whose EgressReady may be out of date.
	queue workqueue.TypedRateLimitingInterface[string]
}

// New returns an Informer that is idle until Run. The API server is reached only from Run.
//
// Each kind is a client-go shared informer over a typed client, so the list, watch, resume and
// backoff are client-go's. Deviation from what informer-gen emits: every watch is bounded to the
// watch period, which is what lets a watch the server ends on schedule stand as proof of life.
func New(base *rest.Config, config Config) (*Informer, error) {
	if config.WatchPeriod < minWatchPeriod || config.WatchPeriod%time.Second != 0 {
		return nil, fmt.Errorf("the watch period is a whole number of seconds, at least %v: %v", minWatchPeriod, config.WatchPeriod)
	}
	scheme := runtime.NewScheme()
	if err := errors.Join(policyapi.AddToScheme(scheme), egressapi.AddToScheme(scheme)); err != nil {
		return nil, err
	}

	recorded := rest.CopyConfig(base)
	recorded.Wrap(cycleRecorder{index: config.Index, now: time.Now}.wrap)
	custom := rest.CopyConfig(recorded)
	custom.GroupVersion, custom.APIPath = &policyapi.GroupVersion, "/apis"
	custom.NegotiatedSerializer = rest.CodecFactoryForGeneratedClient(scheme, serializer.NewCodecFactory(scheme)).WithoutConversion()
	customClient, err := rest.RESTClientFor(custom)
	if err != nil {
		return nil, err
	}
	core, err := corev1client.NewForConfig(recorded)
	if err != nil {
		return nil, err
	}

	parameters := runtime.NewParameterCodec(scheme)
	sets := gentype.NewClientWithList[*policyapi.PolicySet, *policyapi.PolicySetList](
		string(policy.PolicySets), customClient, parameters, config.RulesNamespace,
		func() *policyapi.PolicySet { return &policyapi.PolicySet{} },
		func() *policyapi.PolicySetList { return &policyapi.PolicySetList{} },
	)
	bindings := gentype.NewClientWithList[*policyapi.PolicyBinding, *policyapi.PolicyBindingList](
		string(policy.Bindings), customClient, parameters, config.RulesNamespace,
		func() *policyapi.PolicyBinding { return &policyapi.PolicyBinding{} },
		func() *policyapi.PolicyBindingList { return &policyapi.PolicyBindingList{} },
	)
	credentials := gentype.NewClientWithList[*egressapi.EgressCredential, *egressapi.EgressCredentialList](
		string(policy.Credentials), customClient, parameters, config.RulesNamespace,
		func() *egressapi.EgressCredential { return &egressapi.EgressCredential{} },
		func() *egressapi.EgressCredentialList { return &egressapi.EgressCredentialList{} },
	)

	i := &Informer{
		index:          config.Index,
		bindingsClient: bindings,
		queue:          workqueue.NewTypedRateLimitingQueue(workqueue.DefaultTypedControllerRateLimiter[string]()),
	}
	period := int64(config.WatchPeriod / time.Second)
	if err := watchKind(i, bindings, &policyapi.PolicyBinding{}, period, i.putBinding, i.deleteBinding); err != nil {
		return nil, err
	}
	// EgressReady is judged on the bindings the informer holds, not on a read from the server.
	i.bindings = listers.NewNamespaced(
		listers.New[*policyapi.PolicyBinding](i.informers[0].GetIndexer(), policyapi.GroupVersion.WithResource(string(policy.Bindings)).GroupResource()),
		config.RulesNamespace,
	)
	if err := errors.Join(
		watchKind(i, sets, &policyapi.PolicySet{}, period, i.putSet, i.deleteSet),
		watchKind(i, credentials, &egressapi.EgressCredential{}, period, i.putCredential, i.index.DeleteCredential),
		watchKind(i, core.Secrets(config.CredentialsNamespace), &corev1.Secret{}, period, i.putSecret, i.index.DeleteSecret),
	); err != nil {
		return nil, err
	}
	return i, nil
}

// listerWatcher is the list and watch every typed client has.
type listerWatcher[L runtime.Object] interface {
	List(context.Context, metav1.ListOptions) (L, error)
	Watch(context.Context, metav1.ListOptions) (watch.Interface, error)
}

// object is what a typed shared informer holds: a pointer to a resource.
type object interface {
	cache.Object
	runtime.Object
}

// watchKind adds a shared informer over client to i, with put and remove applying what it sees to
// the Index. The informer lists once, then watches in bounded periods and resumes each from where
// the last stopped; it relists only when the server can no longer resume it, and the relist reports
// the objects that vanished meanwhile as deleted.
func watchKind[T object, L runtime.Object](
	i *Informer, client listerWatcher[L], example T, period int64, put func(T), remove func(name string),
) error {
	informer := cache.NewTypedSharedIndexInformer[T](cache.NewSharedIndexInformerWithOptions(&cache.ListWatch{
		ListWithContextFunc: func(ctx context.Context, options metav1.ListOptions) (runtime.Object, error) {
			return client.List(ctx, options)
		},
		WatchFuncWithContext: func(ctx context.Context, options metav1.ListOptions) (watch.Interface, error) {
			options.TimeoutSeconds = &period
			return client.Watch(ctx, options)
		},
	}, example, cache.SharedIndexInformerOptions{
		Indexers: cache.Indexers{cache.NamespaceIndex: cache.MetaNamespaceIndexFunc},
	}))
	registration, err := informer.AddTypedEventHandler(cache.TypedResourceEventHandlerFuncs[T]{
		AddFunc:    put,
		UpdateFunc: func(_, updated T) { put(updated) },
		DeleteFunc: func(deleted cache.DeletedObject[T]) { remove(deleted.GetName()) },
	})
	if err != nil {
		return err
	}
	i.informers = append(i.informers, informer)
	i.synced = append(i.synced, registration.HasSynced)
	return nil
}

func (i *Informer) putBinding(binding *policyapi.PolicyBinding) {
	i.index.PutBinding(binding)
	i.queue.Add(binding.Name)
}

func (i *Informer) deleteBinding(name string) { i.index.DeleteBinding(name) }

// putSet and deleteSet re-judge every binding: any of them may name the set.
func (i *Informer) putSet(set *policyapi.PolicySet) {
	i.index.PutPolicySet(set)
	i.enqueueBindings()
}

func (i *Informer) deleteSet(name string) {
	i.index.DeletePolicySet(name)
	i.enqueueBindings()
}

func (i *Informer) enqueueBindings() {
	bindings, err := i.bindings.List(labels.Everything())
	if err != nil {
		slog.Error("listing bindings to re-judge failed", "error", err)
	}
	for _, binding := range bindings {
		i.queue.Add(binding.Name)
	}
}

// putCredential removes a credential this proxy cannot read, rather than keep the last one it could:
// a credential read wrongly is a placeholder forwarded raw or a secret sent where it should not go.
func (i *Informer) putCredential(credential *egressapi.EgressCredential) {
	parsed, err := resources.CredentialFromAPI(credential)
	if err != nil {
		slog.Warn("EgressCredential refused", "credential", credential.Name, "error", err)
		i.index.DeleteCredential(credential.Name)
		return
	}
	i.index.PutCredential(parsed)
}

func (i *Informer) putSecret(secret *corev1.Secret) {
	i.index.PutSecret(resources.SecretFromV1(secret))
}

// Run lists and watches until ctx is done. The index is marked synced once every kind has listed and
// its objects are in the index; until then no EgressReady is written, since a set not yet seen would
// read as missing.
func (i *Informer) Run(ctx context.Context) {
	started := time.Now()
	for _, kind := range []policy.Kind{policy.PolicySets, policy.Bindings, policy.Credentials, policy.Secrets} {
		i.index.RecordCycle(kind, started)
	}
	var group wait.Group
	defer group.Wait()
	defer i.queue.ShutDown()
	for _, informer := range i.informers {
		group.StartWithContext(ctx, informer.RunWithContext)
	}
	if !cache.WaitForCacheSync(ctx.Done(), i.synced...) {
		return
	}
	i.index.MarkSynced()
	group.StartWithContext(ctx, i.reportReadiness)
	<-ctx.Done()
}
