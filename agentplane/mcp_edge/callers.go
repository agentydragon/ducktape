package main

import (
	"cmp"
	"context"
	"log/slog"
	"maps"
	"slices"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/policy/reader"
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
	freshness *reader.Freshness
	mintable  sets.Set[auth.Account]
	mu        sync.RWMutex
	accounts  sets.Set[auth.Account]
}

func (i *callerIndex) CallerServiceAccounts() ([]auth.Account, bool) {
	if !i.freshness.Available(time.Now()) {
		return nil, false
	}
	i.mu.RLock()
	defer i.mu.RUnlock()
	return slices.SortedFunc(maps.Keys(i.accounts), func(a, b auth.Account) int {
		return cmp.Or(cmp.Compare(a.Namespace, b.Namespace), cmp.Compare(a.Name, b.Name))
	}), true
}

func (i *callerIndex) Admits(account auth.Account) bool {
	if !i.freshness.Available(time.Now()) {
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

func callerWatches(base *rest.Config, namespaces []string, mintable []auth.Account, period time.Duration) (*callerIndex, *reader.Watches, error) {
	namespaces = sets.List(sets.New(namespaces...))
	kinds := make([]reader.Kind, len(namespaces))
	for n, namespace := range namespaces {
		kinds[n] = reader.Kind(namespace + "/serviceaccounts")
	}
	index := &callerIndex{freshness: reader.NewFreshness(reader.StaleAfterCycles*period, kinds...), mintable: sets.New(mintable...), accounts: sets.New[auth.Account]()}
	watches, err := reader.NewWatches(base, index.freshness, period)
	if err != nil {
		return nil, nil, err
	}
	for n, namespace := range namespaces {
		core, err := kubernetes.NewForConfig(watches.ConfigRecordingAs(kinds[n]))
		if err != nil {
			return nil, nil, err
		}
		selected := func(options *metav1.ListOptions) {
			watches.TweakListOptions(options)
			options.LabelSelector = callerLabel + "=true"
		}
		if err := reader.Watch(
			watches,
			coreinformers.NewTypedFilteredServiceAccountInformer(core, namespace, 0, nil, selected),
			index.put, func(name string) { index.remove(namespace, name) },
		); err != nil {
			return nil, nil, err
		}
	}
	return index, watches, nil
}

func (i *callerIndex) ready() bool {
	status := i.freshness.Status(time.Now())
	if !status.Available {
		slog.Warn("MCP edge caller view unavailable", "synced", status.Synced, "ages", status.Ages)
	}
	return status.Available
}

func runCallers(ctx context.Context, watches *reader.Watches) <-chan struct{} {
	done := make(chan struct{})
	go func() { defer close(done); watches.Run(ctx) }()
	return done
}
