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

	"github.com/agentydragon/ducktape/agentplane/postgres/go"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	centralsampler "github.com/agentydragon/ducktape/agentplane/x/central_sampler"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"github.com/agentydragon/ducktape/util/netlistener"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

func run(ctx context.Context, settings centralsampler.Settings, initialize bool) error {
	databaseURL := strings.Replace(settings.DatabaseURL, "postgresql+asyncpg://", "postgresql://", 1)
	// Allow session state writes and lease renewals, eight exporter workers, and sixteen RPCs.
	db, err := postgres.Open(ctx, databaseURL, 2*settings.MaxLiveSessions+8+16, 16)
	if err != nil {
		return err
	}
	defer db.Close()
	if initialize {
		modelAPIs := make(map[string]centralsampler.APIStyle, len(settings.Models))
		for model, config := range settings.Models {
			modelAPIs[model] = config.API
		}
		return centralsampler.Migrate(ctx, db, modelAPIs)
	}
	if err := os.MkdirAll(settings.Logs, 0700); err != nil {
		return err
	}
	var kubeConfig *rest.Config
	if settings.Kubeconfig != "" {
		kubeConfig, err = clientcmd.BuildConfigFromFlags("", settings.Kubeconfig)
	} else {
		kubeConfig, err = rest.InClusterConfig()
	}
	if err != nil {
		return err
	}
	kubeConfig.QPS = -1
	kubeConfig.ContentType = "application/json"
	client, err := kubernetes.NewForConfig(kubeConfig)
	if err != nil {
		return err
	}
	namespaces := make([]string, 0, len(settings.AllowedAccounts))
	for _, account := range settings.AllowedAccounts {
		namespace, _, _ := strings.Cut(account, "/")
		namespaces = append(namespaces, namespace)
	}
	resolver, err := auth.NewResolver(client.AuthenticationV1().TokenReviews(), settings.Audience, namespaces)
	if err != nil {
		return err
	}
	store := centralsampler.New(db)
	hub, err := wakeups.Listen(ctx, databaseURL, centralsampler.OutboxChannel)
	if err != nil {
		return err
	}
	defer hub.Close()
	runtime := centralsampler.NewRuntime(store, settings)
	server := grpc.NewServer(grpc.MaxRecvMsgSize(64*1024*1024), grpc.MaxSendMsgSize(64*1024*1024))
	samplerpb.RegisterSamplerServer(server, &centralsampler.Sampler{Store: store, Runtime: runtime, Resolver: resolver, Settings: settings, Wakeups: hub})
	healthServer := health.NewServer()
	healthpb.RegisterHealthServer(server, healthServer)
	listener, err := netlistener.Open(settings.Listen)
	if err != nil {
		return err
	}
	defer listener.Close()
	running, runningCtx := errgroup.WithContext(ctx)
	running.Go(func() error { return runtime.Run(runningCtx) })
	if settings.HistoryTarget != "" {
		running.Go(func() error {
			return centralsampler.RunExporter(runningCtx, store, settings.HistoryTarget, settings.HistoryTokenFile, hub)
		})
	}
	running.Go(func() error {
		err := server.Serve(listener)
		if errors.Is(err, grpc.ErrServerStopped) {
			return nil
		}
		return err
	})
	running.Go(func() error {
		<-runningCtx.Done()
		healthServer.Shutdown()
		stopped := make(chan struct{})
		go func() { server.GracefulStop(); close(stopped) }()
		timer := time.NewTimer(5 * time.Second)
		defer timer.Stop()
		select {
		case <-stopped:
		case <-timer.C:
			server.Stop()
			<-stopped
		}
		return nil
	})
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	err = running.Wait()
	if errors.Is(err, context.Canceled) && ctx.Err() != nil {
		return nil
	}
	return err
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	configuration := flag.String("config", os.Getenv("AGENTPLANE_CENTRAL_SAMPLER_CONFIG"), "JSON settings path")
	initialize := flag.Bool("initialize", false, "Apply isolated sampler schema migrations")
	flag.Parse()
	settings, err := centralsampler.LoadSettings(*configuration)
	if err == nil {
		ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer cancel()
		err = run(ctx, settings, *initialize)
	}
	if err != nil {
		slog.Error("central sampler stopped", "error_type", fmt.Sprintf("%T", err))
		os.Exit(1)
	}
}
