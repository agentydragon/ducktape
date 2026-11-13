package main

import (
	"cmp"
	"context"
	"log/slog"
	"maps"
	"slices"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policyclient "github.com/agentydragon/ducktape/agentplane/policy/client/clientset/versioned"
	policyinformers "github.com/agentydragon/ducktape/agentplane/policy/client/informers/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/rest"
)

// identityIndex supplies a fresh picker view and a direct API-server UID check. The informer is only for listing;
// consent, grant resolution, and rebind compare the exact incarnation through GetIdentityUID.
type identityIndex struct {
	health     *watchhealth.Health
	namespace  string
	objects    policyclient.Interface
	mu         sync.RWMutex
	identities map[string]auth.Identity
}

func (i *identityIndex) ConnectionIdentities() ([]auth.Identity, bool) {
	if !i.health.Available() {
		return nil, false
	}
	i.mu.RLock()
	defer i.mu.RUnlock()
	return slices.SortedFunc(maps.Values(i.identities), func(a, b auth.Identity) int {
		return cmp.Compare(a.Name, b.Name)
	}), true
}

// GetIdentityUID reads the policy namespace directly so stale informer state cannot authorize a deleted or replaced
// Identity incarnation.
func (i *identityIndex) GetIdentityUID(ctx context.Context, name string) (string, bool, error) {
	identity, err := i.objects.AgentplaneV1alpha1().Identities(i.namespace).Get(ctx, name, metav1.GetOptions{})
	switch {
	case apierrors.IsNotFound(err):
		return "", false, nil
	case err != nil:
		return "", false, err
	case identity.UID == "", !identity.DeletionTimestamp.IsZero():
		return "", false, nil
	default:
		return string(identity.UID), true, nil
	}
}

func (i *identityIndex) put(identity *policyapi.Identity) {
	if identity.UID == "" || !identity.DeletionTimestamp.IsZero() {
		i.remove(identity.Name)
		return
	}
	i.mu.Lock()
	defer i.mu.Unlock()
	i.identities[identity.Name] = auth.Identity{Name: identity.Name, UID: string(identity.UID)}
}

func (i *identityIndex) remove(name string) {
	i.mu.Lock()
	defer i.mu.Unlock()
	delete(i.identities, name)
}

func identityWatches(base *rest.Config, namespace string) (*identityIndex, *reader.Watches, error) {
	health := watchhealth.New()
	index := &identityIndex{health: health, namespace: namespace, identities: map[string]auth.Identity{}}
	objects, err := policyclient.NewForConfig(base)
	if err != nil {
		return nil, nil, err
	}
	index.objects = objects
	watches := reader.NewWatches(base, health)
	kind := watchhealth.Kind(namespace + "/identities")
	watcher, err := policyclient.NewForConfig(watches.ConfigRecordingAs(kind))
	if err != nil {
		return nil, nil, err
	}
	if err := reader.Watch(watches, kind,
		policyinformers.NewTypedFilteredIdentityInformer(watcher, namespace, 0, nil, nil), index.put, index.remove); err != nil {
		return nil, nil, err
	}
	return index, watches, nil
}

func (i *identityIndex) ready() bool {
	status := i.health.Status()
	if !status.Available {
		slog.Warn("MCP edge Identity view unavailable", "synced", status.Synced, "failing", status.Failing)
	}
	return status.Available
}

func runIdentities(ctx context.Context, watches *reader.Watches) <-chan struct{} {
	done := make(chan struct{})
	go func() { defer close(done); watches.Run(ctx) }()
	return done
}
