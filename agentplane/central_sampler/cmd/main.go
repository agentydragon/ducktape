package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/historyexport"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/residentruntime"
	samplerrpc "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/rpc"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	netlistener "github.com/agentydragon/ducktape/agentplane/net/listener"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

type startupFailure struct {
	stage string
	err   error
}

func (e *startupFailure) Error() string { return e.err.Error() }
func (e *startupFailure) Unwrap() error { return e.err }

func atStartupStage(stage string, err error) error {
	if err == nil {
		return nil
	}
	return &startupFailure{stage: stage, err: err}
}

func startupLogAttrs(err error) []any {
	stage := "startup"
	var failure *startupFailure
	if errors.As(err, &failure) {
		stage = failure.stage
	}
	attrs := []any{"stage", stage, "error_type", safeErrorType(err)}
	var validation *samplersettings.ValidationError
	if errors.As(err, &validation) {
		attrs = append(attrs, "field", validation.Field(), "rule", validation.Rule())
	}
	return attrs
}

func safeErrorType(err error) string {
	var validation *samplersettings.ValidationError
	if errors.As(err, &validation) {
		return fmt.Sprintf("%T", validation)
	}
	for {
		unwrapped := errors.Unwrap(err)
		if unwrapped == nil {
			return fmt.Sprintf("%T", err)
		}
		err = unwrapped
	}
}

func run(ctx context.Context, settings samplersettings.Settings) error {
	if settings.PlatformInstructions == nil {
		slog.Warn("Agentplane platform guidance is not configured; new Sessions retain caller-only instructions")
	}
	databaseURL := strings.Replace(settings.DatabaseURL, "postgresql+asyncpg://", "postgresql://", 1)
	if err := migrate.Run(ctx, databaseURL, migrations.Up); err != nil {
		return atStartupStage("database_migrations", err)
	}
	// Allow session state writes and lease renewals, eight exporter workers, and sixteen RPCs.
	db, err := postgres.Open(ctx, databaseURL, 2*settings.MaxLiveSessions+8+16, 16)
	if err != nil {
		return atStartupStage("service_database", err)
	}
	defer db.Close()
	if err := os.MkdirAll(settings.Logs, 0700); err != nil {
		return atStartupStage("logs_directory", err)
	}
	var kubeConfig *rest.Config
	if settings.Kubeconfig != "" {
		kubeConfig, err = clientcmd.BuildConfigFromFlags("", settings.Kubeconfig)
	} else {
		kubeConfig, err = rest.InClusterConfig()
	}
	if err != nil {
		return atStartupStage("kubernetes_config", err)
	}
	kubeConfig.QPS = -1
	kubeConfig.ContentType = "application/json"
	client, err := kubernetes.NewForConfig(kubeConfig)
	if err != nil {
		return atStartupStage("kubernetes_client", err)
	}
	namespaces := make([]string, 0, len(settings.AllowedAccounts))
	for _, account := range settings.AllowedAccounts {
		namespace, _, _ := strings.Cut(account, "/")
		namespaces = append(namespaces, namespace)
	}
	resolver, err := auth.NewResolver(client.AuthenticationV1().TokenReviews(), settings.Audience, namespaces)
	if err != nil {
		return atStartupStage("token_review_resolver", err)
	}
	store := sessionstore.New(db, time.Duration(settings.RPCTimeoutSeconds)*time.Second)
	hub, err := wakeups.Listen(ctx, databaseURL, sessionstore.OutboxChannel, sessionstore.WorkChannel)
	if err != nil {
		return atStartupStage("database_wakeups", err)
	}
	defer hub.Close()
	work, unsubscribe, err := hub.Subscribe(sessionstore.WorkChannel)
	if err != nil {
		return atStartupStage("work_subscription", err)
	}
	defer unsubscribe()
	runtime := residentruntime.New(store, settings, residentruntime.NewCodeModeHost(context.Background(), settings, store))
	serverOptions := []grpc.ServerOption{grpc.MaxRecvMsgSize(64 * 1024 * 1024), grpc.MaxSendMsgSize(64 * 1024 * 1024)}
	register := func(server *grpc.Server) {
		samplerpb.RegisterSamplerServer(server, &samplerrpc.Sampler{Store: store, Runtime: runtime, Resolver: resolver, Settings: settings, Wakeups: hub})
	}
	server := grpc.NewServer(serverOptions...)
	register(server)
	healthServer := health.NewServer()
	healthpb.RegisterHealthServer(server, healthServer)
	listener, err := netlistener.Open(settings.Listen)
	if err != nil {
		return atStartupStage("grpc_listener", err)
	}
	defer listener.Close()
	var tlsServer *grpc.Server
	var tlsListener net.Listener
	if settings.ListenTLS != nil {
		credentials, err := settings.ListenTLS.Credentials(peertls.CentralSamplerURI)
		if err != nil {
			return atStartupStage("grpc_tls_credentials", err)
		}
		tlsOptions := append(append([]grpc.ServerOption(nil), serverOptions...), grpc.Creds(credentials))
		tlsServer = grpc.NewServer(tlsOptions...)
		register(tlsServer)
		healthpb.RegisterHealthServer(tlsServer, healthServer)
		tlsListener, err = netlistener.Open(settings.ListenTLS.Listen)
		if err != nil {
			tlsServer.Stop()
			return atStartupStage("grpc_tls_listener", err)
		}
		defer tlsListener.Close()
	}
	running, runningCtx := errgroup.WithContext(ctx)
	running.Go(func() error { return runtime.Run(runningCtx, work) })
	if settings.HistoryTarget != "" {
		running.Go(func() error {
			return historyexport.Run(runningCtx, store, settings.HistoryTarget, settings.HistoryTokenFile, sessionstore.OutboxChannel, hub, settings.HistoryTLS)
		})
	}
	running.Go(func() error {
		err := server.Serve(listener)
		if errors.Is(err, grpc.ErrServerStopped) {
			return nil
		}
		return err
	})
	if tlsServer != nil {
		running.Go(func() error {
			err := tlsServer.Serve(tlsListener)
			if errors.Is(err, grpc.ErrServerStopped) {
				return nil
			}
			return err
		})
	}
	running.Go(func() error {
		<-runningCtx.Done()
		healthServer.Shutdown()
		stopping, release := context.WithTimeout(context.Background(), 5*time.Second)
		defer release()
		graceful.Stop(stopping, server)
		if tlsServer != nil {
			graceful.Stop(stopping, tlsServer)
		}
		return nil
	})
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	err = running.Wait()
	if errors.Is(err, context.Canceled) && ctx.Err() != nil {
		return nil
	}
	return atStartupStage("service_runtime", err)
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	if handled, err := migrate.RunOnly(ctx, os.Args[1:], "AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL", migrations.Up); handled {
		if err != nil {
			slog.Error("central sampler migration stopped", startupLogAttrs(atStartupStage("migration_only", err))...)
			os.Exit(1)
		}
		return
	}
	configuration := flag.String("config", os.Getenv("AGENTPLANE_CENTRAL_SAMPLER_CONFIG"), "JSON settings path")
	flag.Parse()
	settings, err := samplersettings.LoadSettings(*configuration)
	if err == nil {
		err = run(ctx, settings)
	} else {
		var validation *samplersettings.ValidationError
		stage := "load_settings"
		if errors.As(err, &validation) {
			stage = "validate_settings"
		}
		err = atStartupStage(stage, err)
	}
	if err != nil {
		slog.Error("central sampler stopped", startupLogAttrs(err)...)
		os.Exit(1)
	}
}
