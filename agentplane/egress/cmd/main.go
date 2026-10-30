// Command egress-proxy is the central egress proxy: it authenticates sandbox workloads, enforces the
// egress policy the cluster holds, substitutes credentials, and records every decision.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/admin"
	"github.com/agentydragon/ducktape/agentplane/egress/docs"
	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/informer"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/proxy"
	"github.com/agentydragon/ducktape/agentplane/egress/upstream"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/agentydragon/ducktape/util/netlistener"
	"golang.org/x/sync/errgroup"
	authenticationv1 "k8s.io/client-go/kubernetes/typed/authentication/v1"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

const (
	// drainTimeout bounds how long admitted requests may finish after a termination signal, inside
	// the Pod's termination grace.
	drainTimeout = 20 * time.Second
	// decisionFlushTimeout bounds how long shutdown waits for queued decisions to be written.
	decisionFlushTimeout = 5 * time.Second
	// changesHeartbeat is how often an idle decision change stream proves it is alive.
	changesHeartbeat  = 15 * time.Second
	readHeaderTimeout = 30 * time.Second
)

func main() {
	c, err := loadConfig()
	if err != nil {
		fmt.Fprintln(os.Stderr, "egress-proxy:", err)
		os.Exit(2)
	}
	level, err := logLevel(c.LogLevels["root"])
	if err != nil {
		fmt.Fprintln(os.Stderr, "egress-proxy:", err)
		os.Exit(2)
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: level})))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	shutdownTracing, err := observability.Init(ctx, "egress-proxy")
	if err != nil {
		slog.Error("egress-proxy tracing setup failed", "error", err)
		os.Exit(1)
	}
	err = run(ctx, c)
	shutdownTracing()
	if err != nil {
		slog.Error("egress-proxy stopped", "error", err)
		os.Exit(1)
	}
}

// logLevel maps a Python-style level name; the empty name is the default, INFO.
func logLevel(name string) (slog.Level, error) {
	switch name {
	case "", "INFO":
		return slog.LevelInfo, nil
	case "DEBUG":
		return slog.LevelDebug, nil
	case "WARNING":
		return slog.LevelWarn, nil
	case "ERROR":
		return slog.LevelError, nil
	}
	return 0, errors.New("log_levels values must be DEBUG, INFO, WARNING, or ERROR")
}

func kubernetesConfig(kubeconfig string) (*rest.Config, error) {
	var base *rest.Config
	var err error
	if kubeconfig != "" {
		base, err = clientcmd.BuildConfigFromFlags("", kubeconfig)
	} else {
		base, err = rest.InClusterConfig()
	}
	if err != nil {
		return nil, fmt.Errorf("load Kubernetes client configuration: %w", err)
	}
	base.WrapTransport = observability.WrapHTTPTransport(base.WrapTransport)
	base.ContentType, base.AcceptContentTypes = "application/json", "application/json"
	// Every proxied request is a TokenReview; client-go's default of 5 requests per second (burst 10)
	// would queue admissions client-side. API Priority and Fairness bounds the load on the server.
	base.QPS = -1
	return base, nil
}

// workloadIdentity reviews the hop bearer for the proxy's own audience and each projected token for
// its audience, accepting only ServiceAccounts of the allowed namespaces.
func workloadIdentity(reviews auth.TokenReviews, c config) (*identity.Verifier, error) {
	hop, err := auth.NewResolver(reviews, c.TokenAudience, c.AllowedServiceAccountNamespaces)
	if err != nil {
		return nil, err
	}
	projected := make(map[string]*auth.Resolver, len(c.ProjectedTokenAudiences))
	for _, audience := range c.ProjectedTokenAudiences {
		if projected[audience], err = auth.NewResolver(reviews, audience, c.AllowedServiceAccountNamespaces); err != nil {
			return nil, err
		}
	}
	return identity.NewVerifier(hop, projected), nil
}

// upstreamRoots is the bundle destinations are verified against: the file when given, else the
// system roots.
func upstreamRoots(path string) (*x509.CertPool, error) {
	if path == "" {
		return x509.SystemCertPool()
	}
	bundle, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("upstream_ca_file: %w", err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(bundle) {
		return nil, errors.New("upstream_ca_file holds no certificate")
	}
	return roots, nil
}

func openListener(name, address string) (net.Listener, error) {
	listener, err := netlistener.Open(address)
	if err != nil {
		return nil, fmt.Errorf("%s listener: %w", name, err)
	}
	slog.Info("listening", "listener", name, "address", listener.Addr().String())
	return listener, nil
}

// serving treats a server shut down on purpose as done and anything else as the failure it is.
func serving(name string, err error) error {
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return fmt.Errorf("%s listener: %w", name, err)
}

func run(ctx context.Context, c config) error {
	proxyListener, err := openListener("proxy", c.ListenAddress)
	if err != nil {
		return err
	}
	defer proxyListener.Close()
	agentAPIListener, err := openListener("agent API", c.AgentAPIListenAddress)
	if err != nil {
		return err
	}
	defer agentAPIListener.Close()
	adminListener, err := openListener("admin", c.AdminListenAddress)
	if err != nil {
		return err
	}
	defer adminListener.Close()
	return serve(ctx, c, proxyListener, agentAPIListener, adminListener)
}

func serve(ctx context.Context, c config, proxyListener, agentAPIListener, adminListener net.Listener) error {
	base, err := kubernetesConfig(c.Kubeconfig)
	if err != nil {
		return err
	}
	reviews, err := authenticationv1.NewForConfig(base)
	if err != nil {
		return err
	}
	verifier, err := workloadIdentity(reviews.TokenReviews(), c)
	if err != nil {
		return err
	}
	ca, err := tls.LoadX509KeyPair(c.CACert, c.CAKey)
	if err != nil {
		return fmt.Errorf("interception CA: %w", err)
	}
	roots, err := upstreamRoots(c.UpstreamCAFile)
	if err != nil {
		return err
	}

	index := policy.NewIndex()
	watcher, err := informer.New(base, informer.Config{
		Index: index, RulesNamespace: c.RulesNamespace, CredentialsNamespace: c.CredentialsNamespace,
	})
	if err != nil {
		return err
	}

	db, err := history.Open(c.DatabaseURL)
	if err != nil {
		return err
	}
	defer db.Close()
	store, err := history.NewStore(db, time.Duration(c.DecisionRetentionDays)*24*time.Hour, c.DecisionHistorySize)
	if err != nil {
		return err
	}
	decisions, err := history.NewLog(store, history.LogConfig{
		QueueSize: c.DecisionQueueSize, BatchSize: c.DecisionBatchSize, FlushTimeout: decisionFlushTimeout,
	})
	if err != nil {
		return err
	}
	defer decisions.Close()
	changes, err := history.NewChanges(c.DatabaseURL, changesHeartbeat)
	if err != nil {
		return err
	}

	egress, err := proxy.New(proxy.Options{
		CA: ca, UpstreamRoots: roots, Identity: verifier, Policy: index,
		Upstream: upstream.NewResolver(c.ExemptNetworks), Record: decisions.Record,
	})
	if err != nil {
		return err
	}
	agentDocs, err := fs.Sub(docs.Agents, "agents")
	if err != nil {
		return err
	}

	// The facade outlives the termination signal so that reads admitted before it finish while draining.
	facadeContext, stopFacade := context.WithCancel(context.WithoutCancel(ctx))
	defer stopFacade()
	agentAPIServer, err := newAgentAPI(facadeContext, agentAPIListener.Addr().String(), verifier, index, agentDocs, docs.OpenAPI)
	if err != nil {
		return err
	}
	adminServer := &http.Server{Handler: http.NotFoundHandler(), ReadHeaderTimeout: readHeaderTimeout}
	adminShutdown, err := mountAdminGRPC(adminServer, c, reviews.TokenReviews(), admin.ServiceOptions{
		Decisions: store, Changes: changes, HistoryHealth: decisions, Index: index.Health,
	})
	if err != nil {
		return err
	}

	group, ctx := errgroup.WithContext(ctx)
	group.Go(func() error { watcher.Run(ctx); return nil })
	group.Go(func() error { return serving("proxy", egress.Serve(proxyListener)) })
	group.Go(func() error { return serving("agent API", agentAPIServer.Serve(agentAPIListener)) })
	// The admin listener is the readiness probe and readiness gates every Service this Pod backs, so
	// it starts accepting last: the readiness check answering means the listeners above are serving.
	group.Go(func() error { return serving("admin", adminServer.Serve(adminListener)) })
	group.Go(func() error {
		<-ctx.Done()
		return drain(egress, index, agentAPIServer, adminShutdown)
	})
	return group.Wait()
}

// shutdowner is a server that stops accepting and lets admitted requests finish until ctx ends.
type shutdowner interface {
	Shutdown(ctx context.Context) error
}

// drain marks the proxy unready, lets admitted requests finish, then stops the other listeners. The
// admin listener outlives the proxy's drain so the kubelet can see the Pod go unready.
func drain(egress *proxy.Proxy, index *policy.Index, agentAPIServer, adminServer shutdowner) error {
	slog.Info("draining")
	index.Health.MarkDraining()
	ctx, cancel := context.WithTimeout(context.Background(), drainTimeout)
	defer cancel()
	if err := egress.Drain(ctx); err != nil {
		slog.Warn("drain deadline reached; closing remaining connections", "error", err)
	}
	return errors.Join(agentAPIServer.Shutdown(ctx), adminServer.Shutdown(ctx))
}
