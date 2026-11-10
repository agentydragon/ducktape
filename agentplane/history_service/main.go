// Command history-service takes the Session journals runners push (agentplane/docs/runner_control.md § Event
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

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"github.com/agentydragon/ducktape/agentplane/history_service/api"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/history_service/ingress"
	"github.com/agentydragon/ducktape/agentplane/history_service/migrations"
	"github.com/agentydragon/ducktape/agentplane/history_service/projection"
	"github.com/agentydragon/ducktape/agentplane/history_service/reads"
	session_ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/session_ingresspb"
	"github.com/agentydragon/ducktape/agentplane/history_service/storage"
	"github.com/agentydragon/ducktape/agentplane/kubeclient"
	netlistener "github.com/agentydragon/ducktape/agentplane/net/listener"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	"github.com/agentydragon/ducktape/agentplane/postgres/migrate"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/keepalive"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

const (
	// shutdownGrace bounds how long open calls may finish once the process is told to stop.
	shutdownGrace = 5 * time.Second
	// A runner heartbeats an idle call at the rate agentplane/docs/runner_control.md § Event ingress names, so the
	// ingress serves with the protocol's pacing.
	ingressIdleAck      = 10 * time.Second
	ingressSilenceLimit = 30 * time.Second
)

func kubernetesConfig(c config) (*rest.Config, error) {
	kubeConfig, err := kubeclient.Config(c.Kubeconfig)
	if err != nil {
		return nil, err
	}
	// Every read and every push is a TokenReview; client-go's default of 5 requests per second (burst 10) would
	// queue them client-side. API Priority and Fairness bounds the load on the server.
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

func readerNamespaces(accounts []auth.Account, servedNamespaces []string, extra ...auth.Account) []string {
	namespaces := sets.New(servedNamespaces...)
	for _, account := range accounts {
		namespaces.Insert(account.Namespace)
	}
	for _, account := range extra {
		if account.Namespace != "" {
			namespaces.Insert(account.Namespace)
		}
	}
	return sets.List(namespaces)
}

// run serves on the configured addresses, authenticating callers with the cluster's TokenReview API.
func run(ctx context.Context, c config) error {
	if err := migrate.Run(ctx, c.DatabaseURL, migrations.Up, migrate.WithMigrationTimeout(seconds(c.MigrationTimeoutSeconds))); err != nil {
		return err
	}
	kubeConfig, err := kubernetesConfig(c)
	if err != nil {
		return err
	}
	kube, err := kubernetes.NewForConfig(kubeConfig)
	if err != nil {
		return err
	}
	rpcListener, err := netlistener.Open(c.RPC.ListenAddress)
	if err != nil {
		return err
	}
	defer rpcListener.Close()
	return serve(ctx, c, kube, rpcListener)
}

// serve answers the read API and runner ingress on one native gRPC listener until ctx ends.
func serve(ctx context.Context, c config, kube kubernetes.Interface, rpcListener net.Listener) error {
	var delegatedNamespaces []auth.Account
	if c.Delegation != nil {
		delegatedNamespaces = append(delegatedNamespaces, c.Delegation.Actor)
	}
	readerAuth, err := auth.NewResolver(kube.AuthenticationV1().TokenReviews(), c.TokenAudience,
		readerNamespaces(c.ReaderAccounts, c.Ingress.WriterNamespaces, delegatedNamespaces...))
	if err != nil {
		return err
	}
	writerAuth, err := auth.NewResolver(kube.AuthenticationV1().TokenReviews(), c.Ingress.TokenAudience, c.Ingress.WriterNamespaces)
	if err != nil {
		return err
	}
	// Reads remain bearer-only even when the shared transport also admits Sandbox certificates.
	readerAuth, err = readerAuth.WithListenerAuthentication(listener.Config{
		ListenAddress: c.RPC.ListenAddress, TLS: c.RPC.TLS,
		Auth: []listener.Authentication{&listener.KubernetesBearerAuth{Audiences: []string{c.TokenAudience}}},
	}, nil)
	if err != nil {
		return err
	}
	var svids *auth.SVIDResolver
	if c.RPC.Has(listener.SPIFFE) {
		base, err := kubernetesConfig(c)
		if err != nil {
			return err
		}
		svids, err = auth.NewKubernetesSVIDResolver(base, c.Ingress.WriterNamespaces, nil)
		if err != nil {
			return err
		}
	}
	writerAuth, err = writerAuth.WithListenerAuthentication(c.RPC, svids)
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
	history := storage.NewStore(readDB, false)
	service := api.NewService(api.Options{
		Reader:         &reads.Reader{DB: readDB, History: history, NativeFrameReadBatchSize: c.NativeFrameReadBatchSize},
		ReaderAccounts: c.ReaderAccounts,
		Wakeups:        hub,
		RequestTimeout: seconds(c.RequestTimeoutSeconds), WatchLease: seconds(c.WatchLeaseSeconds),
	})
	methods := auth.HealthMethods()
	// A watch or follow carries no deadline of its own, so the request timeout bounds its admission.
	historyMethods := api.Methods(readerAuth)
	if c.Delegation != nil {
		delegator := auth.Delegator{Account: c.Delegation.Actor, CallerNamespaces: c.Delegation.CallerNamespaces}
		uidReader := auth.ServiceAccountUIDReaderFunc(func(ctx context.Context, account auth.Account) (string, bool, error) {
			serviceAccount, err := kube.CoreV1().ServiceAccounts(account.Namespace).Get(ctx, account.Name, metav1.GetOptions{})
			if apierrors.IsNotFound(err) {
				return "", false, nil
			}
			if err != nil {
				return "", false, err
			}
			return string(serviceAccount.UID), true, nil
		})
		historyMethods, err = api.MethodsWithDelegation(readerAuth, delegator, uidReader, c.Delegation.Methods)
		if err != nil {
			return fmt.Errorf("configure History delegation: %w", err)
		}
	}
	maps.Copy(methods, historyMethods)
	pushRule := auth.AnyAccount(writerAuth)
	if c.RPC.Has(listener.SPIFFE) {
		pushRule = auth.SVIDOrAccounts(writerAuth, c.Ingress.SamplerAccounts)
	}
	methods[session_ingresspb.SessionJournalIngress_Push_FullMethodName] = pushRule
	options := append(methods.ServerOptions(seconds(c.RequestTimeoutSeconds)), observability.GRPCServerOption(),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime: 30 * time.Second, PermitWithoutStream: false,
		}))
	security, err := c.RPC.ServerCredentials(environment_runtime_transport.HistoryIngressURI)
	if err != nil {
		return err
	}
	if security != nil {
		options = append(options, grpc.Creds(security))
	}
	server := grpc.NewServer(options...)
	historypb.RegisterHistoryServiceServer(server, service)
	healthServer := health.NewServer()
	healthpb.RegisterHealthServer(server, healthServer)
	session_ingresspb.RegisterSessionJournalIngressServer(server, &ingress.Service{
		SandboxUIDs:     ingress.NewServiceAccountSandboxUIDResolver(kube),
		History:         storage.NewStore(ingressDB, c.Ingress.SettleDeltas),
		SamplerAccounts: c.Ingress.SamplerAccounts,
		Timing:          ingress.Timing{IdleAck: ingressIdleAck, SilenceLimit: ingressSilenceLimit},
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
	if slices.Contains(os.Args[1:], "--migrate-only") {
		timeout, err := migrationOnlyTimeout()
		if err != nil {
			slog.Error("invalid History Service migration configuration", "error", err)
			os.Exit(2)
		}
		_, err = migrate.RunOnly(ctx, os.Args[1:], "AGENTPLANE_HISTORY_SERVICE_DATABASE_URL", migrations.Up, migrate.WithMigrationTimeout(timeout))
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
