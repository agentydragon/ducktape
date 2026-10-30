package reader

import (
	"errors"
	"log/slog"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policyclient "github.com/agentydragon/ducktape/agentplane/policy/client/clientset/versioned"
	policytyped "github.com/agentydragon/ducktape/agentplane/policy/client/clientset/versioned/typed/api/v1alpha1"
	policyinformers "github.com/agentydragon/ducktape/agentplane/policy/client/informers/api/v1alpha1"
	policylisters "github.com/agentydragon/ducktape/agentplane/policy/client/listers/api/v1alpha1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/client-go/tools/cache"
	"k8s.io/client-go/util/workqueue"
)

// rules keeps an Index equal to one namespace's PolicySets and PolicyBindings, and the reader's
// condition on each binding.
type rules[R any] struct {
	index     *Index[R]
	condition Condition

	bindings       policylisters.PolicyBindingNamespaceLister
	bindingsClient policytyped.PolicyBindingInterface
	// queue holds the names of bindings whose condition may be out of date.
	queue workqueue.TypedRateLimitingInterface[string]
}

// WatchRules keeps index equal to the PolicySets and PolicyBindings in namespace, and keeps the
// condition on each binding once both have listed: before then a set not yet seen would read as
// missing. The Health of w covers PolicySets and PolicyBindings.
func WatchRules[R any](w *Watches, index *Index[R], namespace string, condition Condition) error {
	_, err := watchRules(w, index, namespace, condition)
	return err
}

func watchRules[R any](w *Watches, index *Index[R], namespace string, condition Condition) (*rules[R], error) {
	policies, err := policyclient.NewForConfig(w.Config())
	if err != nil {
		return nil, err
	}
	r := &rules[R]{
		index:          index,
		condition:      condition,
		bindingsClient: policies.AgentplaneV1alpha1().PolicyBindings(namespace),
		queue:          workqueue.NewTypedRateLimitingQueue(workqueue.DefaultTypedControllerRateLimiter[string]()),
	}
	bindings := policyinformers.NewTypedFilteredPolicyBindingInformer(policies, namespace, 0, nil, nil)
	// The generated typed lister queries the namespace index with metav1.ObjectMeta.
	if err := bindings.AddIndexers(cache.Indexers{cache.NamespaceIndex: cache.MetaNamespaceIndexFunc}); err != nil {
		return nil, err
	}
	// The condition uses the generated cache lister, never an extra API read.
	r.bindings = policylisters.NewPolicyBindingLister(bindings.GetIndexer()).PolicyBindings(namespace)
	if err := errors.Join(
		Watch(w, PolicyBindings, bindings, r.putBinding, index.DeleteBinding),
		Watch(w, PolicySets, policyinformers.NewTypedFilteredPolicySetInformer(policies, namespace, 0, nil, nil), r.putSet, r.deleteSet),
	); err != nil {
		return nil, err
	}
	w.afterSync = append(w.afterSync, r.reportReadiness)
	w.shutdown = append(w.shutdown, r.queue.ShutDown)
	return r, nil
}

func (r *rules[R]) putBinding(binding *policyapi.PolicyBinding) {
	r.index.PutBinding(binding)
	r.queue.Add(binding.Name)
}

// putSet and deleteSet re-judge every binding: any of them may name the set.
func (r *rules[R]) putSet(set *policyapi.PolicySet) {
	r.index.PutPolicySet(set)
	r.enqueueBindings()
}

func (r *rules[R]) deleteSet(name string) {
	r.index.DeletePolicySet(name)
	r.enqueueBindings()
}

func (r *rules[R]) enqueueBindings() {
	bindings, err := r.bindings.List(labels.Everything())
	if err != nil {
		slog.Error("listing bindings to re-judge failed", "error", err)
	}
	for _, binding := range bindings {
		r.queue.Add(binding.Name)
	}
}
