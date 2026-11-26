package app

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"net"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/access"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/kubeclient"
	netlistener "github.com/agentydragon/ducktape/agentplane/net/listener"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/observability"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/wake"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/sandboxinventory"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/store"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/migrations"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/keepalive"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/sets"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	toolscache "k8s.io/client-go/tools/cache"

	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func kubernetesConfig(c config) (*rest.Config, error) {
	kubeConfig, err := kubeclient.Config(c.Kubeconfig)
	if err != nil {
		return nil, err
	}
	// Zero keeps client-go's 5 QPS / burst 10 limiter; the deployment sets -1 to disable it.
	kubeConfig.QPS = c.KubernetesClientQPS
	return kubeConfig, nil
}

func reportUnexpected(ctx context.Context, failed chan<- error, role string, err error) {
	if ctx.Err() != nil {
		return
	}
	if err == nil {
		err = fmt.Errorf("%s stopped unexpectedly", role)
	}
	failed <- fmt.Errorf("%s: %w", role, err)
}

func run(ctx context.Context, c config) error {
	if err := migrate.Run(ctx, c.DispatcherDatabaseURL, migrations.Up); err != nil {
		return err
	}
	return listenAndServe(ctx, c)
}

func listenAndServe(ctx context.Context, c config) error {
	rpcListener, err := netlistener.Open(c.RPC.ListenAddress)
	if err != nil {
		return fmt.Errorf("gRPC listener: %w", err)
	}
	defer rpcListener.Close()
	return serve(ctx, c, rpcListener)
}

// identitySources are the Identity readers behind every Dispatcher verifier.
type identitySources struct {
	callers, sandboxes, delegation auth.IdentityUIDReader
	rules                          reader.IdentitySource
	stop                           func()
}

// startIdentities serves every Identity read from one PolicyService view when policy_service is set, else from
// Kubernetes.
func startIdentities(ctx context.Context, c config, kubeConfig *rest.Config, objects client.Client) (identitySources, error) {
	// CLEANUP(added 2026-11-26): drop the Kubernetes sources once the staging manifests set policy_service
	//   (IDENTITY_READERS_BEHIND_POLICY_SERVICE).
	if c.PolicyService != nil {
		view, stop, err := identitycache.Start(ctx, *c.PolicyService, identitycache.StandardGrace)
		if err != nil {
			return identitySources{}, fmt.Errorf("PolicyService Identity view: %w", err)
		}
		return identitySources{
			callers: view, sandboxes: view, delegation: view, rules: reader.CachedIdentities{View: view}, stop: stop,
		}, nil
	}
	callers, err := auth.NewKubernetesIdentityUIDs(kubeConfig, c.PolicyNamespace)
	if err != nil {
		return identitySources{}, err
	}
	sandboxes, err := auth.KubernetesIdentityUIDs(objects, c.Namespace)
	if err != nil {
		return identitySources{}, err
	}
	delegation, err := auth.KubernetesIdentityUIDs(objects, c.PolicyNamespace)
	if err != nil {
		return identitySources{}, err
	}
	return identitySources{
		callers: callers, sandboxes: sandboxes, delegation: delegation, rules: reader.KubernetesIdentities{}, stop: func() {},
	}, nil
}

// serve uses the supplied shared gRPC listener.
func serve(ctx context.Context, c config, rpcListener net.Listener) error {
	rpcCredentials, err := c.RPC.ServerCredentialsWithServices(peertls.SessionDispatcherURI)
	if err != nil {
		return fmt.Errorf("Gateway RPC listener TLS: %w", err)
	}
	config, err := kubernetesConfig(c)
	if err != nil {
		return err
	}
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		return err
	}
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		return err
	}
	if err := policyapi.AddToScheme(scheme); err != nil {
		return err
	}
	if err := workloadapi.AddToScheme(scheme); err != nil {
		return err
	}
	objects, err := client.NewWithWatch(config, client.Options{Scheme: scheme})
	if err != nil {
		return err
	}
	inventoryHealth := watchhealth.New()
	inventory, err := cache.New(inventoryHealth.Config(config, watchhealth.KindByPath), cache.Options{
		Scheme: scheme, Mapper: objects.RESTMapper(),
		DefaultNamespaces: map[string]cache.Config{c.Namespace: {}},
		DefaultWatchErrorHandler: func(ctx context.Context, r *toolscache.Reflector, err error) {
			inventoryHealth.WatchErrorHandler("sandboxes")(ctx, r, err)
		},
	})
	if err != nil {
		return err
	}
	db, err := postgres.Open(ctx, c.DispatcherDatabaseURL, 8, 4)
	if err != nil {
		return fmt.Errorf("Dispatcher PostgreSQL unavailable: %w", err)
	}
	defer db.Close()
	queueStore := store.NewStore(db, c.QueueCap)
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	identities, err := startIdentities(ctx, c, config, objects)
	if err != nil {
		return err
	}
	defer identities.stop()
	wakeManager, err := wake.NewManager(wake.Options{
		Namespace: c.Namespace, Objects: objects, Identities: identities.sandboxes, WakeBudget: queueStore,
		WakeServiceCallers: c.WakeServiceCallers,
	})
	if err != nil {
		return err
	}
	catalog, err := sandboxinventory.NewKubernetesSandboxes(inventory, objects, c.Namespace, identities.sandboxes)
	if err != nil {
		return err
	}
	dispatcherSVIDs, err := auth.NewKubernetesSVIDResolver(config, []string{c.Namespace}, identities.callers)
	if err != nil {
		return fmt.Errorf("configure Gateway SVID authentication: %w", err)
	}
	failed := make(chan error, 9)
	go func() {
		reportUnexpected(ctx, failed, "Dispatcher tombstone retention", queueStore.RunTombstonePruner(ctx))
	}()
	go func() {
		reportUnexpected(ctx, failed, "Sandbox cache", inventory.Start(ctx))
	}()
	if !catalog.WaitSynced(ctx) {
		return errors.New("Sandbox inventory did not synchronize")
	}
	inventoryHealth.MarkSynced()
	hub, err := wakeups.Listen(ctx, c.DispatcherDatabaseURL, store.NotifyChannel, store.SessionNotifyChannel)
	if err != nil {
		return fmt.Errorf("Dispatcher PostgreSQL listener: %w", err)
	}
	defer hub.Close()
	host := &dispatcher.HarnessHostClient{
		Namespace: c.Namespace, TokenFile: c.RunnerTokenFile, Timeout: time.Duration(c.RunnerTimeoutSeconds) * time.Second,
		GatewayTLS:     c.GatewayTLS,
		TargetTemplate: c.RunnerControlTargetTemplate,
	}
	activity := &dispatcher.HostActivityObserver{Store: queueStore, Catalog: catalog, Client: host, Hub: hub, Namespace: c.Namespace}
	if c.CentralSampler != nil {
		activity.Sampler = &dispatcher.CentralActivityClient{Target: c.CentralSampler, Timeout: time.Duration(c.RunnerTimeoutSeconds) * time.Second}
	}
	go func() { reportUnexpected(ctx, failed, "HarnessHost activity observer", activity.Run(ctx)) }()

	runtime := &dispatcher.SessionDeliveryRouter{Host: host, Store: queueStore, Target: c.CentralSampler}

	ready := make(chan struct{})
	worker := &dispatcher.DeliveryWorker{
		Store: queueStore, Catalog: catalog, Wake: wakeManager,
		Delivery: runtime, Hub: hub, Ready: ready,
	}
	go func() { reportUnexpected(ctx, failed, "Dispatcher delivery", worker.Run(ctx)) }()
	select {
	case <-ready:
	case err := <-failed:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
	// Dispatcher admission owns this policy view independently of the Environments process.
	dispatchEnvironmentRules := access.NewIndex()
	dispatchPolicyHealth := watchhealth.New()
	dispatchPolicyWatches := reader.NewWatches(config, dispatchPolicyHealth)
	if err := reader.WatchRules(dispatchPolicyWatches, dispatchEnvironmentRules, c.PolicyNamespace, access.Ready, identities.rules); err != nil {
		return fmt.Errorf("configure Dispatcher environment policy watches: %w", err)
	}
	go dispatchPolicyWatches.Run(ctx)
	defer dispatchPolicyHealth.MarkDraining()
	dispatchPolicyReady := func() error {
		if !dispatchPolicyHealth.Available() {
			return errors.New("Dispatcher environment policy state is unavailable")
		}
		return nil
	}
	service := &dispatcher.Service{
		Store: queueStore, DispatcherServices: sets.New(c.DispatcherServiceCallers...),
		Sandboxes:        catalog,
		EnvironmentRules: dispatchEnvironmentRules, PolicyReady: dispatchPolicyReady,
		CentralTarget: c.CentralSampler, Hub: hub, WatchLease: time.Duration(c.WatchLeaseSeconds) * time.Second,
	}
	dispatcherRule, err := auth.AnySubject(dispatcherSVIDs).AllowServiceCallers(c.DispatcherServiceCallers)
	if err != nil {
		return err
	}
	dispatcherMethods := dispatcher.Methods(dispatcherRule)
	methods := auth.HealthMethods()
	maps.Copy(methods, dispatcherMethods)

	if c.DispatcherServiceDelegation != nil {
		methods, err = methods.WithServiceDelegation(c.DispatcherServiceDelegation.Methods, c.DispatcherServiceDelegation.Actors, identities.delegation)
		if err != nil {
			return fmt.Errorf("configure service delegation: %w", err)
		}
	}
	serverOptions := methods.ServerOptions(0)
	if rpcCredentials != nil {
		serverOptions = append(serverOptions, grpc.Creds(rpcCredentials))
	}
	server := grpc.NewServer(append(
		serverOptions,
		observability.GRPCServerOption(),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime:             30 * time.Second,
			PermitWithoutStream: false,
		}),
	)...)
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	defer server.Stop()
	healthServer := health.NewServer()
	healthpb.RegisterHealthServer(server, healthServer)
	go func() { reportUnexpected(ctx, failed, "gRPC listener", server.Serve(rpcListener)) }()

	// Serving once the gRPC listener accepts calls.
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	var result error
	select {
	case err := <-failed:
		result = err
	case <-ctx.Done():
		result = ctx.Err()
	}
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_NOT_SERVING)
	cancel()
	closing, release := context.WithTimeout(context.Background(), 5*time.Second)
	defer release()
	graceful.Stop(closing, server)
	return result
}

// Main loads deployment settings, runs migrations and serves until ctx is canceled.
// Its return value preserves the command's configuration and runtime exit codes.
func Main(ctx context.Context, args []string) int {
	if handled, err := migrate.RunOnly(ctx, args, dispatcherDatabaseURLEnv, migrations.Up); handled {
		if err != nil {
			slog.Error("Runner Gateway migration failed", "error", err)
			return 1
		}
		return 0
	}
	c, err := loadConfig()
	if err != nil {
		slog.Error("invalid Runner Gateway configuration", "error", err)
		return 2
	}
	shutdownTracing, err := observability.Init(ctx, "runner-gateway")
	if err != nil {
		slog.Error("Runner Gateway tracing setup failed", "error", err)
		return 1
	}
	err = run(ctx, c)
	shutdownTracing()
	if err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("Runner Gateway stopped", "error", err)
		return 1
	}
	return 0
}
