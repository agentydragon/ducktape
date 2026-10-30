package main

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres/go"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/gateway"
	"github.com/agentydragon/ducktape/agentplane/runner_transport"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/agentydragon/ducktape/util/netlistener"
	"github.com/go-logr/logr"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/keepalive"
	corev1 "k8s.io/api/core/v1"
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

func accountNamespaces(accounts []auth.Account, extra *auth.Account) []string {
	names := make(map[string]struct{}, len(accounts)+1)
	for _, account := range accounts {
		names[account.Namespace] = struct{}{}
	}
	if extra != nil {
		names[extra.Namespace] = struct{}{}
	}
	result := make([]string, 0, len(names))
	for name := range names {
		result = append(result, name)
	}
	return result
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

// serveFacade answers the Dispatcher's HTTP/JSON form on listener, forwarding each request over
// loopback to the gRPC server so the servicer authenticates it.
func serveFacade(ctx context.Context, listener net.Listener, grpcListener net.Listener, failed chan<- error) (*http.Server, error) {
	handler, err := facade.NewHandler(ctx, net.JoinHostPort("127.0.0.1", strconv.Itoa(grpcListener.Addr().(*net.TCPAddr).Port)), 0,
		dispatcherpb.RegisterSessionDispatcherHandlerFromEndpoint)
	if err != nil {
		return nil, err
	}
	// No write timeout: WatchDispatches streams for its lease.
	server := &http.Server{Handler: observability.HTTPHandler(handler), ReadHeaderTimeout: 10 * time.Second}
	go func() { reportUnexpected(ctx, failed, "HTTP listener", server.Serve(listener)) }()
	return server, nil
}

func listenAndServe(ctx context.Context, c config) error {
	listener, err := netlistener.Open(c.ListenAddress)
	if err != nil {
		return fmt.Errorf("gRPC listener: %w", err)
	}
	defer listener.Close()
	var httpListener net.Listener
	if c.HTTPListenAddress != "" {
		httpListener, err = netlistener.Open(c.HTTPListenAddress)
		if err != nil {
			return fmt.Errorf("HTTP listener: %w", err)
		}
		defer httpListener.Close()
	}
	return serve(ctx, c, listener, httpListener)
}

// serve uses supplied listeners; a nil HTTP listener disables the facade.
func serve(ctx context.Context, c config, listener, httpListener net.Listener) error {
	if err := c.RunnerSecurity.Validate(); err != nil {
		return err
	}
	if c.RunnerSecurity.Mode == runner_transport.MutualTLS {
		identity, err := c.RunnerSecurity.TLS.LocalIdentity(x509.ExtKeyUsageClientAuth)
		if err != nil {
			return err
		}
		if identity != runner_transport.GatewayURI {
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
	if err := kubevirtv1.AddToScheme(scheme); err != nil {
		return err
	}
	objects, err := client.New(config, client.Options{Scheme: scheme})
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
	catalog, err := dispatcher.NewKubernetesSandboxes(inventory, objects, c.Namespace)
	if err != nil {
		return err
	}
	dispatchAuth, err := auth.NewResolver(kube.AuthenticationV1().TokenReviews(), c.DispatcherAudience,
		accountNamespaces(c.DispatcherCallers, nil))
	if err != nil {
		return err
	}
	gatewayAuth, err := auth.NewResolver(kube.AuthenticationV1().TokenReviews(), c.GatewayAudience,
		accountNamespaces(append(append([]auth.Account{}, c.GatewayCallers...), c.ExecutorCallers...), c.ControllerAccount))
	if err != nil {
		return err
	}
	g, err := gateway.New(gateway.Config{
		RunnerSecurity: c.RunnerSecurity,
		Namespace:      c.Namespace, CallerAccounts: c.GatewayCallers, ExecutorCallers: c.ExecutorCallers,
		SandboxControllerAccount: c.ControllerAccount,
		HolderName:               c.PodName, HolderIP: c.PodIP, ProxyPort: int32(c.ProxyPort),
	}, gatewayAuth, inventory, inventoryHealth, kube.CoordinationV1(), kube.DiscoveryV1())
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
		TargetTemplate: c.RunnerControlTargetTemplate,
	}
	runner := &dispatcher.RoutingRunner{Native: nativeRunner, Store: store, Target: c.CentralSampler, Timeout: time.Duration(c.RunnerTimeoutSeconds) * time.Second}

	ready := make(chan struct{})
	worker := &dispatcher.DeliveryWorker{
		Store: store, Catalog: catalog, Wake: catalog,
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
		Store: store, WakeCallers: accountSet(c.WakeCallers), Sandboxes: catalog, Runner: runner,
		CentralTarget: c.CentralSampler, Hub: hub, WatchLease: time.Duration(c.WatchLeaseSeconds) * time.Second,
	}
	methods := auth.HealthMethods()
	maps.Copy(methods, dispatcher.Methods(dispatchAuth, c.DispatcherCallers))
	maps.Copy(methods, g.Methods())
	server := grpc.NewServer(append(
		methods.ServerOptions(0),
		observability.GRPCServerOption(),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime:             30 * time.Second,
			PermitWithoutStream: false,
		}),
	)...)
	dispatcherpb.RegisterSessionDispatcherServer(server, service)
	defer server.Stop()
	g.RegisterProxy(server)
	if len(c.ExecutorCallers) > 0 {
		g.RegisterExecutor(server)
	}
	healthServer := health.NewServer()
	healthpb.RegisterHealthServer(server, healthServer)
	go func() { reportUnexpected(ctx, failed, "gRPC listener", server.Serve(listener)) }()
	go func() { reportUnexpected(ctx, failed, "Runner observer supervisor", g.RunRunnerObservers(ctx)) }()

	var facadeServer *http.Server
	if httpListener != nil {
		if facadeServer, err = serveFacade(ctx, httpListener, listener, failed); err != nil {
			return err
		}
	}
	// Serving once both listeners accept, so a ready replica answers the facade too.
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
	if facadeServer != nil {
		closing, release := context.WithTimeout(context.Background(), 5*time.Second)
		defer release()
		if err := facadeServer.Shutdown(closing); err != nil {
			slog.Warn("HTTP listener did not drain", "error", err)
		}
	}
	stopped := make(chan struct{})
	go func() { server.GracefulStop(); close(stopped) }()
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		server.Stop()
	}
	return result
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctrllog.SetLogger(logr.FromSlogHandler(slog.Default().Handler()))
	c, err := loadConfig()
	if err != nil {
		slog.Error("invalid Runner Gateway configuration", "error", err)
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	shutdownTracing, err := observability.Init(ctx, "runner-gateway")
	if err != nil {
		slog.Error("Runner Gateway tracing setup failed", "error", err)
		os.Exit(1)
	}
	err = listenAndServe(ctx, c)
	shutdownTracing()
	if err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("Runner Gateway stopped", "error", err)
		os.Exit(1)
	}
}
