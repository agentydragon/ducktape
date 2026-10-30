// Package informer keeps the Action Service's policy Index equal to the API server: the `action_auto_approve` rules of
// the policy namespace's PolicySets, the PolicyBindings that grant them, and which ServiceAccounts of the caller
// namespaces carry the caller label. Its one write is the ActionsReady condition on each PolicyBinding of the policy
// namespace.
package informer

import (
	"cmp"
	"context"
	"errors"
	"log/slog"
	"maps"
	"slices"
	"sync"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"
	coreinformers "k8s.io/client-go/informers/core/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/agentydragon/ducktape/agentplane/action_service/policies"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
)

// CallerLabel, set to "true" on a ServiceAccount of a caller namespace, admits its workloads as callers. It grants no
// Action. Must equal CALLER_LABEL in agentplane/action_service/policies/resources.py, which labels the cluster's
// callers, and ActionCallerLabel in agentplane/sandbox_controller.
const CallerLabel = "agentplane.allegedly.works/use-action-service"

// ActionsReady is the condition this service keeps on each PolicyBinding of the policy namespace.
var ActionsReady = reader.Condition{
	Type: "ActionsReady", FieldManager: "agentplane-action-service", Rules: "Action rules", Reason: "Applied",
}

// Index is what the Action Service reads of the API server to admit a caller and decide its requests. Its writer is
// the Informer; readers take the state of one moment.
type Index struct {
	Rules *reader.Index[policies.Policy]
	// Health covers the PolicySets, the PolicyBindings and each caller namespace's ServiceAccounts.
	Health *watchhealth.Health
	// namespaces are the caller namespaces, sorted.
	namespaces []string

	mu sync.RWMutex
	// callers are the ServiceAccounts of the caller namespaces that carry the caller label.
	callers sets.Set[auth.Account]
}

// NewIndex returns an empty Index of the callers in callerNamespaces. It is unavailable until an Informer has listed
// every kind, and while any kind fails to list or watch.
func NewIndex(callerNamespaces []string) *Index {
	return &Index{
		Rules:      reader.NewIndex(policies.RuleType, policies.ParseRule),
		Health:     watchhealth.New(),
		namespaces: sets.List(sets.New(callerNamespaces...)),
		callers:    sets.New[auth.Account](),
	}
}

// serviceAccounts is the kind a caller namespace's ServiceAccounts are watched as: each namespace is a watch of its own,
// which fails on its own.
func serviceAccounts(namespace string) watchhealth.Kind {
	return watchhealth.Kind(namespace + "/serviceaccounts")
}

// Admits reports whether the account may call the service: it carries the caller label now. Nothing is admitted while
// the Index is unavailable, so a replica that cannot reach the API server refuses every caller rather than serve a
// picture it cannot vouch for. Every refusal is logged.
func (i *Index) Admits(account auth.Account) bool {
	if !i.Health.Available() {
		slog.Warn("workload bearer refused: the policy index is unavailable",
			"namespace", account.Namespace, "name", account.Name)
		return false
	}
	i.mu.RLock()
	labeled := i.callers.Has(account)
	i.mu.RUnlock()
	if !labeled {
		slog.Warn("workload bearer refused: the ServiceAccount does not carry the caller label",
			"namespace", account.Namespace, "name", account.Name, "label", CallerLabel)
	}
	return labeled
}

// CallerServiceAccounts are the ServiceAccounts that carry the caller label now, in namespace and name order, and
// whether the Index was available to read them: none when it was not.
func (i *Index) CallerServiceAccounts() ([]auth.Account, bool) {
	if !i.Health.Available() {
		return nil, false
	}
	i.mu.RLock()
	defer i.mu.RUnlock()
	return slices.SortedFunc(maps.Keys(i.callers), func(a, b auth.Account) int {
		return cmp.Or(cmp.Compare(a.Namespace, b.Namespace), cmp.Compare(a.Name, b.Name))
	}), true
}

// Resolve is the subject's PolicyBindings in name order, each with how the sets it names stand, and whether the Index
// was available to read them: nothing when it was not. The Policy Controller deletes a binding at expiry. Admission
// and the policy views read this one resolution, so what a view reports and what admission decides cannot drift.
func (i *Index) Resolve(subject policyapi.ServiceAccountRef) ([]reader.BindingResolution[policies.Policy], bool) {
	if !i.Health.Available() {
		return nil, false
	}
	return i.Rules.Bindings(subject), true
}

// ResolveBindings is the caller's PolicyBindings as admission decides from them, in name order, each with the sets it
// names that exist and parse; a missing or refused set contributes nothing. Nothing while the Index is unavailable, so
// every request then waits for an operator.
func (i *Index) ResolveBindings(caller policyapi.ServiceAccountRef) []policies.Binding {
	resolved, _ := i.Resolve(caller)
	var bindings []policies.Binding
	for _, bound := range resolved {
		binding := policies.Binding{
			Namespace: bound.Binding.Namespace, Name: bound.Binding.Name, ResourceVersion: bound.Binding.ResourceVersion,
		}
		for _, set := range bound.Sets {
			binding.PolicySets = append(binding.PolicySets, policies.PolicySet{
				Namespace: bound.Binding.Namespace, Name: set.Name, Generation: set.Generation, Policies: set.Rules,
			})
		}
		bindings = append(bindings, binding)
	}
	return bindings
}

func (i *Index) putCaller(account *corev1.ServiceAccount) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.callers.Insert(auth.Account{Namespace: account.Namespace, Name: account.Name})
}

func (i *Index) deleteCaller(account auth.Account) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.callers.Delete(account)
}

// Informer fills an Index from the API server and keeps ActionsReady on the policy namespace's bindings.
type Informer struct{ watches *reader.Watches }

// New returns an Informer of the Index that is idle until Run. policyNamespace holds the PolicySets and PolicyBindings.
func New(base *rest.Config, index *Index, policyNamespace string) (*Informer, error) {
	watches := reader.NewWatches(base, index.Health)
	watched := []error{reader.WatchRules(watches, index.Rules, policyNamespace, ActionsReady)}
	for _, namespace := range index.namespaces {
		core, err := kubernetes.NewForConfig(watches.ConfigRecordingAs(serviceAccounts(namespace)))
		if err != nil {
			return nil, err
		}
		// The server sends a ServiceAccount that loses the label as deleted.
		labeled := func(options *metav1.ListOptions) { options.LabelSelector = CallerLabel + "=true" }
		watched = append(watched, reader.Watch(
			watches,
			serviceAccounts(namespace),
			coreinformers.NewTypedFilteredServiceAccountInformer(core, namespace, 0, nil, labeled),
			index.putCaller,
			func(name string) { index.deleteCaller(auth.Account{Namespace: namespace, Name: name}) },
		))
	}
	if err := errors.Join(watched...); err != nil {
		return nil, err
	}
	return &Informer{watches: watches}, nil
}

// Run lists and watches until ctx is done; see reader.Watches.Run.
func (i *Informer) Run(ctx context.Context) { i.watches.Run(ctx) }
