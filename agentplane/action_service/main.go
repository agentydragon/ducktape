// Command actions is the Go Action Service, over the Action Service's PostgreSQL database. On one listener it serves
// `ActionService`'s request reads, watches and cancellation and `OperatorActionService`'s Decisions over gRPC and
// their HTTP facade under /v1/, the agent documentation, the standard gRPC health service and the process's
// Prometheus metrics at /metrics, runs the dispatch worker, and keeps its policy index equal to the API server's. The
// other RPCs answer UNIMPLEMENTED, and it runs no Executor.
package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/informer"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/probes"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	_ "github.com/lib/pq"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/grpc"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

const (
	// shutdownGrace bounds how long open calls may finish once the process is told to stop, as the Python
	// service's graceful shutdown does.
	shutdownGrace = 5 * time.Second
	// readHeaderTimeout bounds a request's headers.
	readHeaderTimeout = 10 * time.Second
	// maxConnections and idleConnections size the pool as the Python service's: four kept and two more under load.
	maxConnections  = 6
	idleConnections = 4
)

// deps is who the service asks about a caller: the API server, which reviews a workload's bearer, and the view of
// which ServiceAccounts carry the caller label; and the API server whose policy objects the policy index reads.
type deps struct {
	Reviews auth.TokenReviews
	Callers api.Callers
	Policy  *rest.Config
}

// noCallers admits no workload, as if no ServiceAccount carried the caller label: the Gate does not read the policy
// index's view of the label (informer.Index) yet. Service readers and operators are admitted.
type noCallers struct{}

func (noCallers) Admits(auth.Account) bool { return false }

func inClusterDeps() (deps, error) {
	kubeConfig, err := rest.InClusterConfig()
	if err != nil {
		return deps{}, err
	}
	// Every call is a TokenReview; client-go's default of 5 requests per second (burst 10) would queue them
	// client-side. API Priority and Fairness bounds the load on the server.
	kubeConfig.QPS = -1
	kube, err := kubernetes.NewForConfig(kubeConfig)
	if err != nil {
		return deps{}, err
	}
	return deps{Reviews: kube.AuthenticationV1().TokenReviews(), Callers: noCallers{}, Policy: kubeConfig}, nil
}

// run serves on the configured address until ctx ends, authenticating workloads with the cluster's TokenReview API
// and reading the cluster's policy objects.
func run(ctx context.Context, c config) error {
	cluster, err := inClusterDeps()
	if err != nil {
		return err
	}
	listener, err := net.Listen("tcp", net.JoinHostPort(c.Host, strconv.Itoa(c.Port)))
	if err != nil {
		return err
	}
	defer listener.Close()
	return serve(ctx, c, cluster, listener)
}

func openDatabase(ctx context.Context, databaseURL string) (*sql.DB, error) {
	db, err := sql.Open("postgres", databaseURL)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(maxConnections)
	db.SetMaxIdleConns(idleConnections)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("Action Service PostgreSQL unavailable: %w", err)
	}
	return db, nil
}

// executors routes each Action the catalog offers to the Executor of its group. An Action the catalog does not offer,
// or of a group no Executor runs, fails as unsupported, as in the Python service; one of a group that is unavailable
// waits for it to come back.
type executors struct {
	catalog *catalog.Catalog
	// byGroup holds the Executor of each group that has one, by key. No Executor is ported yet.
	byGroup map[string]worker.Executor
}

func (e executors) Route(action store.ActionIdentity) (worker.Executor, bool) {
	executor, bound := e.byGroup[action.Group]
	if !bound {
		return nil, false
	}
	switch _, _, err := e.catalog.Resolve(action.Group, action.Name); {
	case errors.Is(err, catalog.ErrUnavailable):
		return executor, false
	case err != nil:
		return nil, false
	}
	return executor, true
}

// readiness is the health service of probes, whose readiness also needs the policy index available: a replica whose
// watches have wedged would auto-decide from a frozen copy, so it leaves the Service instead.
type readiness struct {
	*probes.Probes
	policy *reader.Freshness
}

func (r readiness) Check(ctx context.Context, request *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	if request.GetService() == "" {
		if status := r.policy.Status(time.Now()); !status.Available {
			slog.WarnContext(ctx, "policy index unavailable", "synced", status.Synced, "stale_after", status.StaleAfter, "ages", status.Ages)
			return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_NOT_SERVING}, nil
		}
	}
	return r.Probes.Check(ctx, request)
}

// gate admits the callers of the service's RPCs.
func gate(c config, d deps) (*api.Gate, error) {
	workloads, err := auth.NewResolver(d.Reviews, c.TokenAudience, c.CallerServiceAccountNamespaces)
	if err != nil {
		return nil, err
	}
	options := api.GateOptions{Workloads: workloads, Callers: d.Callers, ReaderAccounts: c.ReaderAccounts}
	if c.operatorsEnabled() {
		if options.Operators, err = api.NewOperatorVerifier(c.OperatorOIDC); err != nil {
			return nil, err
		}
	}
	return api.NewGate(options), nil
}

// newRoutes is the listener's HTTP routes: the HTTP form of the gRPC services under /v1/ (served by httpFacade), the
// agent documentation, which needs neither the database nor a peer, and the metrics.
func newRoutes(httpFacade http.Handler) (http.Handler, error) {
	docs, err := fs.Sub(agentDocs, "docs/agents")
	if err != nil {
		return nil, err
	}
	routes := http.NewServeMux()
	routes.Handle("/v1/", httpFacade)
	routes.Handle("/metrics", promhttp.Handler())
	facade.MountAgentDocs(routes, docs, openAPI)
	return routes, nil
}

// serve answers on listener, runs the dispatch worker and keeps the policy index until ctx ends or the listener fails.
// gRPC and the HTTP routes share the listener. The worker drains from then on, beside the listener's shutdown, and
// serve returns once it has and the policy watches have stopped.
func serve(ctx context.Context, c config, d deps, listener net.Listener) error {
	admission, err := gate(c, d)
	if err != nil {
		return err
	}
	db, err := openDatabase(ctx, c.DatabaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	hub, err := wakeups.Listen(ctx, c.DatabaseURL, store.UpdatesChannel, store.DispatchChannel)
	if err != nil {
		return fmt.Errorf("Action Service PostgreSQL listener unavailable: %w", err)
	}
	defer hub.Close()

	dispatches, unsubscribe, err := hub.Subscribe(store.DispatchChannel)
	if err != nil {
		return err
	}
	defer unsubscribe()
	requests := store.New(db)
	dispatcher, err := worker.New(requests, executors{catalog: c.ActionGroups}, worker.DefaultTiming())
	if err != nil {
		return err
	}

	watchPeriod := c.policyWatchPeriod()
	policy := informer.NewIndex(reader.StaleAfterCycles*watchPeriod, c.CallerServiceAccountNamespaces)
	policyInformer, err := informer.New(d.Policy, policy, c.PolicyNamespace, watchPeriod)
	if err != nil {
		return err
	}
	// Until the watches have listed, the replica is not ready and the index admits no caller and resolves no binding.
	watchContext, stopWatching := context.WithCancel(ctx)
	watching := make(chan struct{})
	go func() {
		defer close(watching)
		policyInformer.Run(watchContext)
	}()
	defer func() {
		stopWatching()
		<-watching
	}()

	health := probes.New(db, hub)
	grpcServer := grpc.NewServer(
		grpc.ChainUnaryInterceptor(admission.UnaryInterceptor), grpc.ChainStreamInterceptor(admission.StreamInterceptor),
	)
	healthpb.RegisterHealthServer(grpcServer, readiness{Probes: health, policy: policy.Freshness})
	action_servicepb.RegisterActionServiceServer(grpcServer, api.NewService(requests, hub, admission))
	operator_action_servicepb.RegisterOperatorActionServiceServer(grpcServer, api.NewOperatorService(requests))
	// The facade is the listener's own client: its gRPC calls come back to the gRPC server mounted on it, so the
	// Gate admits every request. It outlives the termination signal, so that calls admitted before it finish
	// while draining.
	facadeContext, stopFacade := context.WithCancel(context.WithoutCancel(ctx))
	defer stopFacade()
	httpFacade, err := facade.NewHandler(
		facadeContext, listener.Addr().String(),
		action_servicepb.RegisterActionServiceHandlerFromEndpoint,
		operator_action_servicepb.RegisterOperatorActionServiceHandlerFromEndpoint,
	)
	if err != nil {
		return err
	}
	routes, err := newRoutes(httpFacade)
	if err != nil {
		return err
	}
	server := &http.Server{Handler: routes, ReadHeaderTimeout: readHeaderTimeout}
	facade.MountGRPC(server, grpcServer)

	working, drain := context.WithCancel(ctx)
	worked := make(chan struct{})
	go func() {
		defer close(worked)
		dispatcher.Run(working, dispatches)
	}()
	failed := make(chan error, 1)
	go func() { failed <- fmt.Errorf("listener: %w", server.Serve(listener)) }()
	var result error
	select {
	case result = <-failed:
	case <-ctx.Done():
		result = ctx.Err()
	}
	health.Drain()
	admission.Drain()
	drain()
	stopped, cancel := context.WithTimeout(context.WithoutCancel(ctx), shutdownGrace)
	defer cancel()
	if err := server.Shutdown(stopped); err != nil {
		server.Close()
	}
	<-worked
	return result
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	c, err := loadConfig()
	if err != nil {
		slog.Error("invalid Action Service configuration", "error", err)
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, c); err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("Action Service stopped", "error", err)
		os.Exit(1)
	}
}
