// Command history-service accepts Session journals through the ingress (agentplane/docs/runner_control.md § Event
// ingress), serves retained Session history, and folds it into Threads.
package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"net"
	"net/url"
	"os"
	"os/signal"
	"slices"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/api/ingress/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/ingress"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/migrations"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/reads"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/rpc"
	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
	"github.com/agentydragon/ducktape/agentplane/kubeclient"
	netlistener "github.com/agentydragon/ducktape/agentplane/net/listener"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/keepalive"
	"k8s.io/client-go/rest"
)

const (
	// shutdownGrace bounds how long open calls may finish once the process is told to stop.
	shutdownGrace = 5 * time.Second
	// Journal writers heartbeat on an idle call at the rate agentplane/docs/runner_control.md § Event ingress names, so the
	// ingress serves with the protocol's pacing.
	ingressIdleAck      = 10 * time.Second
	ingressSilenceLimit = 30 * time.Second
)

func kubernetesConfig(c config) (*rest.Config, error) {
	kubeConfig, err := kubeclient.Config(c.Kubeconfig)
	if err != nil {
		return nil, err
	}
	// Subject records are read live for each request; don't queue them behind client-go's small default burst.
	kubeConfig.QPS = -1
	return kubeConfig, nil
}

// readOnly is the database URL with sessions that refuse writes, so the read API cannot write.
func readOnly(databaseURL string) (string, error) {
	target, err := url.Parse(databaseURL)
	if err != nil {
		return "", errors.New("database_url is not a URL")
	}
	query := target.Query()
	if query.Has("options") {
		return "", errors.New("database_url must not set options")
	}
	query.Set("options", "-c default_transaction_read_only=on")
	target.RawQuery = query.Encode()
	return target.String(), nil
}

func openDatabase(ctx context.Context, databaseURL string, connections int) (*sql.DB, error) {
	db, err := postgres.Open(ctx, databaseURL, connections, connections/2)
	if err != nil {
		return nil, fmt.Errorf("History Service PostgreSQL unavailable: %w", err)
	}
	return db, nil
}

// run serves on the configured addresses, authenticating workload certificates against live Kubernetes records.
func run(ctx context.Context, c config) error {
	if err := migrate.Run(ctx, c.DatabaseURL, migrations.Up, migrate.WithMigrationTimeout(seconds(c.MigrationTimeoutSeconds))); err != nil {
		return err
	}
	rpcListener, err := netlistener.Open(c.RPC.ListenAddress)
	if err != nil {
		return err
	}
	defer rpcListener.Close()
	return serve(ctx, c, rpcListener)
}

// serve answers the read API and journal ingress on one native gRPC listener until ctx ends.
func serve(ctx context.Context, c config, rpcListener net.Listener) error {
	base, err := kubernetesConfig(c)
	if err != nil {
		return err
	}
	svids, err := auth.NewKubernetesSVIDResolver(base, c.Ingress.WriterNamespaces,
		&resolver.Identities{PolicyNamespace: c.PolicyNamespace})
	if err != nil {
		return err
	}
	// Schema initialization precedes the runtime pools; the read API's pool never writes.
	readURL, err := readOnly(c.DatabaseURL)
	if err != nil {
		return err
	}
	readDB, err := openDatabase(ctx, readURL, 6)
	if err != nil {
		return err
	}
	defer readDB.Close()
	foldDB, err := openDatabase(ctx, c.DatabaseURL, 4)
	if err != nil {
		return err
	}
	defer foldDB.Close()
	ingressDB, err := openDatabase(ctx, c.DatabaseURL, 12)
	if err != nil {
		return err
	}
	defer ingressDB.Close()
	hub, err := wakeups.Listen(ctx, c.DatabaseURL, storage.ChannelHistory, projection.ChannelSummaries, storage.ChannelSessions)
	if err != nil {
		return fmt.Errorf("History Service PostgreSQL listener unavailable: %w", err)
	}
	defer hub.Close()

	// The read store never appends, so it settles nothing.
	history := storage.NewStore(readDB, storage.Options{})
	service := rpc.NewService(rpc.Options{
		Reader:         &reads.Reader{DB: readDB, History: history, NativeFrameReadBatchSize: c.NativeFrameReadBatchSize},
		Wakeups:        hub,
		RequestTimeout: seconds(c.RequestTimeoutSeconds), WatchLease: seconds(c.WatchLeaseSeconds),
	})
	methods := auth.HealthMethods()
	// A watch or follow carries no deadline of its own, so the request timeout bounds its admission.
	historyMethods := rpc.Methods(svids)
	var identityReader auth.IdentityUIDReader
	if c.ServiceDelegation != nil || c.Ingress.SamplerServiceActor != "" {
		if identityReader, err = auth.NewKubernetesIdentityUIDs(base, c.PolicyNamespace); err != nil {
			return err
		}
	}
	if len(c.ReaderServiceCallers) > 0 {
		historyMethods, err = historyMethods.WithServiceCallers(rpc.ReadMethods(), c.ReaderServiceCallers)
		if err != nil {
			return fmt.Errorf("configure History service readers: %w", err)
		}
	}
	if c.ServiceDelegation != nil {
		historyMethods, err = historyMethods.WithServiceDelegation(
			c.ServiceDelegation.Methods, c.ServiceDelegation.Actors, identityReader,
		)
		if err != nil {
			return fmt.Errorf("configure History service delegation: %w", err)
		}
	}
	maps.Copy(methods, historyMethods)
	// Runner writes resolve a live workload SVID; central sampler writes use their exact service actor.
	pushRule := auth.AnySubject(svids)
	if c.Ingress.SamplerServiceActor != "" {
		if pushRule, err = pushRule.AllowServiceCallers([]string{c.Ingress.SamplerServiceActor}); err != nil {
			return fmt.Errorf("configure sampler service caller: %w", err)
		}
		if pushRule, err = pushRule.AllowServiceDelegationFrom(
			[]auth.ServiceDelegator{{SPIFFEID: c.Ingress.SamplerServiceActor}}, identityReader,
		); err != nil {
			return fmt.Errorf("configure sampler service delegation: %w", err)
		}
	}
	methods[session_ingresspb.SessionJournalIngress_Push_FullMethodName] = pushRule
	options := append(methods.ServerOptions(seconds(c.RequestTimeoutSeconds)), observability.GRPCServerOption(),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime: 30 * time.Second, PermitWithoutStream: false,
		}))
	security, err := c.RPC.ServerCredentialsWithServices(peertls.HistoryIngressURI)
	if err != nil {
		return err
	}
	options = append(options, grpc.Creds(security))
	server := grpc.NewServer(options...)
	historypb.RegisterHistoryServiceServer(server, service)
	healthServer := health.NewServer()
	healthpb.RegisterHealthServer(server, healthServer)
	ingressOptions := storage.Options{SettleDeltas: c.Ingress.SettleDeltas, CompactDeltas: c.Ingress.CompactDeltas, CompactExecutionEvidence: c.Ingress.CompactExecutionEvidence}
	if c.Ingress.EventBlockWrites {
		ingressOptions.EventPackers, err = c.Ingress.packers()
		if err != nil {
			return fmt.Errorf("configure History event blocks: %w", err)
		}
	}
	session_ingresspb.RegisterSessionJournalIngressServer(server, &ingress.Service{
		History:             storage.NewStore(ingressDB, ingressOptions),
		SamplerServiceActor: c.Ingress.SamplerServiceActor,
		Timing:              ingress.Timing{IdleAck: ingressIdleAck, SilenceLimit: ingressSilenceLimit},
	})
	changes, unsubscribe, err := hub.Subscribe(storage.ChannelHistory)
	if err != nil {
		return err
	}
	defer unsubscribe()
	foldContext, stopFold := context.WithCancel(context.WithoutCancel(ctx))
	defer stopFold()
	folded := make(chan struct{})
	go func() {
		defer close(folded)
		projection.NewFoldRunner(foldDB, history, c.FoldConcurrency, c.FoldPageSize).
			Run(foldContext, seconds(c.FoldIntervalSeconds), changes, healthServer)
	}()

	failed := make(chan error, 1)
	go func() { failed <- fmt.Errorf("gRPC listener: %w", server.Serve(rpcListener)) }()

	var result error
	select {
	case result = <-failed:
	case <-ctx.Done():
		result = ctx.Err()
	}
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_NOT_SERVING)
	// Open watches end with reconnect_required rather than holding the shutdown until their leases.
	service.Drain()
	// Let the fold hand back its leases while in-flight RPCs finish draining. Committed ingress history
	// remains available for the next fold even when it has not yet been projected.
	stopFold()
	stopping, release := context.WithTimeout(context.WithoutCancel(ctx), shutdownGrace)
	defer release()
	graceful.Stop(stopping, server)
	<-folded
	return result
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if len(os.Args) > 1 && os.Args[1] == "compact-history" {
		if err := runCompactHistory(ctx, os.Args[2:]); err != nil {
			slog.Error("History compaction stopped", "error", err)
			os.Exit(1)
		}
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "settle-history" {
		if err := runSettleHistory(ctx, os.Args[2:]); err != nil {
			slog.Error("History settlement backfill stopped", "error", err)
			os.Exit(1)
		}
		return
	}
	if slices.Contains(os.Args[1:], "--migrate-only") {
		c, err := loadConfig()
		if err != nil {
			slog.Error("invalid History Service configuration", "error", err)
			os.Exit(2)
		}
		_, err = migrate.RunOnly(ctx, os.Args[1:], settingPrefix+"DATABASE_URL", migrations.Up,
			migrate.WithMigrationTimeout(seconds(c.MigrationTimeoutSeconds)))
		if err != nil {
			slog.Error("History Service migration failed", "error", err)
			os.Exit(1)
		}
		return
	}
	c, err := loadConfig()
	if err != nil {
		slog.Error("invalid History Service configuration", "error", err)
		os.Exit(2)
	}
	shutdownTracing, err := observability.Init(ctx, "history-service")
	if err != nil {
		slog.Error("History Service tracing setup failed", "error", err)
		os.Exit(1)
	}
	err = run(ctx, c)
	shutdownTracing()
	if err != nil && !errors.Is(err, context.Canceled) {
		slog.Error("History Service stopped", "error", err)
		os.Exit(1)
	}
}
