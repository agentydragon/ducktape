package server

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/internal/journalcache"
	statspb "github.com/agentydragon/ducktape/agentplane/proto/journalstatspb"

	"github.com/agentydragon/ducktape/agentplane/app/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/linkage_servicepb"
	"github.com/agentydragon/ducktape/agentplane/app/internal/auth"
	"github.com/agentydragon/ducktape/agentplane/app/internal/auth/session"
	"github.com/agentydragon/ducktape/agentplane/app/internal/electricproxy"
	"github.com/agentydragon/ducktape/agentplane/app/internal/federation"
	"github.com/agentydragon/ducktape/agentplane/app/internal/historyclient"
	"github.com/agentydragon/ducktape/agentplane/app/internal/historydata"
	apphttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi"
	sessionhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/sessions"
	streamhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/streams"
	threadhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/threads"
	"github.com/agentydragon/ducktape/agentplane/app/internal/migrations"
	"github.com/agentydragon/ducktape/agentplane/app/internal/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/app/internal/sessionclient"
	"github.com/agentydragon/ducktape/agentplane/app/internal/status"
	"github.com/agentydragon/ducktape/agentplane/app/internal/threads"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/agentydragon/ducktape/agentplane/kubeclient"
	netlistener "github.com/agentydragon/ducktape/agentplane/net/listener"
	"github.com/agentydragon/ducktape/agentplane/observability"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	dbmigrate "github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

const systemStatusTimeout = 5 * time.Second

// Run owns process dependencies and their shutdown. Each API call continues to use the
// repository's generated Kubernetes, gRPC, and protobuf APIs; the process adds only composition.
func Run(ctx context.Context, config settings.Config) error {
	logger := slog.Default()
	shutdownTracing, err := observability.Init(ctx, "agentplane-app")
	if err != nil {
		return fmt.Errorf("configure OpenTelemetry tracing: %w", err)
	}
	defer shutdownTracing()

	if err := dbmigrate.Run(ctx, config.DatabaseURL, migrations.Up); err != nil {
		return fmt.Errorf("migrate app database: %w", err)
	}
	db, err := postgres.Open(ctx, config.DatabaseURL, 16, 8)
	if err != nil {
		return fmt.Errorf("open app PostgreSQL database: %w", err)
	}
	defer db.Close()
	db.SetConnMaxLifetime(30 * time.Minute)

	wakeupHub, err := wakeups.Listen(ctx, config.DatabaseURL, threads.UpdatesChannel, session.SessionUpdatesChannel)
	if err != nil {
		return fmt.Errorf("start PostgreSQL update listener: %w", err)
	}
	defer wakeupHub.Close()

	kubeConfig, err := kubeclient.Config(config.Kubeconfig)
	if err != nil {
		return fmt.Errorf("load Kubernetes config: %w", err)
	}
	baseClient := &http.Client{Timeout: 10 * time.Second, Transport: observability.HTTPTransport(http.DefaultTransport)}
	historyReaderClient, err := historyclient.New(historyclient.Config{
		TLS: config.HistoryServiceTLS, Target: config.HistoryServiceTarget, TokenFile: config.HistoryServiceTokenFile,
		Timeout: time.Duration(config.HistoryServiceRequestTimeoutSeconds * float64(time.Second)),
	})
	if err != nil {
		return fmt.Errorf("configure History Service reader: %w", err)
	}
	defer historyReaderClient.Close()
	history, err := historydata.New(historyReaderClient)
	if err != nil {
		return err
	}
	threadStore := threads.NewStore(db)
	dispatcher, err := sessionclient.NewSessionDispatcherClient(sessionclient.SessionDispatcherConfig{
		TLS: config.SessionDispatcherTLS, Target: config.SessionDispatcherTarget, TokenFile: config.SessionDispatcherTokenFile,
		Timeout: time.Duration(config.SessionDispatcherRequestTimeoutSecs * float64(time.Second)),
	})
	if err != nil {
		return fmt.Errorf("configure Session Dispatcher: %w", err)
	}
	environmentPeer := config.EnvironmentService
	environmentTimeout := time.Duration(environmentPeer.RequestTimeoutSeconds * float64(time.Second))
	environmentConn, err := bearer.Dial(environmentPeer.Target, "", environmentTimeout, environmentPeer.TLS)
	if err != nil {
		return fmt.Errorf("configure Environment Service client: %w", err)
	}
	defer environmentConn.Close()
	environments := environmentpb.NewEnvironmentServiceClient(environmentConn)
	sessionClient, err := sessionclient.New(dispatcher)
	if err != nil {
		return fmt.Errorf("configure Session client: %w", err)
	}
	policyConnection, err := bearer.Dial(
		config.PolicyServiceTarget, config.PolicyServiceTokenFile, 10*time.Second, config.PolicyServiceTLS,
	)
	if err != nil {
		return fmt.Errorf("configure Policy Service client: %w", err)
	}
	defer policyConnection.Close()
	policySetNames := policypb.NewPolicyServiceClient(policyConnection)

	oidc := config.OIDC
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
	appAuth, err := auth.New(auth.Config{
		OIDC: oidc, Database: db,
		StoreFederationTokens: federator != nil, SessionChanges: wakeupHub,
		HTTPClient: baseClient,
	})
	if err != nil {
		return fmt.Errorf("configure app authentication: %w", err)
	}

	actionTarget, err := grpcTargetFromHTTPURL(config.ActionFederation.ServiceURL)
	if err != nil {
		return fmt.Errorf("configure Action Service gRPC client: %w", err)
	}
	actionCredentials, err := config.ActionsTLS.Credentials()
	if err != nil {
		return err
	}
	actionConnection, err := grpc.NewClient(actionTarget,
		observability.GRPCClientOption(), grpc.WithTransportCredentials(actionCredentials), grpc.WithDisableRetry())
	if err != nil {
		return fmt.Errorf("configure Action Service linkage gRPC client: %w", err)
	}
	defer closeConnections([]*grpc.ClientConn{actionConnection})
	linkageClient := linkage_servicepb.NewLinkageServiceClient(actionConnection)
	var samplerStats statspb.JournalStatsServiceClient
	if peer := config.SamplerStats; peer != nil {
		conn, err := bearer.Dial(peer.Target, "", 30*time.Second, peer.TLS)
		if err != nil {
			return fmt.Errorf("configure sampler journal stats: %w", err)
		}
		defer conn.Close()
		samplerStats = statspb.NewJournalStatsServiceClient(conn)
	}
	facadeContext, stopFacades := context.WithCancel(context.WithoutCancel(ctx))
	defer stopFacades()
	consoleConnector, err := sandboxes.NewConsoleConnector(kubeConfig)
	if err != nil {
		return fmt.Errorf("configure Sandbox console client: %w", err)
	}
	serviceStatus, healthConnections, err := newSystemStatus(config, baseClient)
	if err != nil {
		return err
	}
	defer closeConnections(healthConnections)

	server := newHTTPServer(appAuth)
	sessionHandlers := sessionhttp.NewHandler(sessionhttp.Dependencies{
		SandboxNamespace: config.SandboxNamespace, SandboxPresets: config.SandboxPresets,
		ThreadPresets: config.ThreadPresets, Environments: environments, SessionOpener: sessionClient,
		ThreadOpener: threadStore,
	})
	threadMetadata := threadhttp.NewHandler(threadhttp.Dependencies{
		Store: threadStore, Dispatcher: dispatcher,
		Updates: wakeupHub, UpdatesConnected: wakeupHub.Connected, DrainDone: server.DrainDone(),
	})
	appConfigHandler := apphttp.NewAppConfigHandler(config, policySetNames)
	statusHandler := apphttp.NewSystemStatusHandler(serviceStatus, journalcache.Start(facadeContext, samplerStats, historyReaderClient.JournalStats()))
	streams := streamhttp.Dependencies{
		Auth: appAuth, SandboxNamespace: config.SandboxNamespace, Sandboxes: environments,
		Federator: federator, Console: consoleConnector, Logger: logger,
	}
	if config.ElectricURL != "" {
		electric, electricErr := electricproxy.NewElectricProxy(baseClient, config.ElectricURL, threadEpochStore{history: history})
		if electricErr != nil {
			return fmt.Errorf("configure Electric thread sync: %w", electricErr)
		}
		streams.Electric = electric.Handler()
	}
	if err := configureFacades(facadeContext, config, &streams); err != nil {
		return err
	}
	if err := registerRoutes(server, routeOwners{
		appConfig: appConfigHandler, sessions: sessionHandlers, threads: threadMetadata,
		operatorLinkage: linkageClient, systemStatus: statusHandler,
		traceRelay: traceRelayHandler(), streams: streams,
	}); err != nil {
		return err
	}
	return serve(ctx, config, server, appAuth)
}

type threadEpochStore struct {
	history *historydata.Store
}

func (s threadEpochStore) CurrentEpoch(ctx context.Context, rawID string) (string, bool, error) {
	id, err := uuid.Parse(rawID)
	if err != nil {
		return "", false, err
	}
	return s.history.CurrentEpoch(ctx, id)
}

func newSystemStatus(config settings.Config, httpClient *http.Client) (*status.Checker, []*grpc.ClientConn, error) {
	probes := make([]status.Probe, 0, 7)
	connections := make([]*grpc.ClientConn, 0, 5)
	dialHealth := func(name, target string, tls *peertls.ClientTLS) error {
		credentials, err := tls.Credentials()
		if err != nil {
			return err
		}
		connection, err := grpc.NewClient(target,
			observability.GRPCClientOption(), grpc.WithTransportCredentials(credentials))
		if err != nil {
			return fmt.Errorf("configure %s health client: %w", name, err)
		}
		connections = append(connections, connection)
		probes = append(probes, status.GRPCHealthProbe(name, healthpb.NewHealthClient(connection)))
		return nil
	}
	for _, endpoint := range []struct {
		name, target string
		tls          *peertls.ClientTLS
	}{
		{"Environments", config.RunnerGatewayTarget, config.RunnerGatewayTLS}, {"History Service", config.HistoryServiceTarget, config.HistoryServiceTLS},
	} {
		if err := dialHealth(endpoint.name, endpoint.target, endpoint.tls); err != nil {
			closeConnections(connections)
			return nil, nil, err
		}
	}
	for _, endpoint := range []struct {
		name, raw string
		tls       *peertls.ClientTLS
	}{
		{"Action Service", config.ActionFederation.ServiceURL, config.ActionsTLS},
		{"Connection Service", config.ConnectionServiceURL, config.ConnectionServiceTLS},
		{"Notification Service", config.NotificationsURL, config.NotificationsTLS},
		{"Egress proxy", config.EgressAdminURL, config.EgressAdminTLS},
	} {
		target, err := grpcTargetFromHTTPURL(endpoint.raw)
		if err != nil {
			closeConnections(connections)
			return nil, nil, fmt.Errorf("configure %s health target: %w", endpoint.name, err)
		}
		if err := dialHealth(endpoint.name, target, endpoint.tls); err != nil {
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

func serve(ctx context.Context, config settings.Config, app *httpServer, appAuth *auth.Auth) error {
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
	serveErrors := make(chan error, 1)
	go func() {
		err := httpServer.Serve(listener)
		if !errors.Is(err, http.ErrServerClosed) {
			serveErrors <- fmt.Errorf("HTTP server: %w", err)
		}
	}()

	var cause error
	select {
	case <-ctx.Done():
	case cause = <-serveErrors:
	}
	app.BeginDrain()
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), time.Duration(config.ShutdownTimeoutSeconds)*time.Second)
	shutdownErr := httpServer.Shutdown(shutdownCtx)
	shutdownCancel()
	cancel()
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
