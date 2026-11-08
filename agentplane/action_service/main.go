// Command actions is the Go Action Service, over the Action Service's PostgreSQL database. It serves Action,
// operator, linkage, Web Push and health APIs over gRPC. It runs the dispatch worker, which
// runs the Actions of the sandbox, orchestration_v1 and MCP groups, refreshes linked OAuth tokens, pushes requests
// awaiting a Decision to operators' browsers when Web Push is configured, and keeps its policy index equal to the API
// server's.
package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"net"
	"net/http"
	"os"
	"os/signal"
	"slices"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/informer"
	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/linkage_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/mcpexec"
	"github.com/agentydragon/ducktape/agentplane/action_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	orchestrationv1 "github.com/agentydragon/ducktape/agentplane/action_service/orchestration_v1"
	"github.com/agentydragon/ducktape/agentplane/action_service/policies"
	"github.com/agentydragon/ducktape/agentplane/action_service/push"
	"github.com/agentydragon/ducktape/agentplane/action_service/sandbox"
	sessiondelegation "github.com/agentydragon/ducktape/agentplane/action_service/session_delegation"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	"github.com/agentydragon/ducktape/agentplane/action_service/web_push_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/worker"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/observability"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/agentydragon/ducktape/agentplane/postgres/probes"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadgrpc "github.com/agentydragon/ducktape/agentplane/workload_auth/grpc"
	"google.golang.org/grpc"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	migrationOnlyDatabaseURLEnv = "AGENTPLANE_ACTIONS_DATABASE_URL"
	// shutdownGrace bounds how long open calls may finish once the process is told to stop, as the Python
	// service's graceful shutdown does.
	shutdownGrace = 5 * time.Second
	// maxConnections and idleConnections size the pool as the Python service's: four kept and two more under load.
	maxConnections  = 6
	idleConnections = 4
)

// deps is who the service asks about a caller: the API server that reviews a workload's bearer, and the one whose
// policy objects and caller labels the policy index reads, which Kube reaches and where the sandbox groups run; and
// how it reaches the browsers' push services.
type deps struct {
	Reviews       auth.TokenReviews
	Kube          *rest.Config
	PushTransport http.RoundTripper
}

func inClusterDeps() (deps, error) {
	kubeConfig, err := rest.InClusterConfig()
	if err != nil {
		return deps{}, err
	}
	kubeConfig.WrapTransport = observability.WrapHTTPTransport(kubeConfig.WrapTransport)
	// Every call is a TokenReview; client-go's default of 5 requests per second (burst 10) would queue them
	// client-side. API Priority and Fairness bounds the load on the server.
	kubeConfig.QPS = -1
	kube, err := kubernetes.NewForConfig(kubeConfig)
	if err != nil {
		return deps{}, err
	}
	return deps{Reviews: kube.AuthenticationV1().TokenReviews(), Kube: kubeConfig, PushTransport: observability.HTTPTransport(http.DefaultTransport)}, nil
}

// run serves on the configured address until ctx ends, authenticating workloads with the cluster's TokenReview API
// and reading the cluster's policy objects.
func run(ctx context.Context, c config) error {
	if err := migrate.Run(ctx, c.DatabaseURL, migrations.Up); err != nil {
		return fmt.Errorf("migrate Action Service database: %w", err)
	}
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
	db, err := postgres.Open(ctx, databaseURL, maxConnections, idleConnections)
	if err != nil {
		return nil, fmt.Errorf("Action Service PostgreSQL unavailable: %w", err)
	}
	return db, nil
}

// executors routes each Action the catalog offers to the Executor of its group. An Action the catalog does not offer,
// or of a group no Executor runs, fails as unsupported, as in the Python service; one of a group that is unavailable
// waits for it to come back.
type executors struct {
	catalog *catalog.Catalog
	// byGroup holds the Executor of each group, by key.
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
// watches are failing would auto-decide from a copy it cannot vouch for, so it leaves the Service instead.
type readiness struct {
	*probes.Probes
	policy *watchhealth.Health
}

func (r readiness) Check(ctx context.Context, request *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	if request.GetService() == "" {
		if status := r.policy.Status(); !status.Available {
			slog.WarnContext(ctx, "policy index unavailable", "synced", status.Synced, "draining", status.Draining, "failing", status.Failing)
			return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_NOT_SERVING}, nil
		}
	}
	return r.Probes.Check(ctx, request)
}

// sandboxExecutors builds the Executor of each sandbox group, and gives the group the Actions it offers: they are
// declared, not discovered, so a direct tool the roster lacks is a typo found now. The offered templates are read once,
// here, so that a missing or unsafe one stops the service rather than failing each create.
func sandboxExecutors(ctx context.Context, groups *catalog.Catalog, kube *rest.Config) (map[string]worker.Executor, error) {
	byGroup := map[string]worker.Executor{}
	var exec sandbox.Exec
	var client kubernetes.Interface
	for key, group := range groups.Groups() {
		binding, isSandbox := group.Executor.(catalog.PodBinding)
		if !isSandbox {
			continue
		}
		if client == nil {
			var err error
			if client, err = kubernetes.NewForConfig(kube); err != nil {
				return nil, err
			}
			exec = sandbox.KubernetesExec(kube, client)
		}
		inventory := sandbox.NewInventory(binding, client, exec)
		if err := inventory.CheckTemplates(ctx); err != nil {
			return nil, fmt.Errorf("ActionGroup %s: %w", key, err)
		}
		executor, err := sandbox.NewExecutor(binding, inventory)
		if err != nil {
			return nil, err
		}
		if err := offer(key, group, executor.Actions()); err != nil {
			return nil, err
		}
		byGroup[key] = executor
	}
	return byGroup, nil
}

// offer gives a group the Actions its executor declares, so a direct tool the roster lacks is a typo found at startup.
func offer(key string, group *catalog.Group, actions map[string]catalog.ActionDefinition) error {
	group.Actions = actions
	var unoffered []string
	for tool := range group.DirectTools {
		if _, offered := actions[tool]; !offered {
			unoffered = append(unoffered, tool)
		}
	}
	if len(unoffered) > 0 {
		slices.Sort(unoffered)
		return fmt.Errorf("ActionGroup %s direct_tools names Actions it does not offer: %q", key, unoffered)
	}
	return nil
}

// orchestrationExecutors builds the Executor of each orchestration_v1 group, and gives the group the Actions it
// offers. They reach the API server through kube, and call the Session Dispatcher and the History Service.
func orchestrationExecutors(
	groups *catalog.Catalog, kube *rest.Config, environment environmentpb.EnvironmentServiceClient, dispatcher dispatcherpb.SessionDispatcherClient,
	history historypb.HistoryServiceClient, directWait time.Duration,
) (map[string]worker.Executor, error) {
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{sandboxapi.AddToScheme, policyapi.AddToScheme} {
		if err := add(scheme); err != nil {
			return nil, err
		}
	}
	kubeClient, err := client.New(kube, client.Options{Scheme: scheme})
	if err != nil {
		return nil, err
	}
	byGroup := map[string]worker.Executor{}
	for key, group := range groups.Groups() {
		binding, isOrchestration := group.Executor.(catalog.OrchestrationBinding)
		if !isOrchestration {
			continue
		}
		executor, err := orchestrationv1.NewExecutor(key, binding, kubeClient, environment, dispatcher, history, directWait)
		if err != nil {
			return nil, fmt.Errorf("ActionGroup %s: %w", key, err)
		}
		if err := offer(key, group, executor.Actions()); err != nil {
			return nil, err
		}
		byGroup[key] = executor
	}
	return byGroup, nil
}

// sessionDelegationExecutors builds the exact existing-environment Session operations. Unlike orchestration_v1,
// these groups do not read or write Kubernetes grants; every backend call carries the Action caller's verified UID.
func sessionDelegationExecutors(
	groups *catalog.Catalog,
	environment environmentpb.EnvironmentServiceClient,
	dispatcher dispatcherpb.SessionDispatcherClient,
	history historypb.HistoryServiceClient,
) (map[string]worker.Executor, error) {
	byGroup := map[string]worker.Executor{}
	for key, group := range groups.Groups() {
		binding, isSessionDelegation := group.Executor.(catalog.SessionDelegationBinding)
		if !isSessionDelegation {
			continue
		}
		executor, err := sessiondelegation.NewExecutor(key, binding, environment, dispatcher, history)
		if err != nil {
			return nil, fmt.Errorf("ActionGroup %s: %w", key, err)
		}
		if err := offer(key, group, executor.Actions()); err != nil {
			return nil, err
		}
		byGroup[key] = executor
	}
	return byGroup, nil
}

// dial connects to p as this service's workload.
func dial(p peer) (*grpc.ClientConn, error) {
	return bearer.Dial(p.Target, p.TokenFile, time.Duration(p.RequestTimeoutSeconds*float64(time.Second)), nil)
}

// mcpExecutors builds the Executor of each MCP group, which withdraws the group's Actions until its server lists them:
// a server that is down leaves its group unavailable, not the service stopped. linkages read the tokens of the
// OAuth-backed groups.
func mcpExecutors(groups *catalog.Catalog, linkages mcpexec.Linkages) (map[string]*mcpexec.Executor, error) {
	byGroup := map[string]*mcpexec.Executor{}
	for key, group := range groups.Groups() {
		binding, isMCP := group.Executor.(catalog.MCPBinding)
		if !isMCP {
			continue
		}
		executor, err := mcpexec.New(groups, key, binding, linkages, mcpexec.DefaultTiming())
		if err != nil {
			return nil, err
		}
		byGroup[key] = executor
	}
	return byGroup, nil
}

// webPush is the service's Web Push identity and the Notifier that delivers under it, both nil while Web Push is not
// configured.
func webPush(c config, d deps, db *sql.DB, requests *store.Store) (*push.Identity, *push.Notifier, error) {
	if !c.WebPush.Configured() {
		return nil, nil, nil
	}
	identity, err := push.NewIdentity(c.WebPush)
	if err != nil {
		return nil, nil, err
	}
	notifier, err := push.NewNotifier(db, requests, identity, c.WebPush.PublicBaseURL, d.PushTransport, push.DefaultTiming())
	return identity, notifier, err
}

// gate admits the callers of the service's RPCs: a workload calls while callers admits its ServiceAccount.
func gate(c config, d deps, callers api.Callers) (*api.Gate, error) {
	workloads, err := auth.NewResolver(d.Reviews, c.TokenAudience, c.CallerServiceAccountNamespaces)
	if err != nil {
		return nil, err
	}
	options := api.GateOptions{Workloads: workloads, Callers: callers, ReaderAccounts: c.ReaderAccounts}
	if c.operatorsEnabled() {
		if options.Operators, err = api.NewOperatorVerifier(c.OperatorOIDC); err != nil {
			return nil, err
		}
	}
	return api.NewGate(options), nil
}

// serve answers gRPC on listener, runs the dispatch worker, supervises the MCP groups' sessions, refreshes the linked
// tokens, delivers Web Push and keeps the policy index until ctx ends or the listener fails. The worker,
// the MCP groups, the refreshes and Web Push delivery drain from then on, beside the listener's shutdown, and serve
// returns once they have and the policy watches have stopped.
func serve(ctx context.Context, c config, d deps, listener net.Listener) error {
	policy := informer.NewIndex(c.CallerServiceAccountNamespaces)
	admission, err := gate(c, d, policy)
	if err != nil {
		return err
	}
	db, err := openDatabase(ctx, c.DatabaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	hub, err := wakeups.Listen(
		ctx, c.DatabaseURL, store.UpdatesChannel, store.DispatchChannel, push.UpdatesChannel, linkage.UpdatesChannel,
	)
	if err != nil {
		return fmt.Errorf("Action Service PostgreSQL listener unavailable: %w", err)
	}
	defer hub.Close()

	dispatches, unsubscribe, err := hub.Subscribe(store.DispatchChannel)
	if err != nil {
		return err
	}
	defer unsubscribe()
	linkageChanges, unsubscribeLinkages, err := hub.Subscribe(linkage.UpdatesChannel)
	if err != nil {
		return err
	}
	defer unsubscribeLinkages()
	linkages, err := linkage.New(db, c.MCPServers, c.MCPClientMetadata)
	if err != nil {
		return err
	}
	if err := linkages.RemoveUnconfigured(ctx); err != nil {
		return fmt.Errorf("removing the linkages of unconfigured MCP servers: %w", err)
	}
	byGroup, err := sandboxExecutors(ctx, c.ActionGroups, d.Kube)
	if err != nil {
		return err
	}
	// Both are set exactly when an orchestration_v1 group is configured.
	if c.SessionDispatcher != nil {
		dispatcher, err := dial(*c.SessionDispatcher)
		if err != nil {
			return err
		}
		defer dispatcher.Close()
		history, err := dial(*c.HistoryService)
		if err != nil {
			return err
		}
		defer history.Close()
		orchestrators, err := orchestrationExecutors(c.ActionGroups, d.Kube,
			environmentpb.NewEnvironmentServiceClient(dispatcher), dispatcherpb.NewSessionDispatcherClient(dispatcher), historypb.NewHistoryServiceClient(history), time.Duration(c.DirectWaitSeconds*float64(time.Second)))
		if err != nil {
			return err
		}
		maps.Copy(byGroup, orchestrators)
	}
	// The delegated Dispatcher peer is also the shared EnvironmentService listener. Its projected token and the
	// History token are read for every call, and the executor adds the durable Action caller envelope to each one.
	if c.SessionDelegationDispatcher != nil {
		dispatcherConn, err := workloadgrpc.DialDelegated(
			c.SessionDelegationDispatcher.Target, c.SessionDelegationDispatcher.TokenFile,
			time.Duration(c.SessionDelegationDispatcher.RequestTimeoutSeconds*float64(time.Second)), nil,
		)
		if err != nil {
			return fmt.Errorf("dial delegated Environment Service and Session Dispatcher: %w", err)
		}
		defer dispatcherConn.Close()
		historyConn, err := workloadgrpc.DialDelegated(
			c.SessionDelegationHistory.Target, c.SessionDelegationHistory.TokenFile,
			time.Duration(c.SessionDelegationHistory.RequestTimeoutSeconds*float64(time.Second)), nil,
		)
		if err != nil {
			return fmt.Errorf("dial delegated History Service: %w", err)
		}
		defer historyConn.Close()
		delegated, err := sessionDelegationExecutors(
			c.ActionGroups,
			environmentpb.NewEnvironmentServiceClient(dispatcherConn),
			dispatcherpb.NewSessionDispatcherClient(dispatcherConn),
			historypb.NewHistoryServiceClient(historyConn),
		)
		if err != nil {
			return err
		}
		maps.Copy(byGroup, delegated)
	}
	requests := store.New(db)
	supervisors, err := mcpExecutors(c.ActionGroups, linkages)
	if err != nil {
		return err
	}
	for key, executor := range supervisors {
		byGroup[key] = executor
	}
	pushIdentity, notifier, err := webPush(c, d, db, requests)
	if err != nil {
		return err
	}
	// pushChanges wakes the notifier on each commit to a request.
	var pushChanges <-chan struct{}
	if notifier != nil {
		var unsubscribe func()
		if pushChanges, unsubscribe, err = hub.Subscribe(store.UpdatesChannel); err != nil {
			return err
		}
		defer unsubscribe()
	}
	actions := executors{catalog: c.ActionGroups, byGroup: byGroup}
	dispatcher, err := worker.New(requests, actions, worker.DefaultTiming())
	if err != nil {
		return err
	}
	visibility, err := policies.NewVisibility(
		c.GitHubVisibility.APIBaseURL, time.Duration(c.GitHubVisibility.CacheTTLSeconds*float64(time.Second)),
	)
	if err != nil {
		return err
	}

	policyInformer, err := informer.New(d.Kube, policy, c.PolicyNamespace)
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
		observability.GRPCServerOption(),
		grpc.ChainUnaryInterceptor(admission.UnaryInterceptor), grpc.ChainStreamInterceptor(admission.StreamInterceptor),
	)
	healthpb.RegisterHealthServer(grpcServer, readiness{Probes: health, policy: policy.Health})
	actionAPI := api.NewService(requests, hub, c.ActionGroups, admission, policy, api.Submissions{
		Executors: actions, Provider: policies.NewProvider(visibility),
	})
	action_servicepb.RegisterActionServiceServer(grpcServer, actionAPI)
	operator_action_servicepb.RegisterOperatorActionServiceServer(grpcServer, api.NewOperatorService(requests, hub, admission, linkages))
	// The callback is public: its single-use state, not a bearer, authorizes completion.
	linkage_servicepb.RegisterLinkageServiceServer(grpcServer, linkage.NewService(linkages))
	web_push_servicepb.RegisterWebPushServiceServer(grpcServer, api.NewPushService(push.NewSubscriptions(db), pushIdentity, hub, admission))
	working, drain := context.WithCancel(ctx)
	var supervising sync.WaitGroup
	for _, executor := range supervisors {
		supervising.Go(func() { executor.Run(working) })
	}
	supervising.Go(func() { linkages.Run(working) })
	// A commit to a linkage, on any replica, may make an OAuth-backed group usable or unusable.
	supervising.Go(func() {
		for {
			select {
			case <-working.Done():
				return
			case <-linkageChanges:
				for _, executor := range supervisors {
					executor.LinkageChanged()
				}
			}
		}
	})
	if notifier != nil {
		supervising.Go(func() { notifier.Run(working, pushChanges) })
	}
	// The worker reads the pending dispatches again on a dispatch and whenever a group changes, which can make its
	// Executor ready.
	wakeups := make(chan struct{}, 1)
	go func() {
		changed := c.ActionGroups.Changed()
		for {
			select {
			case <-working.Done():
				return
			case <-dispatches:
			case <-changed:
				changed = c.ActionGroups.Changed()
			}
			select {
			case wakeups <- struct{}{}:
			default:
			}
		}
	}()
	worked := make(chan struct{})
	go func() {
		defer close(worked)
		dispatcher.Run(working, wakeups)
	}()
	failed := make(chan error, 1)
	go func() { failed <- fmt.Errorf("gRPC listener: %w", grpcServer.Serve(listener)) }()
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
	graceful.Stop(stopped, grpcServer)
	<-worked
	supervising.Wait()
	return result
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	handled, err := migrate.RunOnly(ctx, os.Args[1:], migrationOnlyDatabaseURLEnv, migrations.Up)
	if err != nil {
		slog.Error("Action Service migration failed", "error", err)
		os.Exit(1)
	}
	if handled {
		return
	}
	c, err := loadConfig()
	if err != nil {
		slog.Error("invalid Action Service configuration", "error", err)
		os.Exit(2)
	}
	shutdownTracing, err := observability.Init(ctx, "action-service")
	if err != nil {
		slog.Error("Action Service tracing setup failed", "error", err)
		os.Exit(1)
	}
	err = run(ctx, c)
	shutdownTracing()
	if err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("Action Service stopped", "error", err)
		os.Exit(1)
	}
}
