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
	"github.com/agentydragon/ducktape/agentplane/action_service/linkage_servicepb"
	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/kubeclient"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/api/connection/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/authserver"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/connections"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/internal/mcpserver"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/migrations"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/agentydragon/ducktape/agentplane/postgres/probes"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/grpc"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"k8s.io/client-go/rest"
)

const shutdownGrace = 5 * time.Second

type readiness struct {
	*probes.Probes
	identities *identityIndex
}

func (r readiness) Check(ctx context.Context, request *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	if request.GetService() == "" && !r.identities.ready() {
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
	if err := migrate.Run(ctx, c.DatabaseURL, migrations.Up); err != nil {
		return err
	}
	kubeConfig, err := kubeclient.Config("")
	if err != nil {
		return err
	}
	kubeConfig.QPS = -1
	grpcListener, err := net.Listen("tcp", c.grpcAddress())
	if err != nil {
		return fmt.Errorf("gRPC listener: %w", err)
	}
	defer grpcListener.Close()
	httpListener, err := net.Listen("tcp", c.httpAddress())
	if err != nil {
		return fmt.Errorf("HTTP listener: %w", err)
	}
	defer httpListener.Close()
	return serve(ctx, c, kubeConfig, grpcListener, httpListener)
}

// serve answers gRPC on grpcListener and HTTP (the facade, /mcp, OAuth and metrics) on httpListener until ctx ends or a
// listener fails.
func serve(ctx context.Context, c config, kubeConfig *rest.Config, grpcListener, httpListener net.Listener) error {
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
	identities, watches, err := identityWatches(kubeConfig, c.PolicyNamespace)
	if err != nil {
		return err
	}
	watchContext, stopWatching := context.WithCancel(ctx)
	watching := runIdentities(watchContext, watches)
	defer func() { stopWatching(); <-watching }()
	operators, err := api.NewOperatorVerifier(c.OperatorOIDC)
	if err != nil {
		return err
	}
	gate := api.NewGate(api.GateOptions{
		Operators: operators,
		AdditionalOperatorMethods: []string{
			connection_servicepb.ConnectionService_ListConnectionIdentities_FullMethodName,
			connection_servicepb.ConnectionService_ListConnections_FullMethodName,
			connection_servicepb.ConnectionService_GetConnection_FullMethodName,
			connection_servicepb.ConnectionService_UpdateConnection_FullMethodName,
			connection_servicepb.ConnectionService_RebindConnection_FullMethodName,
			connection_servicepb.ConnectionService_UnbindConnection_FullMethodName,
			connection_servicepb.ConnectionService_WatchConnections_FullMethodName,
			connection_servicepb.ConnectionService_PreviewConnectionEnrollment_FullMethodName,
			connection_servicepb.ConnectionService_DecideConnectionEnrollment_FullMethodName,
		},
	})
	connectionStore := connections.NewStore(db, identities, identities)
	grpcServer := grpc.NewServer(observability.GRPCServerOption(),
		grpc.ChainUnaryInterceptor(gate.UnaryInterceptor), grpc.ChainStreamInterceptor(gate.StreamInterceptor))
	health := probes.New(db, hub)
	healthpb.RegisterHealthServer(grpcServer, readiness{Probes: health, identities: identities})
	connection_servicepb.RegisterConnectionServiceServer(grpcServer, connections.NewService(connectionStore, gate, hub))
	facadeContext, stopFacade := context.WithCancel(context.WithoutCancel(ctx))
	defer stopFacade()
	httpFacade, err := facade.NewHandler(facadeContext, grpcListener.Addr().String(), 0, connection_servicepb.RegisterConnectionServiceHandlerFromEndpoint)
	if err != nil {
		return err
	}
	routes := http.NewServeMux()
	routes.Handle("/v1/", httpFacade)
	routes.Handle("/metrics", promhttp.Handler())
	// This public document is owned by Actions; the edge supplies only its HTTP presentation.
	actionsCredentials, err := c.ActionsTLS.Credentials()
	if err != nil {
		return err
	}
	metadataConnection, err := grpc.NewClient(c.ActionsTarget, observability.GRPCClientOption(), grpc.WithTransportCredentials(actionsCredentials))
	if err != nil {
		return fmt.Errorf("configure Action client metadata: %w", err)
	}
	defer metadataConnection.Close()
	routes.Handle("GET "+actionClientMetadataPath, actionClientMetadata(linkage_servicepb.NewLinkageServiceClient(metadataConnection)))
	if c.OAuth != nil {
		connection, err := mountOAuth(ctx, routes, c, db, connectionStore, identities)
		if err != nil {
			return err
		}
		defer connection.Close()
	}
	server := &http.Server{Handler: observability.HTTPHandler(routes), ReadHeaderTimeout: 10 * time.Second}
	failed := make(chan error, 2)
	go func() { failed <- fmt.Errorf("gRPC listener: %w", grpcServer.Serve(grpcListener)) }()
	go func() { failed <- fmt.Errorf("HTTP listener: %w", server.Serve(httpListener)) }()
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
	// The facade's calls are gRPC calls, so the HTTP listener stops first.
	if err := server.Shutdown(stopped); err != nil {
		server.Close()
	}
	graceful.Stop(stopped, grpcServer)
	return result
}

func mountOAuth(ctx context.Context, routes *http.ServeMux, c config, db *sql.DB, connectionStore *connections.Store, identities *identityIndex) (*grpc.ClientConn, error) {
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
	caller := &coreCaller{
		authority: func(ctx context.Context, bearer string) (*connections.ResolvedGrant, error) {
			info, err := authority.VerifyConnectionBearer(ctx, bearer)
			if err != nil {
				return nil, err
			}
			return &info.Grant, nil
		},
		recheckPeriod: 5 * time.Second, logger: slog.Default(),
	}
	connection, err := dialActions(c.ActionsTarget, c.ActionsTokenFile, 30*time.Second, c.ActionsTLS, caller)
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
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if handled, err := migrate.RunOnly(ctx, os.Args[1:], databaseURLEnv, migrations.Up); handled {
		if err != nil {
			slog.Error("MCP edge migration failed", "error", err)
			os.Exit(1)
		}
		return
	}
	c, err := loadConfig()
	if err != nil {
		slog.Error("invalid MCP edge configuration", "error", err)
		os.Exit(2)
	}
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
