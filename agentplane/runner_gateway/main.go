package main

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"net"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	netlistener "github.com/agentydragon/ducktape/agentplane/net/listener"
	"github.com/agentydragon/ducktape/agentplane/observability"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/gateway"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/migrations"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/go-logr/logr"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/metadata"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	toolscache "k8s.io/client-go/tools/cache"
	"k8s.io/client-go/tools/clientcmd"
	kubevirtv1 "kubevirt.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	ctrllog "sigs.k8s.io/controller-runtime/pkg/log"
)

func kubernetesConfig(c config) (*rest.Config, error) {
	var kubeConfig *rest.Config
	var err error
	if c.Kubeconfig != "" {
		kubeConfig, err = clientcmd.BuildConfigFromFlags("", c.Kubeconfig)
	} else {
		kubeConfig, err = rest.InClusterConfig()
	}
	if err != nil {
		return nil, err
	}
	// Zero keeps client-go's 5 QPS / burst 10 limiter, which once queued Dispatcher ownership Lease
	// acquisitions for seconds each; the deployment sets -1 to disable it.
	kubeConfig.QPS = c.KubernetesClientQPS
	kubeConfig.WrapTransport = observability.WrapHTTPTransport(kubeConfig.WrapTransport)
	return kubeConfig, nil
}

func accountSet(accounts []auth.Account) map[auth.Account]struct{} {
	result := make(map[auth.Account]struct{}, len(accounts))
	for _, account := range accounts {
		result[account] = struct{}{}
	}
	return result
}

// dispatcherAccount proves that the token used by delivery workers belongs to this
// Gateway/Dispatcher Pod. Caller headers and an account allowlist alone cannot identify
// the combined process; TokenReview's Pod UID must agree with the current Pod as well.
func dispatcherAccount(ctx context.Context, c config, resolver *auth.Resolver, objects client.Reader) (auth.Account, error) {
	token, err := os.ReadFile(c.RunnerTokenFile)
	if err != nil {
		return auth.Account{}, errors.New("could not read Dispatcher workload credential")
	}
	ctx = metadata.NewIncomingContext(ctx, metadata.Pairs("authorization", "Bearer "+strings.TrimSpace(string(token))))
	principal, err := resolver.RequireAccount(ctx, accountSet(c.GatewayCallers))
	if err != nil {
		return auth.Account{}, fmt.Errorf("review Dispatcher workload credential: %w", err)
	}
	if principal.Account.Namespace != c.Namespace || principal.PodName != c.PodName {
		return auth.Account{}, errors.New("Dispatcher workload credential belongs to another Pod")
	}
	var pod corev1.Pod
	if err := objects.Get(ctx, client.ObjectKey{Namespace: principal.Account.Namespace, Name: c.PodName}, &pod); err != nil {
		return auth.Account{}, fmt.Errorf("read Dispatcher workload Pod: %w", err)
	}
	if string(pod.UID) != principal.PodUID || pod.Spec.ServiceAccountName != principal.Account.Name {
		return auth.Account{}, errors.New("Dispatcher workload credential disagrees with current Pod identity")
	}
	return principal.Account, nil
}

func accountNamespaces(accounts []auth.Account, extra *auth.Account, additional ...auth.Account) []string {
	names := make(map[string]struct{}, len(accounts)+1+len(additional))
	for _, account := range accounts {
		names[account.Namespace] = struct{}{}
	}
	if extra != nil {
		names[extra.Namespace] = struct{}{}
	}
	for _, account := range additional {
		if account.Namespace != "" {
			names[account.Namespace] = struct{}{}
		}
	}
	result := make([]string, 0, len(names))
	for name := range names {
		result = append(result, name)
	}
	return result
}

type lifecycleWakeBudget struct {
	store *dispatcher.Store
}

func (b lifecycleWakeBudget) ReserveWakeAttempt(
	ctx context.Context,
	caller *lifecycle.ServiceAccountIdentity,
	sandboxUID string,
	generation int64,
) (time.Duration, error) {
	return b.store.ReserveWakeAttempt(ctx, caller, sandboxUID, generation)
}

func waitForListener(ctx context.Context, hub *dispatcher.WatchHub, failed <-chan error) error {
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()
	for !hub.Connected() {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-failed:
			return fmt.Errorf("PostgreSQL listener failed before startup: %w", err)
		case <-ticker.C:
		}
	}
	return nil
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

// serve uses the supplied shared gRPC listener.
func serve(ctx context.Context, c config, rpcListener net.Listener) error {
	if err := c.RunnerSecurity.Validate(); err != nil {
		return err
	}
	rpcCredentials, err := c.RPC.ServerCredentials(environment_runtime_transport.SessionDispatcherURI)
	if err != nil {
		return fmt.Errorf("Gateway RPC listener TLS: %w", err)
	}
	if c.RunnerSecurity.Mode == environment_runtime_transport.MutualTLS {
		identity, err := c.RunnerSecurity.TLS.LocalIdentity(x509.ExtKeyUsageClientAuth)
		if err != nil {
			return err
		}
		if identity != environment_runtime_transport.GatewayURI {
			return errors.New("Gateway runner certificate has the wrong identity")
		}
	}
	config, err := kubernetesConfig(c)
	if err != nil {
		return err
	}
	kube, err := kubernetes.NewForConfig(config)
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
	if err := kubevirtv1.AddToScheme(scheme); err != nil {
		return err
	}
	objects, err := client.NewWithWatch(config, client.Options{Scheme: scheme})
	if err != nil {
		return err
	}
	inventoryHealth := watchhealth.New()
	// A reflector names its type as the Go type; the health transport names a kind by its path.
	kindByType := make(map[string]watchhealth.Kind)
	for kind, object := range map[watchhealth.Kind]client.Object{
		"sandboxes": &sandboxapi.Sandbox{}, "pods": &corev1.Pod{},
		"virtualmachineinstances": &kubevirtv1.VirtualMachineInstance{}, "virtualmachines": &kubevirtv1.VirtualMachine{},
	} {
		kindByType[fmt.Sprintf("%T", object)] = kind
	}
	// KubeVirt informers start on the first VM Sandbox's read, so clusters without KubeVirt never watch it.
	inventory, err := cache.New(inventoryHealth.Config(config, watchhealth.KindByPath), cache.Options{
		Scheme: scheme, Mapper: objects.RESTMapper(),
		DefaultNamespaces: map[string]cache.Config{c.Namespace: {}},
		DefaultWatchErrorHandler: func(ctx context.Context, r *toolscache.Reflector, err error) {
			inventoryHealth.WatchErrorHandler(kindByType[r.TypeDescription()])(ctx, r, err)
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
	store := dispatcher.NewStore(db, c.QueueCap)
	lifecycleManager, err := lifecycle.NewManager(lifecycle.Options{
		Namespace: c.Namespace, Objects: objects, WakeBudget: lifecycleWakeBudget{store: store},
		WakeCallers: c.WakeCallers, OperatorCallers: c.OperatorCallers,
	})
	if err != nil {
		return err
	}
	catalog, err := dispatcher.NewKubernetesSandboxes(inventory, objects, c.Namespace)
	if err != nil {
		return err
	}
	environmentService, err := environments.New(c.Namespace, objects, c.RuntimeCatalog, lifecycleManager, accountSet(c.EnvironmentReaders), environments.ManagementOptions{
		Managers: accountSet(c.EnvironmentManagers), Launchers: accountSet(c.EnvironmentLaunchers), DefaultPolicySets: c.EnvironmentDefaultPolicySets,
	})
	if err != nil {
		return err
	}
	var delegatedActors []auth.Account
	if c.Delegation != nil {
		delegatedActors = append(delegatedActors, c.Delegation.Actor)
	}
	dispatchAccounts := append(append(append(append([]auth.Account{}, c.DispatcherCallers...), c.EnvironmentReaders...), c.EnvironmentManagers...), c.EnvironmentLaunchers...)
	workloadNamespaces := accountNamespaces(dispatchAccounts, &auth.Account{Namespace: c.Namespace}, delegatedActors...)
	dispatchAuth, err := auth.NewResolver(kube.AuthenticationV1().TokenReviews(), c.DispatcherAudience, workloadNamespaces)
	if err != nil {
		return err
	}
	dispatcherSVIDs, err := auth.NewKubernetesSVIDResolver(config, []string{c.Namespace})
	if err != nil {
		return fmt.Errorf("configure Gateway SVID authentication: %w", err)
	}
	dispatcherAuth, err := dispatchAuth.WithListenerAuthentication(c.rpcForMethodAudience(c.DispatcherAudience, true), dispatcherSVIDs)
	if err != nil {
		return fmt.Errorf("configure Gateway Dispatcher authentication: %w", err)
	}
	gatewayAuth, err := auth.NewResolver(kube.AuthenticationV1().TokenReviews(), c.GatewayAudience,
		accountNamespaces(append(append([]auth.Account{}, c.GatewayCallers...), c.ExecutorCallers...), c.ControllerAccount))
	if err != nil {
		return err
	}
	gatewayRPCAuth, err := gatewayAuth.WithListenerAuthentication(c.rpcForMethodAudience(c.GatewayAudience, false), nil)
	if err != nil {
		return fmt.Errorf("configure Gateway proxy authentication: %w", err)
	}
	deliveryAccount, err := dispatcherAccount(ctx, c, gatewayAuth, objects)
	if err != nil {
		return err
	}
	g, err := gateway.New(gateway.Config{
		RunnerSecurity: c.RunnerSecurity,
		Namespace:      c.Namespace, CallerAccounts: c.GatewayCallers, ExecutorCallers: c.ExecutorCallers,
		DispatcherAccount:        deliveryAccount,
		SandboxControllerAccount: c.ControllerAccount,
		HolderName:               c.PodName, HolderIP: c.PodIP, ProxyPort: int32(c.ProxyPort),
	}, gatewayRPCAuth, inventory, inventoryHealth, kube.CoordinationV1(), kube.DiscoveryV1())
	if err != nil {
		return err
	}
	for _, watched := range []client.Object{&sandboxapi.Sandbox{}, &corev1.Pod{}} {
		informer, err := inventory.GetInformer(ctx, watched)
		if err != nil {
			return err
		}
		if _, err := informer.AddEventHandler(g.RunnerDiscoveryHandler()); err != nil {
			return err
		}
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	failed := make(chan error, 8)
	go func() {
		reportUnexpected(ctx, failed, "Sandbox cache", inventory.Start(ctx))
	}()
	if !catalog.WaitSynced(ctx) {
		return errors.New("Sandbox inventory did not synchronize")
	}
	inventoryHealth.MarkSynced()
	if err := g.WatchConnectionLeases(ctx, kube); err != nil {
		return err
	}
	hub := dispatcher.NewWatchHub(c.DispatcherDatabaseURL)
	go func() { reportUnexpected(ctx, failed, "PostgreSQL listener", hub.Run(ctx)) }()
	if err := waitForListener(ctx, hub, failed); err != nil {
		return err
	}
	nativeRunner := &dispatcher.ProxyRunner{
		Namespace: c.Namespace, TokenFile: c.RunnerTokenFile, Timeout: time.Duration(c.RunnerTimeoutSeconds) * time.Second,
		GatewayTLS:     c.GatewayTLS,
		TargetTemplate: c.RunnerControlTargetTemplate,
	}
	runner := &dispatcher.RoutingRunner{Native: nativeRunner, Store: store, Target: c.CentralSampler, Timeout: time.Duration(c.RunnerTimeoutSeconds) * time.Second}

	ready := make(chan struct{})
	worker := &dispatcher.DeliveryWorker{
		Store: store, Catalog: catalog, Wake: lifecycleManager,
		Leases: dispatcher.NewOwnershipLeases(kube.CoordinationV1().Leases(c.Namespace), store, c.Namespace),
		Runner: runner, Hub: hub, Ready: ready,
	}
	go func() { reportUnexpected(ctx, failed, "Dispatcher delivery", worker.Run(ctx)) }()
	select {
	case <-ready:
	case err := <-failed:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
	service := &dispatcher.Service{
		Store: store, DispatcherCallers: accountSet(c.DispatcherCallers),
		Sandboxes:     catalog,
		CentralTarget: c.CentralSampler, Hub: hub, WatchLease: time.Duration(c.WatchLeaseSeconds) * time.Second,
	}
	dispatcherMethods := dispatcher.Methods(auth.ReviewedServiceAccount(dispatcherAuth))
	methods := auth.HealthMethods()
	maps.Copy(methods, dispatcherMethods)
	maps.Copy(methods, environments.Methods(dispatcherAuth))
	if c.Delegation != nil {
		delegator := auth.Delegator{Account: c.Delegation.Actor, CallerNamespaces: c.Delegation.CallerNamespaces}
		uidReader := auth.ServiceAccountUIDReaderFunc(func(ctx context.Context, account auth.Account) (string, bool, error) {
			serviceAccount, err := kube.CoreV1().ServiceAccounts(account.Namespace).Get(ctx, account.Name, metav1.GetOptions{})
			if apierrors.IsNotFound(err) {
				return "", false, nil
			}
			if err != nil {
				return "", false, err
			}
			return string(serviceAccount.UID), true, nil
		})
		methods, err = methods.WithDelegation(c.Delegation.Methods, []auth.Delegator{delegator}, uidReader)
		if err != nil {
			return fmt.Errorf("configure Runner Gateway delegation: %w", err)
		}
	}
	// Delegation remains scoped to the configured Dispatcher and Environment methods. The Gateway proxy and Executor
	// continue using only their separate Gateway-audience bearer resolver.
	maps.Copy(methods, g.Methods())
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
	environmentpb.RegisterEnvironmentServiceServer(server, environmentService)
	defer server.Stop()
	g.RegisterProxy(server)
	if len(c.ExecutorCallers) > 0 {
		g.RegisterExecutor(server)
	}
	healthServer := health.NewServer()
	healthpb.RegisterHealthServer(server, healthServer)
	go func() { reportUnexpected(ctx, failed, "gRPC listener", server.Serve(rpcListener)) }()
	go func() { reportUnexpected(ctx, failed, "Runner observer supervisor", g.RunRunnerObservers(ctx)) }()

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

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctrllog.SetLogger(logr.FromSlogHandler(slog.Default().Handler()))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if handled, err := migrate.RunOnly(ctx, os.Args[1:], "AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL", migrations.Up); handled {
		if err != nil {
			slog.Error("Runner Gateway migration failed", "error", err)
			os.Exit(1)
		}
		return
	}
	c, err := loadConfig()
	if err != nil {
		slog.Error("invalid Runner Gateway configuration", "error", err)
		os.Exit(2)
	}
	shutdownTracing, err := observability.Init(ctx, "runner-gateway")
	if err != nil {
		slog.Error("Runner Gateway tracing setup failed", "error", err)
		os.Exit(1)
	}
	err = run(ctx, c)
	shutdownTracing()
	if err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("Runner Gateway stopped", "error", err)
		os.Exit(1)
	}
}
