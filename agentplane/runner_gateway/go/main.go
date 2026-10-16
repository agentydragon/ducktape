package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

	dispatcherpb "github.com/agentydragon/ducktape/agentplane/proto/session_dispatcher"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/auth"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/dispatcher"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/go/gateway"
	_ "github.com/lib/pq"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

const (
	proxyAddress      = ":8080"
	dispatcherAddress = ":8081"
	controlAddress    = ":8082"
)

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

func serve(ctx context.Context, c config) error {
	config, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	kube, err := kubernetes.NewForConfig(config)
	if err != nil {
		return err
	}
	objects, err := dynamic.NewForConfig(config)
	if err != nil {
		return err
	}
	db, err := sql.Open("postgres", c.DispatcherDatabaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(4)
	if err := db.PingContext(ctx); err != nil {
		return fmt.Errorf("Dispatcher PostgreSQL unavailable: %w", err)
	}
	store := dispatcher.NewStore(db, c.QueueCap)
	catalog, err := dispatcher.NewKubernetesSandboxes(objects, c.Namespace)
	if err != nil {
		return err
	}
	dispatchAuth, err := auth.NewResolver(kube.AuthenticationV1().TokenReviews(), c.DispatcherAudience,
		accountNamespaces(c.DispatcherCallers, nil))
	if err != nil {
		return err
	}
	gatewayAuth, err := auth.NewResolver(kube.AuthenticationV1().TokenReviews(), c.GatewayAudience,
		accountNamespaces(c.GatewayCallers, c.ControllerAccount))
	if err != nil {
		return err
	}
	runnerAuth, err := auth.NewResolver(kube.AuthenticationV1().TokenReviews(), c.RunnerAudience, []string{c.Namespace})
	if err != nil {
		return err
	}
	g, err := gateway.New(gateway.Config{
		Namespace: c.Namespace, CallerAccounts: c.GatewayCallers,
		SandboxControllerAccount: c.ControllerAccount,
		HolderName:               c.PodName, HolderIP: c.PodIP, ProxyPort: 8080,
	}, gatewayAuth, runnerAuth, objects, kube.CoreV1(), kube.CoordinationV1(), kube.DiscoveryV1())
	if err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	failed := make(chan error, 6)
	go func() {
		catalog.Run(ctx)
		if ctx.Err() == nil {
			failed <- errors.New("Sandbox informer stopped")
		}
	}()
	if !catalog.WaitSynced(ctx) {
		return errors.New("Sandbox inventory did not synchronize")
	}
	if err := g.WatchConnectionLeases(ctx, kube.CoordinationV1()); err != nil {
		return err
	}
	hub := dispatcher.NewWatchHub(c.DispatcherDatabaseURL)
	go func() { reportUnexpected(ctx, failed, "PostgreSQL listener", hub.Run(ctx)) }()
	if err := waitForListener(ctx, hub, failed); err != nil {
		return err
	}
	runner := &dispatcher.ProxyRunner{Namespace: c.Namespace, TokenFile: c.RunnerTokenFile, Timeout: c.RunnerTimeout}
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
		Store: store, Auth: dispatchAuth, Callers: accountSet(c.DispatcherCallers),
		WakeCallers: accountSet(c.WakeCallers), Sandboxes: catalog, Runner: runner,
		Hub: hub, WatchLease: c.WatchLease,
	}
	proxy := grpc.NewServer()
	publicDispatcher := grpc.NewServer()
	control := grpc.NewServer()
	dispatcherpb.RegisterSessionDispatcherServer(publicDispatcher, service)
	g.RegisterProxy(proxy)
	g.RegisterRunnerControl(control)
	proxyHealth, dispatcherHealth, controlHealth := health.NewServer(), health.NewServer(), health.NewServer()
	healthpb.RegisterHealthServer(proxy, proxyHealth)
	healthpb.RegisterHealthServer(publicDispatcher, dispatcherHealth)
	healthpb.RegisterHealthServer(control, controlHealth)
	proxyListener, err := net.Listen("tcp", proxyAddress)
	if err != nil {
		return err
	}
	defer proxyListener.Close()
	dispatcherListener, err := net.Listen("tcp", dispatcherAddress)
	if err != nil {
		return err
	}
	defer dispatcherListener.Close()
	controlListener, err := net.Listen("tcp", controlAddress)
	if err != nil {
		return err
	}
	defer controlListener.Close()
	proxyHealth.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	dispatcherHealth.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	controlHealth.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	go func() { reportUnexpected(ctx, failed, "Runner proxy gRPC listener", proxy.Serve(proxyListener)) }()
	go func() {
		reportUnexpected(ctx, failed, "Dispatcher gRPC listener", publicDispatcher.Serve(dispatcherListener))
	}()
	go func() { reportUnexpected(ctx, failed, "runner Connect listener", control.Serve(controlListener)) }()
	var result error
	select {
	case err := <-failed:
		result = err
	case <-ctx.Done():
		result = ctx.Err()
	}
	proxyHealth.SetServingStatus("", healthpb.HealthCheckResponse_NOT_SERVING)
	dispatcherHealth.SetServingStatus("", healthpb.HealthCheckResponse_NOT_SERVING)
	controlHealth.SetServingStatus("", healthpb.HealthCheckResponse_NOT_SERVING)
	cancel()
	stopped := make(chan struct{})
	go func() { proxy.GracefulStop(); publicDispatcher.GracefulStop(); control.GracefulStop(); close(stopped) }()
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		proxy.Stop()
		publicDispatcher.Stop()
		control.Stop()
	}
	return result
}

func main() {
	c, err := loadConfig(os.Args[1:], os.Getenv)
	if err != nil {
		slog.Error("invalid Runner Gateway configuration", "error", err)
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := serve(ctx, c); err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("Runner Gateway stopped", "error", err)
		os.Exit(1)
	}
}
