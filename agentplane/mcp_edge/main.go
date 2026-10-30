// Command mcp-edge serves external MCP Connections and OAuth independently of the Action Service.
// The Action Service remains the authority over Actions and their policy; this process owns the external grants.
package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/authserver"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connections"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/mcpserver"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres/go"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/probes"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/grpc"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

const shutdownGrace = 5 * time.Second

type readiness struct {
	*probes.Probes
	callers *callerIndex
}

func (r readiness) Check(ctx context.Context, request *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	if request.GetService() == "" && !r.callers.ready() {
		return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_NOT_SERVING}, nil
	}
	return r.Probes.Check(ctx, request)
}

func openDatabase(ctx context.Context, databaseURL string) (*sql.DB, error) {
	db, err := postgres.Open(ctx, databaseURL, 6, 4)
	if err != nil {
		return nil, fmt.Errorf("MCP edge PostgreSQL unavailable: %w", err)
	}
	return db, nil
}

func run(ctx context.Context, c config) error {
	kubeConfig, err := rest.InClusterConfig()
	if err != nil {
		return err
	}
	kubeConfig.WrapTransport = observability.WrapHTTPTransport(kubeConfig.WrapTransport)
	kubeConfig.QPS = -1
	kube, err := kubernetes.NewForConfig(kubeConfig)
	if err != nil {
		return err
	}
	listener, err := net.Listen("tcp", c.address())
	if err != nil {
		return err
	}
	defer listener.Close()
	return serve(ctx, c, kubeConfig, kube, listener)
}

func serve(ctx context.Context, c config, kubeConfig *rest.Config, kube kubernetes.Interface, listener net.Listener) error {
	db, err := openDatabase(ctx, c.DatabaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	hub, err := wakeups.Listen(ctx, c.DatabaseURL, connections.UpdatesChannel)
	if err != nil {
		return fmt.Errorf("MCP edge PostgreSQL listener unavailable: %w", err)
	}
	defer hub.Close()
	callers, watches, err := callerWatches(kubeConfig, c.CallerServiceAccountNamespaces, c.MintableAccounts)
	if err != nil {
		return err
	}
	watchContext, stopWatching := context.WithCancel(ctx)
	watching := runCallers(watchContext, watches)
	defer func() { stopWatching(); <-watching }()
	operators, err := api.NewOperatorVerifier(c.OperatorOIDC)
	if err != nil {
		return err
	}
	gate := api.NewGate(api.GateOptions{Operators: operators})
	connectionStore := connections.NewStore(db, callers)
	grpcServer := grpc.NewServer(observability.GRPCServerOption(),
		grpc.ChainUnaryInterceptor(gate.UnaryInterceptor), grpc.ChainStreamInterceptor(gate.StreamInterceptor))
	health := probes.New(db, hub)
	healthpb.RegisterHealthServer(grpcServer, readiness{Probes: health, callers: callers})
	connection_servicepb.RegisterConnectionServiceServer(grpcServer, connections.NewService(connectionStore, gate, hub))
	facadeContext, stopFacade := context.WithCancel(context.WithoutCancel(ctx))
	defer stopFacade()
	httpFacade, err := facade.NewHandler(facadeContext, listener.Addr().String(), 0, connection_servicepb.RegisterConnectionServiceHandlerFromEndpoint)
	if err != nil {
		return err
	}
	routes := http.NewServeMux()
	routes.Handle("/v1/", httpFacade)
	routes.Handle("/metrics", promhttp.Handler())
	if c.OAuth != nil {
		connection, err := mountOAuth(ctx, routes, c, db, connectionStore, callers, kube)
		if err != nil {
			return err
		}
		defer connection.Close()
	}
	server := &http.Server{Handler: observability.HTTPHandler(routes), ReadHeaderTimeout: 10 * time.Second}
	facade.MountGRPC(server, grpcServer)
	failed := make(chan error, 1)
	go func() { failed <- server.Serve(listener) }()
	var result error
	select {
	case result = <-failed:
	case <-ctx.Done():
		result = ctx.Err()
	}
	health.Drain()
	gate.Drain()
	stopped, cancel := context.WithTimeout(context.WithoutCancel(ctx), shutdownGrace)
	defer cancel()
	if err := server.Shutdown(stopped); err != nil {
		server.Close()
	}
	grpcStopped := make(chan struct{})
	go func() { grpcServer.GracefulStop(); close(grpcStopped) }()
	select {
	case <-grpcStopped:
	case <-stopped.Done():
		grpcServer.Stop()
		<-grpcStopped
	}
	return result
}

func mountOAuth(ctx context.Context, routes *http.ServeMux, c config, db *sql.DB, connectionStore *connections.Store, callers *callerIndex, kube kubernetes.Interface) (*grpc.ClientConn, error) {
	o := c.OAuth
	secret, err := os.ReadFile(o.UpstreamClientSecretFile)
	if err != nil {
		return nil, errors.New("reading oauth.upstream_client_secret_file failed")
	}
	if strings.TrimSpace(string(secret)) == "" {
		return nil, errors.New("oauth upstream client secret is empty")
	}
	authority, err := authserver.New(ctx, authserver.Config{
		Issuer: o.BaseURL, ConsentURL: o.IntegrationAppURL,
		UpstreamIssuer: o.UpstreamIssuer, UpstreamClientID: o.UpstreamClientID,
		UpstreamClientSecret: strings.TrimSpace(string(secret)), UpstreamSubject: o.UpstreamSubject,
		Approver:       connections.Operator{Issuer: o.ApprovingOperator.Issuer, Subject: o.ApprovingOperator.Subject},
		DocumentClient: authserver.NewDocumentClient(),
	}, db, connectionStore)
	if err != nil {
		return nil, fmt.Errorf("starting external MCP authorization: %w", err)
	}
	caller := &coreCaller{authority: authority.VerifyBearer, callers: callers, kube: kube, recheckPeriod: 5 * time.Second, tokens: map[auth.Account]cachedToken{}}
	connection, err := dialActions(c.ActionsTarget, caller)
	if err != nil {
		return nil, err
	}
	mcp := mcpserver.NewHandler(action_servicepb.NewActionServiceClient(connection), authority.VerifyBearer, mcpserver.Options{
		MaxWaitSeconds: c.MaxWaitSeconds, DirectWaitSeconds: c.DirectWaitSeconds,
		ResourceMetadataURL: authority.ResourceMetadataURL(), Logger: slog.Default(),
	})
	routes.Handle("/mcp", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		bearer, _ := auth.BearerToken(r.Header.Values("Authorization"))
		mcp.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), edgeBearerKey{}, bearer)))
	}))
	routes.Handle("/", authority.Handler())
	return connection, nil
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	c, err := loadConfig()
	if err != nil {
		slog.Error("invalid MCP edge configuration", "error", err)
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	shutdownTracing, err := observability.Init(ctx, "mcp-edge")
	if err != nil {
		slog.Error("MCP edge tracing setup failed", "error", err)
		os.Exit(1)
	}
	err = run(ctx, c)
	shutdownTracing()
	if err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("MCP edge stopped", "error", err)
		os.Exit(1)
	}
}
