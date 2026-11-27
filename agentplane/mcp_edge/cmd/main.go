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
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/linkage_servicepb"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/operator"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/api/connection/connection_servicepb"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/internal/actionclient"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/internal/admission"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/internal/authserver"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/internal/connections"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/internal/mcpserver"
	"github.com/agentydragon/ducktape/agentplane/mcp_edge/migrations"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/agentydragon/ducktape/agentplane/postgres/probes"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/agentydragon/ducktape/agentplane/servicemain"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/grpc"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

type readiness struct {
	*probes.Probes
	identities identitySource
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
	rpcTLSListener, err := net.Listen("tcp", c.RPCTLS.Listen)
	if err != nil {
		return fmt.Errorf("TLS gRPC listener: %w", err)
	}
	defer rpcTLSListener.Close()
	httpListener, err := net.Listen("tcp", c.httpAddress())
	if err != nil {
		return fmt.Errorf("HTTP listener: %w", err)
	}
	defer httpListener.Close()
	return serve(ctx, c, rpcTLSListener, httpListener)
}

// serve answers mTLS gRPC on rpcTLSListener and HTTP (the facade, /mcp, OAuth, health and metrics) on httpListener
// until ctx ends or a listener fails.
func serve(ctx context.Context, c config, rpcTLSListener, httpListener net.Listener) error {
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
	identities, stopIdentities, err := startIdentities(ctx, c)
	if err != nil {
		return err
	}
	defer stopIdentities()
	operators, err := operator.NewVerifier(c.OperatorOIDC)
	if err != nil {
		return err
	}
	gate := admission.NewGate(operators)
	connectionStore := connections.NewStore(db, identities, identities)
	newGRPCServer := func(gate *admission.Gate, options ...grpc.ServerOption) *grpc.Server {
		options = append(options, observability.GRPCServerOption(),
			grpc.ChainUnaryInterceptor(gate.UnaryInterceptor), grpc.ChainStreamInterceptor(gate.StreamInterceptor))
		return grpc.NewServer(options...)
	}
	health := probes.New(db, hub)
	tlsGate := admission.NewGateWithOperatorServices(operators, c.OperatorServices)
	credentials, err := c.RPCTLS.Credentials(peertls.MCPEdgeURI)
	if err != nil {
		return fmt.Errorf("configure TLS gRPC listener: %w", err)
	}
	tlsServer := newGRPCServer(tlsGate, grpc.Creds(credentials))
	healthpb.RegisterHealthServer(tlsServer, readiness{Probes: health, identities: identities})
	connection_servicepb.RegisterConnectionServiceServer(tlsServer, connections.NewService(connectionStore, tlsGate, hub))
	facadeContext, stopFacade := context.WithCancel(context.WithoutCancel(ctx))
	defer stopFacade()
	connectionService := connections.NewService(connectionStore, gate, hub)
	httpFacade, err := connectionHTTPHandler(facadeContext, connectionService, gate)
	if err != nil {
		return err
	}
	routes := http.NewServeMux()
	routes.Handle("/v1/", httpFacade)
	routes.Handle("GET /healthz", healthHTTPHandler(readiness{Probes: health, identities: identities}, probes.LivenessService))
	routes.Handle("GET /readyz", healthHTTPHandler(readiness{Probes: health, identities: identities}, ""))
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
	routes.Handle("GET "+actionclient.ClientMetadataPath, actionclient.MetadataHandler(linkage_servicepb.NewLinkageServiceClient(metadataConnection)))
	if c.OAuth != nil {
		connection, err := mountOAuth(ctx, routes, c, db, connectionStore)
		if err != nil {
			return err
		}
		defer connection.Close()
	}
	server := &http.Server{Handler: observability.HTTPHandler(routes), ReadHeaderTimeout: 10 * time.Second}
	return graceful.Serve(ctx, graceful.Group{
		GRPC: []graceful.GRPC{{Name: "TLS gRPC", Server: tlsServer, Listener: rpcTLSListener}},
		HTTP: []graceful.HTTP{{Name: "HTTP", Server: server, Listener: httpListener}},
		Drain: func() {
			health.Drain()
			gate.Drain()
			tlsGate.Drain()
		},
	})
}

func mountOAuth(ctx context.Context, routes *http.ServeMux, c config, db *sql.DB, connectionStore *connections.Store) (*grpc.ClientConn, error) {
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
	client, err := actionclient.New(actionclient.Config{
		ResolveGrant: func(ctx context.Context, bearer string) (*connections.ResolvedGrant, error) {
			info, err := authority.VerifyConnectionBearer(ctx, bearer)
			if err != nil {
				return nil, err
			}
			return &info.Grant, nil
		},
		RecheckPeriod: 5 * time.Second,
		Logger:        slog.Default(),
	})
	if err != nil {
		return nil, err
	}
	connection, err := client.Dial(c.ActionsTarget, c.ActionsTokenFile, 30*time.Second, c.ActionsTLS)
	if err != nil {
		return nil, err
	}
	mcp := mcpserver.NewHandler(action_servicepb.NewActionServiceClient(connection), authority.VerifyBearer, mcpserver.Options{
		MaxWaitSeconds: c.MaxWaitSeconds, DirectWaitSeconds: c.DirectWaitSeconds,
		ResourceMetadataURL: authority.ResourceMetadataURL(), Logger: slog.Default(),
	})
	routes.Handle("/mcp", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		bearer, _ := auth.BearerToken(r.Header.Values("Authorization"))
		mcp.ServeHTTP(w, r.WithContext(actionclient.WithBearer(r.Context(), bearer)))
	}))
	routes.Handle("/", authority.Handler())
	return connection, nil
}

func main() {
	servicemain.Main(servicemain.Service[config]{
		Name:       "MCP edge",
		TraceName:  "mcp-edge",
		Migrations: servicemain.Migrations{DatabaseURLEnv: databaseURLEnv, Up: migrations.Up},
		Load:       loadConfig,
		Run:        run,
	})
}
