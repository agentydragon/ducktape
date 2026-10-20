// Command history-service takes the Session journals runners push (agentplane/docs/runner_control.md § Event
// ingress), serves retained Session history, and folds it into Threads.
package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/history_service/api"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/ingress"
	"github.com/agentydragon/ducktape/agentplane/history_service/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/reads"
	runneringresspb "github.com/agentydragon/ducktape/agentplane/history_service/runner_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	_ "github.com/lib/pq"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

const (
	// shutdownGrace bounds how long open calls may finish once the process is told to stop.
	shutdownGrace = 5 * time.Second
	// A runner heartbeats an idle call at the rate agentplane/docs/runner_control.md § Event ingress names, so the
	// ingress serves with the protocol's pacing.
	ingressIdleAck      = 10 * time.Second
	ingressSilenceLimit = 30 * time.Second
)

func kubernetesConfig(c config) (*rest.Config, error) {
	if c.Kubeconfig != "" {
		return clientcmd.BuildConfigFromFlags("", c.Kubeconfig)
	}
	return rest.InClusterConfig()
}

// readOnly is the database URL with sessions that refuse writes, so the read API cannot write.
func readOnly(databaseURL string) (string, error) {
	target, err := url.Parse(databaseURL)
	if err != nil {
		return "", errors.New("database_url is not a URL")
	}
	query := target.Query()
	if query.Has("options") {
		return "", errors.New("database_url must not set options")
	}
	query.Set("options", "-c default_transaction_read_only=on")
	target.RawQuery = query.Encode()
	return target.String(), nil
}

func openDatabase(ctx context.Context, databaseURL string, connections int) (*sql.DB, error) {
	db, err := sql.Open("postgres", databaseURL)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(connections)
	db.SetMaxIdleConns(connections / 2)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("History Service PostgreSQL unavailable: %w", err)
	}
	return db, nil
}

func readerNamespaces(accounts []auth.Account) []string {
	namespaces := make(map[string]struct{}, len(accounts))
	for _, account := range accounts {
		namespaces[account.Namespace] = struct{}{}
	}
	result := make([]string, 0, len(namespaces))
	for namespace := range namespaces {
		result = append(result, namespace)
	}
	return result
}

// run serves on the configured addresses, authenticating callers with the cluster's TokenReview API.
func run(ctx context.Context, c config) error {
	kubeConfig, err := kubernetesConfig(c)
	if err != nil {
		return err
	}
	kube, err := kubernetes.NewForConfig(kubeConfig)
	if err != nil {
		return err
	}
	listener, err := net.Listen("tcp", net.JoinHostPort(c.Host, strconv.Itoa(c.Port)))
	if err != nil {
		return err
	}
	defer listener.Close()
	// A separate listener, so the network fence can admit the egress proxy to the ingress and nothing else.
	ingressListener, err := net.Listen("tcp", net.JoinHostPort(c.Host, strconv.Itoa(c.Ingress.Port)))
	if err != nil {
		return err
	}
	defer ingressListener.Close()
	return serve(ctx, c, kube.AuthenticationV1().TokenReviews(), listener, ingressListener)
}

// serve answers the read API on listener and the runner ingress on ingressListener until ctx ends.
func serve(ctx context.Context, c config, reviews auth.TokenReviews, listener, ingressListener net.Listener) error {
	readerAuth, err := auth.NewResolver(reviews, c.TokenAudience, readerNamespaces(c.ReaderAccounts))
	if err != nil {
		return err
	}
	writerAuth, err := auth.NewResolver(reviews, c.Ingress.TokenAudience, c.Ingress.WriterNamespaces)
	if err != nil {
		return err
	}
	// The migration init container owns the schema; the fold and the ingress write through their own pools, and
	// the read API's never writes.
	readURL, err := readOnly(c.DatabaseURL)
	if err != nil {
		return err
	}
	readDB, err := openDatabase(ctx, readURL, 6)
	if err != nil {
		return err
	}
	defer readDB.Close()
	foldDB, err := openDatabase(ctx, c.DatabaseURL, 4)
	if err != nil {
		return err
	}
	defer foldDB.Close()
	ingressDB, err := openDatabase(ctx, c.DatabaseURL, 12)
	if err != nil {
		return err
	}
	defer ingressDB.Close()
	hub, err := wakeups.Listen(ctx, c.DatabaseURL, storage.ChannelHistory, projection.ChannelSummaries, storage.ChannelSessions)
	if err != nil {
		return fmt.Errorf("History Service PostgreSQL listener unavailable: %w", err)
	}
	defer hub.Close()

	// The read store never appends, so it settles nothing.
	history := storage.NewStore(readDB, false)
	service := api.NewService(api.Options{
		Reader: &reads.Reader{DB: readDB, History: history}, Wakeups: hub, Resolver: readerAuth, Callers: c.ReaderAccounts,
		RequestTimeout: seconds(c.RequestTimeoutSeconds), WatchLease: seconds(c.WatchLeaseSeconds),
	})
	server := grpc.NewServer()
	historypb.RegisterHistoryServiceServer(server, service)
	healthServer := health.NewServer()
	healthpb.RegisterHealthServer(server, healthServer)
	ingressServer := grpc.NewServer()
	runneringresspb.RegisterRunnerIngressServer(ingressServer, &ingress.Service{
		Principals: writerAuth, History: storage.NewStore(ingressDB, c.Ingress.SettleDeltas),
		Timing: ingress.Timing{IdleAck: ingressIdleAck, SilenceLimit: ingressSilenceLimit},
	})
	changes, unsubscribe, err := hub.Subscribe(storage.ChannelHistory)
	if err != nil {
		return err
	}
	defer unsubscribe()
	foldContext, stopFold := context.WithCancel(context.WithoutCancel(ctx))
	defer stopFold()
	folded := make(chan struct{})
	go func() {
		defer close(folded)
		projection.NewFoldRunner(foldDB, history, c.FoldConcurrency, c.FoldPageSize).
			Run(foldContext, seconds(c.FoldIntervalSeconds), changes, healthServer)
	}()

	failed := make(chan error, 2)
	go func() { failed <- fmt.Errorf("gRPC listener: %w", server.Serve(listener)) }()
	go func() { failed <- fmt.Errorf("runner ingress listener: %w", ingressServer.Serve(ingressListener)) }()

	var result error
	select {
	case result = <-failed:
	case <-ctx.Done():
		result = ctx.Err()
	}
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_NOT_SERVING)
	// Open watches end with reconnect_required rather than holding the shutdown until their leases.
	service.Drain()
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		ingressStopped := make(chan struct{})
		go func() { ingressServer.GracefulStop(); close(ingressStopped) }()
		server.GracefulStop()
		<-ingressStopped
	}()
	select {
	case <-stopped:
	case <-time.After(shutdownGrace):
		server.Stop()
		ingressServer.Stop()
	}
	stopFold()
	<-folded
	return result
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	c, err := loadConfig()
	if err != nil {
		slog.Error("invalid History Service configuration", "error", err)
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, c); err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("History Service stopped", "error", err)
		os.Exit(1)
	}
}
