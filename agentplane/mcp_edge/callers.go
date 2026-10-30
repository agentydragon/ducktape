package main

import (
	"cmp"
	"context"
	"log/slog"
	"maps"
	"slices"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"
	coreinformers "k8s.io/client-go/informers/core/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

// callerIndex reads only caller labels. The Action Service owns policy and ActionsReady; this edge never writes them.
// Keep this label equal to action_service/informer.CallerLabel; both services independently check it.
const callerLabel = "agentplane.allegedly.works/use-action-service"

type callerIndex struct {
	health   *watchhealth.Health
	mintable sets.Set[auth.Account]
	mu       sync.RWMutex
	accounts sets.Set[auth.Account]
}

func (i *callerIndex) CallerServiceAccounts() ([]auth.Account, bool) {
	if !i.health.Available() {
		return nil, false
	}
	i.mu.RLock()
	defer i.mu.RUnlock()
	return slices.SortedFunc(maps.Keys(i.accounts), func(a, b auth.Account) int {
		return cmp.Or(cmp.Compare(a.Namespace, b.Namespace), cmp.Compare(a.Name, b.Name))
	}), true
}

func (i *callerIndex) Admits(account auth.Account) bool {
	if !i.health.Available() {
		return false
	}
	i.mu.RLock()
	defer i.mu.RUnlock()
	return i.accounts.Has(account)
}

func (i *callerIndex) put(account *corev1.ServiceAccount) {
	i.mu.Lock()
	defer i.mu.Unlock()
	caller := auth.Account{Namespace: account.Namespace, Name: account.Name}
	if i.mintable.Has(caller) {
		i.accounts.Insert(caller)
	}
}

func (i *callerIndex) remove(namespace, name string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.accounts.Delete(auth.Account{Namespace: namespace, Name: name})
}

func callerWatches(base *rest.Config, namespaces []string, mintable []auth.Account) (*callerIndex, *reader.Watches, error) {
	index := &callerIndex{health: watchhealth.New(), mintable: sets.New(mintable...), accounts: sets.New[auth.Account]()}
	watches := reader.NewWatches(base, index.health)
	for _, namespace := range sets.List(sets.New(namespaces...)) {
		kind := watchhealth.Kind(namespace + "/serviceaccounts")
		core, err := kubernetes.NewForConfig(watches.ConfigRecordingAs(kind))
		if err != nil {
			return nil, nil, err
		}
		selected := func(options *metav1.ListOptions) { options.LabelSelector = callerLabel + "=true" }
		if err := reader.Watch(
			watches,
			kind,
			coreinformers.NewTypedFilteredServiceAccountInformer(core, namespace, 0, nil, selected),
			index.put, func(name string) { index.remove(namespace, name) },
		); err != nil {
			return nil, nil, err
		}
	}
	return index, watches, nil
}

func (i *callerIndex) ready() bool {
	status := i.health.Status()
	if !status.Available {
		slog.Warn("MCP edge caller view unavailable", "synced", status.Synced, "failing", status.Failing)
	}
	return status.Available
}

func runCallers(ctx context.Context, watches *reader.Watches) <-chan struct{} {
	done := make(chan struct{})
	go func() { defer close(done); watches.Run(ctx) }()
	return done
}
