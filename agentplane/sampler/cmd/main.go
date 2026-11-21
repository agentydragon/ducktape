package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	harnesshostpb "github.com/agentydragon/ducktape/agentplane/harness_host/harnesshostpb"
	netlistener "github.com/agentydragon/ducktape/agentplane/net/listener"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	sampleradminpb "github.com/agentydragon/ducktape/agentplane/sampler/api/sampleradminpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/historyexport"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/actions"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime/session"
	samplerrpc "github.com/agentydragon/ducktape/agentplane/sampler/internal/rpc"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/migrations"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
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

func newHealthOnlyServer(healthServer *health.Server) *grpc.Server {
	server := grpc.NewServer()
	healthpb.RegisterHealthServer(server, healthServer)
	return server
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
	var actionClient *actions.Client
	if settings.Actions != nil {
		actionClient, err = actions.Dial(ctx, settings.Actions.Target, settings.Actions.TLS, time.Duration(settings.RPCTimeoutSeconds)*time.Second)
		if err != nil {
			return atStartupStage("actions_client", err)
		}
		defer actionClient.Close()
	}
	runtime := residentruntime.New(store, settings, session.NewCodeModeHost(codemode.NewManager(context.Background(), settings.CodeModeHost, settings.Logs, time.Duration(settings.RPCTimeoutSeconds)*time.Second, settings.CodeModeObservationTimeout()), store, actionClient))
	serverOptions := []grpc.ServerOption{grpc.MaxRecvMsgSize(64 * 1024 * 1024), grpc.MaxSendMsgSize(64 * 1024 * 1024)}
	register := func(server *grpc.Server) {
		sampler := &samplerrpc.Sampler{Store: store, Runtime: runtime, Settings: settings, Wakeups: hub}
		sampleradminpb.RegisterSamplerAdminServer(server, &samplerrpc.SamplerAdmin{Sampler: *sampler})
		harnesshostpb.RegisterHarnessHostServer(server, &samplerrpc.HarnessHost{Sampler: sampler})
	}
	healthServer := health.NewServer()
	healthOnlyServer := newHealthOnlyServer(healthServer)
	healthListener, err := netlistener.Open(settings.HealthListenAddress())
	if err != nil {
		healthOnlyServer.Stop()
		return atStartupStage("grpc_health_listener", err)
	}
	defer healthListener.Close()
	credentials, err := settings.ListenTLS.Credentials(peertls.CentralSamplerURI)
	if err != nil {
		return atStartupStage("grpc_tls_credentials", err)
	}
	tlsOptions := append(append([]grpc.ServerOption(nil), serverOptions...), grpc.Creds(credentials))
	tlsServer := grpc.NewServer(tlsOptions...)
	register(tlsServer)
	healthpb.RegisterHealthServer(tlsServer, healthServer)
	tlsListener, err := netlistener.Open(settings.ListenTLS.Listen)
	if err != nil {
		tlsServer.Stop()
		return atStartupStage("grpc_tls_listener", err)
	}
	defer tlsListener.Close()
	running, runningCtx := errgroup.WithContext(ctx)
	running.Go(func() error { return runtime.Run(runningCtx, work) })
	if settings.HistoryTarget != "" {
		running.Go(func() error {
			return historyexport.Run(runningCtx, store, settings.HistoryTarget, settings.HistoryTokenFile, sessionstore.OutboxChannel, hub, settings.HistoryTLS)
		})
	}
	running.Go(func() error {
		err := healthOnlyServer.Serve(healthListener)
		if errors.Is(err, grpc.ErrServerStopped) {
			return nil
		}
		return err
	})
	running.Go(func() error {
		err := tlsServer.Serve(tlsListener)
		if errors.Is(err, grpc.ErrServerStopped) {
			return nil
		}
		return err
	})
	running.Go(func() error {
		<-runningCtx.Done()
		healthServer.Shutdown()
		for _, server := range []*grpc.Server{healthOnlyServer, tlsServer} {
			stopping, release := context.WithTimeout(context.Background(), 5*time.Second)
			graceful.Stop(stopping, server)
			release()
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
	if handled, err := runOwnershipCommand(ctx, os.Args[1:], os.Stdout); handled {
		if err != nil {
			fmt.Fprintln(os.Stderr, "central-sampler ownership:", err)
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
