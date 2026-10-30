// Command notification-service serves subscriptions and Session inboxes, works them into runner notices, and
// takes GitHub's webhooks.
package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"maps"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	actionpb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/actions"
	"github.com/agentydragon/ducktape/agentplane/notification_service/api"
	"github.com/agentydragon/ducktape/agentplane/notification_service/docs"
	"github.com/agentydragon/ducktape/agentplane/notification_service/github"
	githubclient "github.com/agentydragon/ducktape/agentplane/notification_service/github/client" // Distinguish GitHub transport from the Kubernetes client.
	"github.com/agentydragon/ducktape/agentplane/notification_service/inboxaccess"
	operatorpb "github.com/agentydragon/ducktape/agentplane/notification_service/notifications_operatorpb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/webhook"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres/go"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/probes"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"

	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	// shutdownGrace bounds how long open calls may finish once the process is told to stop.
	shutdownGrace = 5 * time.Second
	// readHeaderTimeout bounds a request's headers. There is no other server timeout: the operator's watches
	// stream for their lease, and the webhook handler bounds itself.
	readHeaderTimeout = 10 * time.Second
	// workers is how many goroutines claim inboxes.
	workers = 4
	// databaseConnections bounds the pool the workers, the servicers and the webhook handler share.
	databaseConnections = 16
	// historyReceiveBytes admits a retained Codex thread/resume entry, observed at about 14 MiB.
	historyReceiveBytes = 32 * 1024 * 1024
)

// kubeDeps is what the service reads from Kubernetes: whom a bearer proves, the Sandboxes of its namespace, and
// the API server its send grants are watched on.
type kubeDeps struct {
	Reviews   auth.TokenReviews
	Sandboxes client.Reader
	Policies  *rest.Config
}

func inClusterDeps() (kubeDeps, error) {
	kubeConfig, err := rest.InClusterConfig()
	if err != nil {
		return kubeDeps{}, err
	}
	kubeConfig.WrapTransport = observability.WrapHTTPTransport(kubeConfig.WrapTransport)
	// Every call is a TokenReview; client-go's default of 5 requests per second (burst 10) would queue them
	// client-side. API Priority and Fairness bounds the load on the server.
	kubeConfig.QPS = -1
	kube, err := kubernetes.NewForConfig(kubeConfig)
	if err != nil {
		return kubeDeps{}, err
	}
	scheme := runtime.NewScheme()
	if err := sandboxapi.AddToScheme(scheme); err != nil {
		return kubeDeps{}, err
	}
	sandboxes, err := client.New(kubeConfig, client.Options{Scheme: scheme})
	if err != nil {
		return kubeDeps{}, err
	}
	return kubeDeps{Reviews: kube.AuthenticationV1().TokenReviews(), Sandboxes: sandboxes, Policies: kubeConfig}, nil
}

// run serves on the configured address, authenticating callers with the cluster's TokenReview API.
func run(ctx context.Context, c config) error {
	deps, err := inClusterDeps()
	if err != nil {
		return err
	}
	listener, err := net.Listen("tcp", net.JoinHostPort(c.Host, strconv.Itoa(c.Port)))
	if err != nil {
		return err
	}
	defer listener.Close()
	return serve(ctx, c, deps, listener)
}

func openDatabase(ctx context.Context, databaseURL string) (*sql.DB, error) {
	db, err := postgres.Open(ctx, databaseURL, databaseConnections, databaseConnections/2)
	if err != nil {
		return nil, fmt.Errorf("Notification Service PostgreSQL unavailable: %w", err)
	}
	return db, nil
}

// newRoutes is the listener's HTTP routes: the HTTP form of `Notifications` under /v1/ (served by agentFacade), the
// webhook when GitHub is configured, and the agent documentation, which needs neither the database nor a peer.
func newRoutes(agentFacade, webhookHandler http.Handler) (http.Handler, error) {
	agentDocs, err := fs.Sub(docs.Agents, "agents")
	if err != nil {
		return nil, err
	}
	routes := http.NewServeMux()
	routes.Handle("/v1/", agentFacade)
	if webhookHandler != nil {
		routes.Handle(webhook.Pattern, webhookHandler)
	}
	facade.MountAgentDocs(routes, agentDocs, docs.OpenAPI)
	return routes, nil
}

// serve answers on listener until ctx ends or a worker or the listener fails. gRPC and the HTTP routes share the
// listener: the servicers are gRPC, and the HTTP form of `Notifications`, its documentation and the webhook are
// plain HTTP.
func serve(ctx context.Context, c config, deps kubeDeps, listener net.Listener) error {
	resolver, err := auth.NewResolver(deps.Reviews, c.TokenAudience, []string{c.Namespace})
	if err != nil {
		return err
	}
	sandboxes, err := sandboxreader.New(c.Namespace, deps.Sandboxes)
	if err != nil {
		return err
	}
	db, err := openDatabase(ctx, c.DatabaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	hub, err := wakeups.Listen(ctx, c.DatabaseURL, store.WorkChannel)
	if err != nil {
		return fmt.Errorf("Notification Service PostgreSQL listener unavailable: %w", err)
	}
	defer hub.Close()
	// Neither connection dials until its first call, so a peer that is down does not stop the service starting.
	historyConnection, err := bearer.Dial(c.HistoryService.Target, c.HistoryService.TokenFile, seconds(c.HistoryService.RequestTimeoutSeconds),
		grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(historyReceiveBytes)))
	if err != nil {
		return err
	}
	defer historyConnection.Close()
	dispatcherConnection, err := bearer.Dial(c.SessionDispatcher.Target, c.SessionDispatcher.TokenFile, seconds(c.SessionDispatcher.RequestTimeoutSeconds))
	if err != nil {
		return err
	}
	defer dispatcherConnection.Close()
	actionEndpoint, err := c.Actions.grpcEndpoint()
	if err != nil {
		return err
	}
	var actionDialOptions []grpc.DialOption
	if actionEndpoint.tlsServerName != "" {
		actionDialOptions = append(actionDialOptions,
			grpc.WithTransportCredentials(credentials.NewClientTLSFromCert(nil, actionEndpoint.tlsServerName)))
	}
	actionsConnection, err := bearer.Dial(actionEndpoint.target, c.Actions.TokenFile, seconds(c.Actions.RequestTimeoutSeconds), actionDialOptions...)
	if err != nil {
		return err
	}
	defer actionsConnection.Close()
	sendPolicy := inboxaccess.NewPolicy()
	policyWatches, err := sendPolicy.Watch(deps.Policies, c.Namespace)
	if err != nil {
		return err
	}
	// Until the watches have listed, every send is refused as unavailable; nothing else waits for them.
	watchContext, stopWatching := context.WithCancel(ctx)
	watching := make(chan struct{})
	go func() {
		defer close(watching)
		policyWatches.Run(watchContext)
	}()
	defer func() {
		stopWatching()
		<-watching
	}()
	history := historypb.NewHistoryServiceClient(historyConnection)

	inboxes := store.New(db, c.Quotas)
	actionsSource := &actions.Source{Service: actionpb.NewActionServiceClient(actionsConnection), Store: inboxes}
	// The providers are interfaces, so a disabled GitHub is a nil interface, not a nil *github.Provider.
	var githubAPI api.GitHub
	var githubSource worker.Provider
	var webhookHandler http.Handler
	if c.githubEnabled() {
		githubClient, err := githubclient.New(c.GitHub)
		if err != nil {
			return err
		}
		provider := github.NewProvider(githubClient, inboxes, c.GitHub)
		githubAPI, githubSource = provider, provider
		webhookHandler = webhook.New(c.GitHub, inboxes)
	}

	options := api.Options{
		Store: inboxes, Wakeups: hub, Resolver: resolver, Namespace: c.Namespace, Sandboxes: sandboxes,
		Runner: &api.ProxyRunner{
			TokenFile: c.RunnerGateway.TokenFile, Timeout: seconds(c.RunnerGateway.RequestTimeoutSeconds),
			TargetTemplate: c.RunnerGateway.ControlTarget,
		},
		// The Action Service source authorizes a subscription the same way it reads one.
		Actions: actionsSource, GitHub: githubAPI, SendPolicy: sendPolicy, History: history,
		MessageBodyLimit: c.MessageBodyLimitBytes, OperatorReader: c.OperatorReaderAccount,
		Debounce:       c.NoticeDebounce.Debounce(),
		RequestTimeout: seconds(c.RequestTimeoutSeconds), WatchLease: seconds(c.WatchLeaseSeconds),
	}
	operator := api.NewNotificationsOperator(options)
	// A worker that stops ends the process, so a ready replica's workers are running.
	health := probes.New(db, hub)
	methods := auth.HealthMethods()
	// A watch carries no deadline of its own, so the request timeout bounds its admission.
	maps.Copy(methods, api.Methods(options))
	grpcServer := grpc.NewServer(append(methods.ServerOptions(options.RequestTimeout), observability.GRPCServerOption())...)
	notificationspb.RegisterNotificationsServer(grpcServer, api.NewNotifications(options))
	operatorpb.RegisterNotificationsOperatorServer(grpcServer, operator)
	healthpb.RegisterHealthServer(grpcServer, health)
	// The facade is the listener's own client: its gRPC calls come back to the gRPC server mounted on it, so the
	// servicers authenticate every request. It outlives the termination signal, so that calls admitted before it
	// finish while draining.
	facadeContext, stopFacade := context.WithCancel(context.WithoutCancel(ctx))
	defer stopFacade()
	agentFacade, err := facade.NewHandler(facadeContext, listener.Addr().String(), 0, notificationspb.RegisterNotificationsHandlerFromEndpoint)
	if err != nil {
		return err
	}
	routes, err := newRoutes(agentFacade, webhookHandler)
	if err != nil {
		return err
	}
	server := &http.Server{Handler: observability.HTTPHandler(routes), ReadHeaderTimeout: readHeaderTimeout}
	facade.MountGRPC(server, grpcServer)

	// Each worker follows the commits on its own signal.
	signals := make([]<-chan struct{}, workers)
	for i := range signals {
		changes, unsubscribe, err := hub.Subscribe(store.WorkChannel)
		if err != nil {
			return err
		}
		defer unsubscribe()
		signals[i] = changes
	}
	inboxWorker := &worker.Worker{
		Store: inboxes, Namespace: c.Namespace, Sandboxes: sandboxes,
		History:    history,
		Dispatcher: dispatcherpb.NewSessionDispatcherClient(dispatcherConnection),
		Actions:    actionsSource, GitHub: githubSource, Settings: c.Settings,
	}
	failed := make(chan error, workers+1)
	// The workers outlive the listener, so a claim in progress is released before the database closes: the
	// deferred cancel ends them, and the wait after it returns once they have released their claims.
	workContext, stopWorkers := context.WithCancel(context.WithoutCancel(ctx))
	running := runWorkers(workContext, workers, failed, func(ctx context.Context, index int) error {
		return inboxWorker.Run(ctx, signals[index])
	})
	defer running.Wait()
	defer stopWorkers()
	go func() { failed <- fmt.Errorf("listener: %w", server.Serve(listener)) }()

	var result error
	select {
	case result = <-failed:
	case <-ctx.Done():
		result = ctx.Err()
	}
	health.Drain()
	// Open watches end with reconnect_required rather than holding the shutdown until their leases.
	operator.Drain()
	stopped, cancel := context.WithTimeout(context.WithoutCancel(ctx), shutdownGrace)
	defer cancel()
	if err := server.Shutdown(stopped); err != nil {
		server.Close()
	}
	return result
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	c, err := loadConfig()
	if err != nil {
		slog.Error("invalid Notification Service configuration", "error", err)
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	shutdownTracing, err := observability.Init(ctx, "notification-service")
	if err != nil {
		slog.Error("Notification Service tracing setup failed", "error", err)
		os.Exit(1)
	}
	err = run(ctx, c)
	shutdownTracing()
	if err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("Notification Service stopped", "error", err)
		os.Exit(1)
	}
}
