package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	actionpb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/operator_action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/web_push_servicepb"
	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/bridge"
	"github.com/agentydragon/ducktape/agentplane/app/go/federation"
	"github.com/agentydragon/ducktape/agentplane/app/go/historyclient"
	"github.com/agentydragon/ducktape/agentplane/app/go/historydata"
	"github.com/agentydragon/ducktape/agentplane/app/go/historywatch"
	"github.com/agentydragon/ducktape/agentplane/app/go/liveinventory"
	"github.com/agentydragon/ducktape/agentplane/app/go/policyaccess"
	"github.com/agentydragon/ducktape/agentplane/app/go/proxy"
	"github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/app/go/status"
	"github.com/agentydragon/ducktape/agentplane/app/go/threadcommands"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notifications_operatorpb"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/util/netlistener"
	"github.com/google/uuid"
	_ "github.com/lib/pq"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const systemStatusTimeout = 5 * time.Second

// run owns process dependencies and their shutdown. Each API call continues to use the
// repository's generated Kubernetes, gRPC, and protobuf APIs; the process adds only composition.
func run(ctx context.Context, config Config) error {
	logger := slog.Default()
	shutdownTracing, err := observability.Init(ctx, "agentplane-app")
	if err != nil {
		return fmt.Errorf("configure OpenTelemetry tracing: %w", err)
	}
	defer shutdownTracing()

	databaseURL := postgresDriverURL(config.DatabaseURL)
	db, err := sql.Open("postgres", databaseURL)
	if err != nil {
		return fmt.Errorf("open app PostgreSQL database: %w", err)
	}
	defer db.Close()
	db.SetMaxOpenConns(16)
	db.SetMaxIdleConns(8)
	db.SetConnMaxLifetime(30 * time.Minute)
	if err := db.PingContext(ctx); err != nil {
		return fmt.Errorf("app PostgreSQL unavailable: %w", err)
	}

	wakeupHub, err := wakeups.Listen(ctx, databaseURL, threads.UpdatesChannel, auth.SessionUpdatesChannel)
	if err != nil {
		return fmt.Errorf("start PostgreSQL update listener: %w", err)
	}
	defer wakeupHub.Close()

	kubeConfig, err := kubernetesConfig(config.Kubeconfig)
	if err != nil {
		return err
	}
	kubeConfig.WrapTransport = observability.WrapHTTPTransport(kubeConfig.WrapTransport)
	kube, err := kubernetes.NewForConfig(kubeConfig)
	if err != nil {
		return fmt.Errorf("create Kubernetes API clients: %w", err)
	}
	scheme := runtime.NewScheme()
	for _, addToScheme := range []func(*runtime.Scheme) error{
		clientgoscheme.AddToScheme, sandboxapi.AddToScheme, v1alpha1.AddToScheme, egressapi.AddToScheme,
	} {
		if err := addToScheme(scheme); err != nil {
			return fmt.Errorf("register generated Kubernetes API types: %w", err)
		}
	}
	objects, err := client.New(kubeConfig, client.Options{Scheme: scheme})
	if err != nil {
		return fmt.Errorf("create typed Kubernetes client: %w", err)
	}

	baseClient := &http.Client{Timeout: 10 * time.Second, Transport: observability.HTTPTransport(http.DefaultTransport)}
	actionCallbackHTTP, err := proxy.NewUpstreamProxy(baseClient, config.ActionFederation.ServiceURL)
	if err != nil {
		return fmt.Errorf("configure Action Service callback HTTP client: %w", err)
	}
	connectionTarget, err := grpcTargetFromHTTPURL(config.ConnectionServiceURL)
	if err != nil {
		return fmt.Errorf("configure Connection Service gRPC client: %w", err)
	}

	newHistory := func() (*historyclient.Client, error) {
		return historyclient.New(historyclient.Config{
			Target: config.HistoryServiceTarget, TokenFile: config.HistoryServiceTokenFile,
			Timeout: time.Duration(config.HistoryServiceRequestTimeoutSeconds * float64(time.Second)),
		})
	}
	historyReaderClient, err := newHistory()
	if err != nil {
		return fmt.Errorf("configure History Service reader: %w", err)
	}
	defer historyReaderClient.Close()
	historyWatchClient, err := newHistory()
	if err != nil {
		return fmt.Errorf("configure History Service watcher: %w", err)
	}
	defer historyWatchClient.Close()
	sessionOpeningClient, err := newHistory()
	if err != nil {
		return fmt.Errorf("configure Session openings watcher: %w", err)
	}
	defer sessionOpeningClient.Close()

	history, err := historydata.New(historyReaderClient)
	if err != nil {
		return err
	}
	threadStore := threads.NewStore(db)
	inventory, err := sandboxes.New(config.SandboxNamespace, objects, config.DefaultPolicySets,
		&sandboxespb.ServiceAccount{Namespace: config.SandboxNamespace, Name: "app"})
	if err != nil {
		return fmt.Errorf("configure Sandbox inventory: %w", err)
	}
	policies := policyaccess.New(config.SandboxNamespace, objects)
	liveIndex, err := liveinventory.New(kubeConfig, config.SandboxNamespace)
	if err != nil {
		return fmt.Errorf("configure live Kubernetes inventory: %w", err)
	}
	runner, err := bridge.NewGatewayRunnerProxy(bridge.RunnerConfig{
		Namespace: config.SandboxNamespace, TokenFile: config.RunnerGatewayTokenFile,
		Timeout:        time.Duration(config.RunnerGatewayRequestTimeoutSeconds * float64(time.Second)),
		TargetTemplate: config.RunnerGatewayControlTarget,
	})
	if err != nil {
		return fmt.Errorf("configure Runner Gateway: %w", err)
	}
	dispatcher, err := bridge.NewDispatcherClient(bridge.DispatcherConfig{
		Target: config.SessionDispatcherTarget, TokenFile: config.SessionDispatcherTokenFile,
		Timeout: time.Duration(config.SessionDispatcherRequestTimeoutSecs * float64(time.Second)),
	})
	if err != nil {
		return fmt.Errorf("configure Session Dispatcher: %w", err)
	}
	readiness := threadReadiness{threads: threadStore, history: history}
	appBridge, err := bridge.New(inventory, runner, dispatcher, readiness)
	if err != nil {
		return fmt.Errorf("configure Runner bridge: %w", err)
	}

	oidc, err := auth.LoadOIDCFromEnv()
	if err != nil {
		return err
	}
	var federator *federation.Client
	if oidc != nil {
		tokenEndpoint := ""
		if config.ActionFederation.TokenEndpoint != nil {
			tokenEndpoint = *config.ActionFederation.TokenEndpoint
		}
		federator, err = federation.New(federation.Config{
			Mode: config.ActionFederation.Mode, ServiceURL: config.ActionFederation.ServiceURL,
			LoginJWKSURI:      config.ActionFederation.LoginJWKsURI,
			LoginTokenProfile: config.ActionFederation.LoginTokenProfile,
			Target: federation.ProviderConfig{
				Issuer: config.ActionFederation.Target.Issuer, Audience: config.ActionFederation.Target.Audience,
				JWKSURI: config.ActionFederation.Target.JWKsURI, TokenProfile: config.ActionFederation.Target.TokenProfile,
			},
			Scope: config.ActionFederation.Scope, TokenEndpoint: tokenEndpoint, HTTPClient: baseClient,
		}, *oidc)
		if err != nil {
			return fmt.Errorf("configure Action Service federation: %w", err)
		}
	}
	appAuth, err := auth.NewWithContext(ctx, auth.Config{
		OIDC: oidc, Database: db, TokenReviews: kube.AuthenticationV1().TokenReviews(),
		TokenAudience: config.TokenAudience, TokenSubjects: config.TokenSubjects,
		StoreFederationTokens: federator != nil, SessionChanges: wakeupHub,
		HTTPClient: baseClient,
	})
	if err != nil {
		return fmt.Errorf("configure app authentication: %w", err)
	}

	historyWatchClientAdapter := historyWatchAdapter{historyWatchClient}
	historyWatch, err := historywatch.NewHistoryWatch(historyWatchClientAdapter, func() {
		if _, notifyErr := db.ExecContext(ctx, `SELECT pg_notify($1, '')`, threads.UpdatesChannel); notifyErr != nil {
			logger.WarnContext(ctx, "publish History update wakeup", "error", notifyErr)
		}
	}, historywatch.Options{RetryInterval: time.Second})
	if err != nil {
		return fmt.Errorf("configure History summary watch: %w", err)
	}
	sessionOpeningClientAdapter := historyWatchAdapter{sessionOpeningClient}
	sessionOpenings, err := historywatch.NewSessionOpenings(sessionOpeningClientAdapter, threadStore,
		historywatch.Options{RetryInterval: time.Second})
	if err != nil {
		return fmt.Errorf("configure Session openings worker: %w", err)
	}

	actionTarget, err := grpcTargetFromHTTPURL(config.ActionFederation.ServiceURL)
	if err != nil {
		return fmt.Errorf("configure Action Service gRPC client: %w", err)
	}
	actionConn, err := grpc.NewClient(actionTarget,
		observability.GRPCClientOption(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return fmt.Errorf("configure Action Service gRPC client: %w", err)
	}
	defer actionConn.Close()
	actionClient := actionpb.NewActionServiceClient(actionConn)
	facadeContext, stopFacades := context.WithCancel(context.WithoutCancel(ctx))
	defer stopFacades()
	actionFacade, err := facade.NewHandler(
		facadeContext, actionTarget, 10*time.Second,
		actionpb.RegisterActionServiceHandlerFromEndpoint,
		operator_action_servicepb.RegisterOperatorActionServiceHandlerFromEndpoint,
		web_push_servicepb.RegisterWebPushServiceHandlerFromEndpoint,
	)
	if err != nil {
		return fmt.Errorf("configure generated Action Service HTTP facade: %w", err)
	}
	actionGateway := proxy.NewHandlerUpstream(actionFacade, "")
	connectionFacade, err := facade.NewHandler(facadeContext, connectionTarget, 10*time.Second, connection_servicepb.RegisterConnectionServiceHandlerFromEndpoint)
	if err != nil {
		return fmt.Errorf("configure generated Connection Service HTTP facade: %w", err)
	}
	connectionGateway := proxy.NewHandlerUpstream(connectionFacade, "")
	dispatcherFacade, err := facade.NewHandler(
		facadeContext, config.SessionDispatcherTarget,
		time.Duration(config.SessionDispatcherRequestTimeoutSecs*float64(time.Second)),
		session_dispatcherpb.RegisterSessionDispatcherHandlerFromEndpoint,
	)
	if err != nil {
		return fmt.Errorf("configure generated Session Dispatcher HTTP facade: %w", err)
	}
	dispatcherGateway := proxy.NewHandlerUpstream(dispatcherFacade, config.SessionDispatcherTokenFile)
	notificationTarget, err := grpcTargetFromHTTPURL(config.NotificationsURL)
	if err != nil {
		return fmt.Errorf("configure Notification Service gRPC client: %w", err)
	}
	notificationConn, err := bearer.Dial(notificationTarget, config.NotificationsTokenFile,
		time.Duration(config.NotificationsRequestTimeoutSeconds*float64(time.Second)))
	if err != nil {
		return fmt.Errorf("configure Notification Service gRPC client: %w", err)
	}
	defer notificationConn.Close()
	egressTarget, err := grpcTargetFromHTTPURL(config.EgressAdminURL)
	if err != nil {
		return fmt.Errorf("configure Egress Service gRPC client: %w", err)
	}
	egressConn, err := bearer.Dial(egressTarget, config.EgressAdminTokenFile,
		time.Duration(config.EgressAdminTimeoutSeconds*float64(time.Second)))
	if err != nil {
		return fmt.Errorf("configure Egress Service gRPC client: %w", err)
	}
	defer egressConn.Close()
	consoleConnector, err := sandboxes.NewConsoleConnector(kubeConfig)
	if err != nil {
		return fmt.Errorf("configure Sandbox console client: %w", err)
	}
	serviceStatus, healthConnections, err := newSystemStatus(config, baseClient)
	if err != nil {
		return err
	}
	defer closeConnections(healthConnections)

	server := NewHTTPServer(appAuth)
	RegisterCoreRoutes(server, config)
	RegisterSandboxRoutes(server, config, inventory)
	RegisterPolicyRoutes(server, config, inventory, policies)
	RegisterSessionRoutes(server, config, inventory, appBridge, threadStore)
	RegisterRunnerRoutes(server, appBridge)
	RegisterThreadReadRoutes(server, threadStore, history, inventory, appBridge)
	if err := threadcommands.RegisterRoutes(server.ProtectedAPI(), threadcommands.Dependencies{
		Threads: threadStore, History: history, Readiness: appBridge, Dispatcher: dispatcher,
		Models: config.Models.Harnesses,
	}); err != nil {
		return fmt.Errorf("register Thread command routes: %w", err)
	}
	RegisterOperatorRoutes(server, policies, federator, actionClient, actionCallbackHTTP, config.ActionFederation.ServiceURL)
	RegisterStatusRoutes(server, serviceStatus)
	server.RegisterRawRoute("POST /telemetry/v1/traces", traceRelayHandler(), true)
	RegisterStreamingRoutes(server, StreamingDependencies{
		Auth: appAuth, Updates: wakeupHub, UpdatesConnected: wakeupHub.Connected,
		Live: liveIndex, Sandboxes: inventory, Threads: threadStore, History: history, HistoryFollower: historyReaderClient,
		ActionPolicy: actionClient, Federator: federator,
		ActionServiceURL: config.ActionFederation.ServiceURL,
		DispatcherHTTP:   dispatcherGateway, ActionHTTP: actionGateway, ConnectionHTTP: connectionGateway,
		Notifications:    notifications_operatorpb.NewNotificationsOperatorClient(notificationConn),
		NotificationWait: time.Duration(config.NotificationsRequestTimeoutSeconds * float64(time.Second)),
		Egress:           egress_adminpb.NewEgressAdminClient(egressConn),
		EgressWait:       time.Duration(config.EgressAdminTimeoutSeconds * float64(time.Second)),
		Console:          consoleConnector, Logger: logger,
	})
	if config.ElectricURL != "" {
		electric, electricErr := proxy.NewElectricProxy(baseClient, config.ElectricURL,
			threadScopeStore{threads: threadStore, history: history, changes: wakeupHub}, 20*time.Second)
		if electricErr != nil {
			return fmt.Errorf("configure Electric thread sync: %w", electricErr)
		}
		electricHandler := electric.Handler()
		server.RegisterRawRoute("/threads/{thread_id}/sync/{rest...}", electricHandler, false)
	}
	workers := []processWorker{
		{name: "live-inventory", run: liveIndex.Run},
		{name: "history-watch", run: historyWatch.Run},
		{name: "session-openings", run: sessionOpenings.Run},
	}
	return serve(ctx, config, server, appAuth, workers)
}

type threadReadiness struct {
	threads *threads.Store
	history *historydata.Store
}

// historyWatchAdapter narrows the generated streaming client to the local worker interface.
// Go method return types are invariant, so this small wrapper preserves the generated stream
// while allowing the worker package to depend on its minimal Recv surface.
type historyWatchAdapter struct{ client *historyclient.Client }

func (c historyWatchAdapter) WatchSessionSummaries(
	ctx context.Context,
	request *historypb.WatchSessionSummariesRequest,
) (historywatch.Stream, error) {
	return c.client.WatchSessionSummaries(ctx, request)
}

func (c historyWatchAdapter) Redial() { c.client.Redial() }

type threadScopeStore struct {
	threads *threads.Store
	history *historydata.Store
	changes auth.ChangeSource
}

func (s threadScopeStore) ThreadExists(ctx context.Context, rawID string) (bool, error) {
	id, err := uuid.Parse(rawID)
	if err != nil || id.String() != rawID {
		return false, nil
	}
	thread, err := s.threads.GetThread(ctx, id)
	return thread != nil, err
}

func (s threadScopeStore) CurrentScope(ctx context.Context, rawID string) (proxy.ThreadScope, bool, error) {
	id, err := uuid.Parse(rawID)
	if err != nil || id.String() != rawID {
		return proxy.ThreadScope{}, false, nil
	}
	scope, err := s.history.CurrentScope(ctx, id)
	if err != nil || scope == nil {
		return proxy.ThreadScope{}, false, err
	}
	return proxy.ThreadScope{ProjectionEpoch: scope.ProjectionEpoch, ThroughCursor: strconv.FormatUint(scope.ThroughCursor, 10)}, true, nil
}

func (s threadScopeStore) SubscribeThreadChanges(_ context.Context, _ string) (<-chan struct{}, func()) {
	signal, unsubscribe, err := s.changes.Subscribe(threads.UpdatesChannel)
	if err != nil {
		closed := make(chan struct{})
		close(closed)
		return closed, func() {}
	}
	return signal, unsubscribe
}

func (r threadReadiness) Exists(ctx context.Context, id uuid.UUID) (bool, error) {
	thread, err := r.threads.GetThread(ctx, id)
	return thread != nil, err
}

func (r threadReadiness) FeedState(ctx context.Context, id uuid.UUID) (*bridge.FeedSnapshot, error) {
	summary, err := r.history.GetSummary(ctx, id)
	if err != nil || summary.Feed == nil || summary.Feed.Status != historypb.FeedStatus_FEED_STATUS_FAILED {
		return nil, err
	}
	message := ""
	if summary.Feed.End != nil {
		message = summary.Feed.End.Message
	}
	return &bridge.FeedSnapshot{End: &bridge.FeedError{Message: message}}, nil
}

func postgresDriverURL(value string) string {
	return strings.Replace(value, "postgresql+asyncpg://", "postgres://", 1)
}

func kubernetesConfig(path string) (*rest.Config, error) {
	if path != "" {
		config, err := clientcmd.BuildConfigFromFlags("", path)
		if err != nil {
			return nil, fmt.Errorf("load Kubernetes config: %w", err)
		}
		return config, nil
	}
	config, err := rest.InClusterConfig()
	if err != nil {
		return nil, fmt.Errorf("load in-cluster Kubernetes config: %w", err)
	}
	return config, nil
}

func newSystemStatus(config Config, httpClient *http.Client) (*status.Checker, []*grpc.ClientConn, error) {
	probes := make([]status.Probe, 0, 7)
	connections := make([]*grpc.ClientConn, 0, 5)
	dialHealth := func(name, target string) error {
		connection, err := grpc.NewClient(target,
			observability.GRPCClientOption(), grpc.WithTransportCredentials(insecure.NewCredentials()))
		if err != nil {
			return fmt.Errorf("configure %s health client: %w", name, err)
		}
		connections = append(connections, connection)
		probes = append(probes, status.GRPCHealthProbe(name, healthpb.NewHealthClient(connection)))
		return nil
	}
	for _, endpoint := range []struct{ name, target string }{
		{"Runner Gateway", config.RunnerGatewayTarget}, {"History Service", config.HistoryServiceTarget},
	} {
		if err := dialHealth(endpoint.name, endpoint.target); err != nil {
			closeConnections(connections)
			return nil, nil, err
		}
	}
	for _, endpoint := range []struct{ name, raw string }{
		{"Action Service", config.ActionFederation.ServiceURL},
		{"Connection Service", config.ConnectionServiceURL},
		{"Notification Service", config.NotificationsURL},
		{"Egress proxy", config.EgressAdminURL},
	} {
		target, err := grpcTargetFromHTTPURL(endpoint.raw)
		if err != nil {
			closeConnections(connections)
			return nil, nil, fmt.Errorf("configure %s health target: %w", endpoint.name, err)
		}
		if err := dialHealth(endpoint.name, target); err != nil {
			closeConnections(connections)
			return nil, nil, err
		}
	}
	if config.ElectricURL != "" {
		probes = append(probes, status.HTTPHealthProbe("Electric", httpClient, healthURL(config.ElectricURL, "/v1/health")))
	}
	checker, err := status.New(probes, systemStatusTimeout)
	if err != nil {
		closeConnections(connections)
		return nil, nil, err
	}
	return checker, connections, nil
}

func grpcTargetFromHTTPURL(raw string) (string, error) {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return "", fmt.Errorf("expected an absolute HTTP(S) service URL")
	}
	port := parsed.Port()
	if port == "" {
		if parsed.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	return net.JoinHostPort(parsed.Hostname(), port), nil
}

func healthURL(raw, path string) string {
	parsed, _ := url.Parse(raw)
	parsed.Path = path
	parsed.RawPath = ""
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed.String()
}

type processWorker struct {
	name string
	run  func(context.Context) error
}

func serve(ctx context.Context, config Config, app *HTTPServer, appAuth *auth.Auth, workers []processWorker) error {
	listener, err := netlistener.Open(config.ListenAddress)
	if err != nil {
		return fmt.Errorf("listen for app HTTP: %w", err)
	}
	defer listener.Close()

	appCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	httpServer := &http.Server{
		Handler: appHTTPHandler(app.Handler(appAuth)), ReadHeaderTimeout: 10 * time.Second,
		BaseContext: func(net.Listener) context.Context { return appCtx },
	}
	workerErrors := make(chan error, len(workers)+1)
	var group sync.WaitGroup
	for _, worker := range workers {
		worker := worker
		group.Add(1)
		go func() {
			defer group.Done()
			err := worker.run(appCtx)
			if appCtx.Err() == nil {
				if err == nil {
					err = errors.New("worker stopped unexpectedly")
				}
				workerErrors <- fmt.Errorf("%s: %w", worker.name, err)
			}
		}()
	}
	go func() {
		err := httpServer.Serve(listener)
		if !errors.Is(err, http.ErrServerClosed) {
			workerErrors <- fmt.Errorf("HTTP server: %w", err)
		}
	}()

	var cause error
	select {
	case <-ctx.Done():
	case cause = <-workerErrors:
	}
	app.BeginDrain()
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), time.Duration(config.ShutdownTimeoutSeconds)*time.Second)
	shutdownErr := httpServer.Shutdown(shutdownCtx)
	shutdownCancel()
	cancel()
	group.Wait()
	if cause != nil {
		return errors.Join(cause, shutdownErr)
	}
	if shutdownErr != nil {
		return fmt.Errorf("gracefully shut down app HTTP: %w", shutdownErr)
	}
	return nil
}

func closeConnections(connections []*grpc.ClientConn) {
	for _, connection := range connections {
		if err := connection.Close(); err != nil {
			slog.Warn("close gRPC health connection", "error", err)
		}
	}
}
