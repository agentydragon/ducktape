// Package informer keeps a policy.Index equal to the API server and reports the EgressReady
// condition this proxy keeps on each PolicyBinding: the one write the proxy makes to the API server.
package informer

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	egressclient "github.com/agentydragon/ducktape/agentplane/egress/client/clientset/versioned"
	egressinformers "github.com/agentydragon/ducktape/agentplane/egress/client/informers/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policyclient "github.com/agentydragon/ducktape/agentplane/policy/client/clientset/versioned"
	policytyped "github.com/agentydragon/ducktape/agentplane/policy/client/clientset/versioned/typed/api/v1alpha1"
	policyinformers "github.com/agentydragon/ducktape/agentplane/policy/client/informers/api/v1alpha1"
	policylisters "github.com/agentydragon/ducktape/agentplane/policy/client/listers/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/wait"
	coreinformers "k8s.io/client-go/informers/core/v1"
	"k8s.io/client-go/kubernetes"
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

	bindings       policylisters.PolicyBindingNamespaceLister
	bindingsClient policytyped.PolicyBindingInterface
	// queue holds the names of bindings whose EgressReady may be out of date.
	queue workqueue.TypedRateLimitingInterface[string]
}

// New returns an Informer that is idle until Run. The API server is reached only from Run.
//
// Each kind is a client-go shared informer over a typed client, so the list, watch, resume and
// backoff are client-go's. A list-options tweak bounds watch periods, so a watch the server
// ends on schedule stands as proof of life.
func New(base *rest.Config, config Config) (*Informer, error) {
	if config.WatchPeriod < minWatchPeriod || config.WatchPeriod%time.Second != 0 {
		return nil, fmt.Errorf("the watch period is a whole number of seconds, at least %v: %v", minWatchPeriod, config.WatchPeriod)
	}
	recorded := rest.CopyConfig(base)
	recorded.Wrap(cycleRecorder{index: config.Index, now: time.Now}.wrap)
	policies, err := policyclient.NewForConfig(recorded)
	if err != nil {
		return nil, err
	}
	credentials, err := egressclient.NewForConfig(recorded)
	if err != nil {
		return nil, err
	}
	core, err := kubernetes.NewForConfig(recorded)
	if err != nil {
		return nil, err
	}

	i := &Informer{
		index:          config.Index,
		bindingsClient: policies.AgentplaneV1alpha1().PolicyBindings(config.RulesNamespace),
		queue:          workqueue.NewTypedRateLimitingQueue(workqueue.DefaultTypedControllerRateLimiter[string]()),
	}
	period := int64(config.WatchPeriod / time.Second)
	tweak := func(options *metav1.ListOptions) { options.TimeoutSeconds = &period }
	bindings := policyinformers.NewTypedFilteredPolicyBindingInformer(policies, config.RulesNamespace, 0, nil, tweak)
	// The generated typed lister queries the namespace index with metav1.ObjectMeta.
	if err := bindings.AddIndexers(cache.Indexers{cache.NamespaceIndex: cache.MetaNamespaceIndexFunc}); err != nil {
		return nil, err
	}
	// EgressReady uses the generated cache lister, never an extra API read.
	i.bindings = policylisters.NewPolicyBindingLister(bindings.GetIndexer()).PolicyBindings(config.RulesNamespace)
	if err := errors.Join(
		watchKind(i, bindings, i.putBinding, i.deleteBinding),
		watchKind(i, policyinformers.NewTypedFilteredPolicySetInformer(policies, config.RulesNamespace, 0, nil, tweak), i.putSet, i.deleteSet),
		watchKind(i, egressinformers.NewTypedFilteredEgressCredentialInformer(credentials, config.RulesNamespace, 0, nil, tweak), i.putCredential, i.index.DeleteCredential),
		watchKind(i, coreinformers.NewTypedFilteredSecretInformer(core, config.CredentialsNamespace, 0, nil, tweak), i.putSecret, i.index.DeleteSecret),
	); err != nil {
		return nil, err
	}
	return i, nil
}

// object is what a typed shared informer holds: a pointer to a resource.
type object interface {
	cache.Object
	runtime.Object
}

// watchKind applies a generated shared informer's events to the policy Index. Typed
// handlers preserve tombstone deletion and registration-level synchronization.
func watchKind[T object](i *Informer, informer cache.TypedSharedIndexInformer[T], put func(T), remove func(name string)) error {
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
