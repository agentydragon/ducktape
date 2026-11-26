package main

import (
	"cmp"
	"context"
	"log/slog"
	"maps"
	"slices"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/kubeclient"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/internal/connections"
	"github.com/agentydragon/ducktape/agentplane/observability"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	policyclient "github.com/agentydragon/ducktape/agentplane/policy/client/clientset/versioned"
	policyinformers "github.com/agentydragon/ducktape/agentplane/policy/client/informers/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"google.golang.org/grpc"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/rest"
)

// identitySource is what the edge needs of an Identity view: the picker list, exact UID checks, and readiness.
type identitySource interface {
	connections.IdentityCatalog
	auth.IdentityUIDReader
	ready() bool
}

// identityGrace keeps the PolicyService view answering this long after its watch breaks, covering a Policy Controller
// replica restart. Past it, the picker, consent, grant resolution and rebind refuse until a fresh snapshot, so a deleted
// Identity authorizes nothing for longer than this while the watch is down.
const identityGrace = 30 * time.Second

// startIdentities follows Identities through PolicyService when configured, else through the informer; stop ends it.
func startIdentities(ctx context.Context, c config) (identitySource, func(), error) {
	if c.PolicyService == nil {
		kubeConfig, err := kubeclient.Config("")
		if err != nil {
			return nil, nil, err
		}
		kubeConfig.QPS = -1
		index, watches, err := identityWatches(kubeConfig, c.PolicyNamespace)
		if err != nil {
			return nil, nil, err
		}
		watchContext, stopWatching := context.WithCancel(ctx)
		watching := runIdentities(watchContext, watches)
		return index, func() { stopWatching(); <-watching }, nil
	}
	credentials, err := c.PolicyService.TLS.Credentials()
	if err != nil {
		return nil, nil, err
	}
	connection, err := grpc.NewClient(c.PolicyService.Target, observability.GRPCClientOption(), grpc.WithTransportCredentials(credentials))
	if err != nil {
		return nil, nil, err
	}
	cache, err := identitycache.New(policypb.NewPolicyServiceClient(connection), identityGrace)
	if err != nil {
		_ = connection.Close()
		return nil, nil, err
	}
	runContext, stopRunning := context.WithCancel(ctx)
	running := make(chan struct{})
	go func() { defer close(running); _ = cache.Run(runContext) }()
	return cachedIdentities{cache}, func() { stopRunning(); <-running; _ = connection.Close() }, nil
}

// cachedIdentities is the PolicyService-backed view; a stale view lists nothing and is not ready.
type cachedIdentities struct{ *identitycache.Cache }

func (i cachedIdentities) ConnectionIdentities() ([]auth.Identity, bool) {
	listed, err := i.List()
	return listed, err == nil
}

func (i cachedIdentities) ready() bool {
	_, err := i.List()
	if err != nil {
		slog.Warn("MCP edge Identity view unavailable", "error", err)
	}
	return err == nil
}

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
