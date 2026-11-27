// Command notification-service serves subscriptions and Session inboxes, works them into runner notices, and
// takes GitHub's webhooks.
package main

import (
	"context"
	"database/sql"
	"fmt"
	"maps"
	"net"
	"net/http"
	"strconv"
	"time"

	actionpb "github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/kubeclient"
	netlistener "github.com/agentydragon/ducktape/agentplane/net/listener"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	inboxstore "github.com/agentydragon/ducktape/agentplane/notification_service/internal/inbox"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/inboxaccess"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/rpc"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/actions"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github"
	githubclient "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/client" // Distinguish GitHub transport from the Kubernetes client.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/publication"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/publication/fanout"
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/webhook"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/transaction"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/wakeup"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/worker"
	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/observability"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/agentydragon/ducktape/agentplane/postgres/probes"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/servicemain"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	migrationOnlyDatabaseURLEnv = "AGENTPLANE_NOTIFICATIONS_DATABASE_URL"
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

// kubeDeps is what the service reads from Kubernetes: live workload credentials, Sandboxes, and
// the API server its send grants are watched on. Identities come from PolicyService.
type kubeDeps struct {
	Sandboxes client.Reader
	Policies  *rest.Config
}

func inClusterDeps() (kubeDeps, error) {
	kubeConfig, err := kubeclient.Config("")
	if err != nil {
		return kubeDeps{}, err
	}
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{sandboxapi.AddToScheme, policyapi.AddToScheme} {
		if err := add(scheme); err != nil {
			return kubeDeps{}, err
		}
	}
	sandboxes, err := client.New(kubeConfig, client.Options{Scheme: scheme})
	if err != nil {
		return kubeDeps{}, err
	}
	return kubeDeps{Sandboxes: sandboxes, Policies: kubeConfig}, nil
}

// run serves on the configured address, authenticating workload and service SVIDs.
func run(ctx context.Context, c config) error {
	if err := migrate.Run(ctx, c.DatabaseURL, migrations.Up); err != nil {
		return fmt.Errorf("migrate Notification Service database: %w", err)
	}
	deps, err := inClusterDeps()
	if err != nil {
		return err
	}
	grpcListener, err := netlistener.Open(c.RPC.ListenAddress)
	if err != nil {
		return fmt.Errorf("gRPC listener: %w", err)
	}
	defer grpcListener.Close()
	httpListener, err := net.Listen("tcp", net.JoinHostPort(c.Host, strconv.Itoa(c.HTTPPort)))
	if err != nil {
		return fmt.Errorf("HTTP listener: %w", err)
	}
	defer httpListener.Close()
	return serve(ctx, c, deps, grpcListener, httpListener)
}

func openDatabase(ctx context.Context, databaseURL string) (*sql.DB, error) {
	db, err := postgres.Open(ctx, databaseURL, databaseConnections, databaseConnections/2)
	if err != nil {
		return nil, fmt.Errorf("Notification Service PostgreSQL unavailable: %w", err)
	}
	return db, nil
}

// newRoutes keeps the HTTP listener for the signed GitHub webhook only. Workload methods are gRPC.
func newRoutes(webhookHandler http.Handler) http.Handler {
	routes := http.NewServeMux()
	if webhookHandler != nil {
		routes.Handle(webhook.Pattern, webhookHandler)
	}
	return routes
}

// serve answers until ctx ends or a worker or a listener fails: the gRPC servicers and signed webhook.
func serve(ctx context.Context, c config, deps kubeDeps, grpcListener, httpListener net.Listener) error {
	identities, stopIdentities, err := startIdentities(ctx, c)
	if err != nil {
		return err
	}
	defer stopIdentities()
	callers, err := auth.NewKubernetesSVIDResolver(deps.Policies, []string{c.Namespace}, identities.uids)
	if err != nil {
		return err
	}
	sandboxes, err := sandboxreader.New(c.Namespace, deps.Sandboxes, identities.uids)
	if err != nil {
		return err
	}
	db, err := openDatabase(ctx, c.DatabaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	hub, err := wakeups.Listen(ctx, c.DatabaseURL, wakeup.WorkChannel, wakeup.InboxChannel)
	if err != nil {
		return fmt.Errorf("Notification Service PostgreSQL listener unavailable: %w", err)
	}
	defer hub.Close()
	// Neither connection dials until its first call, so a peer that is down does not stop the service starting.
	historyConnection, err := bearer.Dial(c.HistoryService.Target, c.HistoryService.TokenFile, seconds(c.HistoryService.RequestTimeoutSeconds), c.HistoryService.TLS,
		grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(historyReceiveBytes)))
	if err != nil {
		return err
	}
	defer historyConnection.Close()
	dispatcherConnection, err := bearer.Dial(c.SessionDispatcher.Target, c.SessionDispatcher.TokenFile, seconds(c.SessionDispatcher.RequestTimeoutSeconds), c.SessionDispatcher.TLS)
	if err != nil {
		return err
	}
	defer dispatcherConnection.Close()
	actionEndpoint, err := c.Actions.grpcEndpoint()
	if err != nil {
		return err
	}
	var actionDialOptions []grpc.DialOption
	if actionEndpoint.tlsServerName != "" && c.Actions.TLS == nil {
		actionDialOptions = append(actionDialOptions,
			grpc.WithTransportCredentials(credentials.NewClientTLSFromCert(nil, actionEndpoint.tlsServerName)))
	}
	actionsConnection, err := bearer.Dial(actionEndpoint.target, c.Actions.TokenFile, seconds(c.Actions.RequestTimeoutSeconds), c.Actions.TLS, actionDialOptions...)
	if err != nil {
		return err
	}
	defer actionsConnection.Close()
	sendPolicy := inboxaccess.NewPolicy()
	policyWatches, err := sendPolicy.Watch(deps.Policies, c.Namespace, identities.rules)
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

	runner := transaction.New(db)
	inboxes := inboxstore.New(db, runner, c.Quotas)
	githubFanout := fanout.New(db, runner, inboxes)
	actionsSource := &actions.Source{Service: actionpb.NewActionServiceClient(actionsConnection), Store: inboxes}
	// Source processing is independent of inbox delivery and the destination being online.
	var githubAPI rpc.GitHub
	providers := map[string]sources.Provider{"actions": actionsSource}
	var webhookHandler http.Handler
	if c.githubEnabled() {
		githubClient, err := githubclient.New(c.GitHub.Settings)
		if err != nil {
			return err
		}
		sharedState := githubstate.New(db, runner, time.Duration(c.GitHub.FreshnessSeconds)*time.Second, publication.Adapter{})
		provider := github.NewProvider(githubClient, githubFanout, sharedState, c.GitHub.Settings)
		githubAPI, providers["github"] = provider, provider
		webhookHandler = webhook.New(c.GitHub.Settings, c.GitHub.Retention, githubFanout)
	}

	options := rpc.Options{
		Store: inboxes, Wakeups: hub, Namespace: c.Namespace, Sandboxes: sandboxes,
		// The Action Service source authorizes a subscription the same way it reads one.
		Actions: actionsSource, GitHub: githubAPI, SendPolicy: sendPolicy, History: history,
		Sessions:               dispatcherpb.NewSessionDispatcherClient(dispatcherConnection),
		MessageBodyLimit:       c.MessageBodyLimitBytes,
		OperatorReaderServices: c.OperatorReaderServices,
		Debounce:               c.NoticeDebounce.Debounce(),
		RequestTimeout:         seconds(c.RequestTimeoutSeconds),
	}
	// A worker that stops ends the process, so a ready replica's workers are running.
	health := probes.New(db, hub)
	methods := auth.HealthMethods()
	// A watch carries no deadline of its own, so the request timeout bounds its admission.
	notificationMethods, err := rpc.Methods(options, callers)
	if err != nil {
		return fmt.Errorf("configure Notification Service methods: %w", err)
	}
	maps.Copy(methods, notificationMethods)
	serverOptions := append(methods.ServerOptions(options.RequestTimeout), observability.GRPCServerOption())
	security, err := c.RPC.ServerCredentialsWithServices(peertls.NotificationsURI)
	if err != nil {
		return err
	}
	if security != nil {
		serverOptions = append(serverOptions, grpc.Creds(security))
	}
	grpcServer := grpc.NewServer(serverOptions...)
	notifications := rpc.NewNotifications(options)
	notificationspb.RegisterNotificationsServer(grpcServer, notifications)
	healthpb.RegisterHealthServer(grpcServer, health)
	routes := newRoutes(webhookHandler)
	server := &http.Server{Handler: observability.HTTPHandler(routes), ReadHeaderTimeout: readHeaderTimeout}

	// Each worker follows the commits on its own signal.
	signals := make([]<-chan struct{}, workers*2)
	for i := range signals {
		changes, unsubscribe, err := hub.Subscribe(wakeup.WorkChannel)
		if err != nil {
			return err
		}
		defer unsubscribe()
		signals[i] = changes
	}
	inboxWorker := &worker.Worker{
		Store:      inboxes,
		History:    history,
		Dispatcher: dispatcherpb.NewSessionDispatcherClient(dispatcherConnection),
		Settings:   c.Settings,
	}
	sourceWorker := &sources.Worker{Store: inboxes, Providers: providers}
	maintenance := github.Maintenance{Fanout: githubFanout, Retention: c.GitHub.Retention}
	failed := make(chan error, workers*2+1)
	// The workers outlive the listeners, so a claim in progress is released before the database closes: the
	// deferred cancel ends them, and the wait after it returns once they have released their claims.
	workContext, stopWorkers := context.WithCancel(context.WithoutCancel(ctx))
	running := runWorkers(workContext, workers*2+1, failed, func(ctx context.Context, index int) error {
		if index == workers*2 {
			return maintenance.Run(ctx)
		}
		if index < workers {
			return sourceWorker.Run(ctx, signals[index])
		}
		return inboxWorker.Run(ctx, signals[index])
	})
	defer running.Wait()
	defer stopWorkers()
	// The webhook listener stops before workload and backend gRPC calls drain.
	return graceful.Serve(ctx, graceful.Group{
		GRPC:     []graceful.GRPC{{Name: "gRPC", Server: grpcServer, Listener: grpcListener}},
		HTTP:     []graceful.HTTP{{Name: "HTTP", Server: server, Listener: httpListener}},
		Failures: failed,
		Drain: func() {
			health.Drain()
			notifications.Drain()
		},
	})
}

func main() {
	servicemain.Main(servicemain.Service[config]{
		Name: "Notification Service", TraceName: "notification-service",
		Migrations: servicemain.Migrations{DatabaseURLEnv: migrationOnlyDatabaseURLEnv, Up: migrations.Up},
		Load:       loadConfig, Run: run,
	})
}
