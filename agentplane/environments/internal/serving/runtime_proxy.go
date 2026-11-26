package serving

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/environments/runtimeproxy"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"golang.org/x/sync/errgroup"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/rest"
	toolscache "k8s.io/client-go/tools/cache"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// runtimeProxy owns a replica-local inventory and callable Runtime connections. It never
// uses the manager's leader-scoped cache or the Controller's observe-only binding watch.
type runtimeProxy struct {
	server *runtimeproxy.Server
	cache  cache.Cache
	health *watchhealth.Health
}

func newRuntimeProxy(c RuntimeProxyConfig, namespace string, apiConfig *rest.Config, scheme *runtime.Scheme, identities reader.IdentitySource) (*runtimeProxy, error) {
	if err := c.ValidateCredentials(); err != nil {
		return nil, err
	}
	health := watchhealth.New()
	kindByType := make(map[string]watchhealth.Kind)
	for kind, object := range map[watchhealth.Kind]client.Object{
		"sandboxes": &sandboxapi.Sandbox{}, "pods": &corev1.Pod{},
		"virtualmachineinstances": &kubevirtv1.VirtualMachineInstance{}, "virtualmachines": &kubevirtv1.VirtualMachine{},
	} {
		kindByType[fmt.Sprintf("%T", object)] = kind
	}
	// KubeVirt informers remain lazy: clusters without its API can serve Pod Sandboxes.
	inventory, err := cache.New(health.Config(apiConfig, watchhealth.KindByPath), cache.Options{
		Scheme: scheme, DefaultNamespaces: map[string]cache.Config{namespace: {}},
		DefaultWatchErrorHandler: func(ctx context.Context, r *toolscache.Reflector, err error) {
			health.WatchErrorHandler(kindByType[r.TypeDescription()])(ctx, r, err)
		},
	})
	if err != nil {
		return nil, err
	}
	// Kubernetes-backed Identities are read through the inventory, as Sandboxes are.
	sandboxIdentities, err := identityUIDs(identities, inventory, namespace)
	if err != nil {
		return nil, err
	}
	// No bearer accounts or workload SVID resolver are installed on the proxy's methods.
	// Only the four exact service allowlists confer authority; delegation stays absent.
	server, err := runtimeproxy.New(runtimeproxy.Config{
		Namespace: namespace, RunnerSecurity: c.RunnerSecurity,
		RuntimeReaders: c.Readers, SessionServices: c.SessionCallers,
		ControllerServices: c.ControllerCallers, ExecutorServices: c.ExecutorCallers,
	}, inventory, health, sandboxIdentities)
	if err != nil {
		return nil, err
	}
	for _, watched := range []client.Object{&sandboxapi.Sandbox{}, &corev1.Pod{}} {
		informer, err := inventory.GetInformer(context.Background(), watched)
		if err != nil {
			return nil, err
		}
		if _, err := informer.AddEventHandler(server.RuntimeDiscoveryHandler()); err != nil {
			return nil, err
		}
	}
	return &runtimeProxy{server: server, cache: inventory, health: health}, nil
}

func (p *runtimeProxy) run(ctx context.Context) error {
	workers, ctx := errgroup.WithContext(ctx)
	workers.Go(func() error { return p.cache.Start(ctx) })
	workers.Go(func() error {
		if !p.cache.WaitForCacheSync(ctx) {
			return fmt.Errorf("Runtime proxy inventory did not synchronize: %w", ctx.Err())
		}
		p.health.MarkSynced()
		return p.server.RunRuntimeObservers(ctx)
	})
	defer p.health.MarkDraining()
	return workers.Wait()
}
