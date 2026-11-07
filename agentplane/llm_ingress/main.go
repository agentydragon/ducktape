package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	modelconfigpb "github.com/agentydragon/ducktape/agentplane/llm_ingress/v1"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	authenticationclient "k8s.io/client-go/kubernetes/typed/authentication/v1"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

func main() {
	c, err := loadConfig()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	logger := configureLogging(c.LogLevels)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	shutdownTracing, err := observability.Init(ctx, "llm-ingress")
	if err != nil {
		logger.Error("LLM ingress tracing setup failed", "error", err)
		os.Exit(1)
	}
	err = serve(ctx, c, logger)
	shutdownTracing()
	if err != nil {
		logger.Error("LLM ingress stopped", "error", err)
		os.Exit(1)
	}
}

func serve(ctx context.Context, c config, logger *slog.Logger) error {
	kubeConfig, err := kubernetesConfig(c)
	if err != nil {
		return err
	}
	authentication, err := authenticationclient.NewForConfig(kubeConfig)
	if err != nil {
		return errors.New("could not create Kubernetes client")
	}
	resolver, err := auth.NewResolver(authentication.TokenReviews(), c.TokenAudience, c.AllowedServiceAccountNamespaces)
	if err != nil {
		return err
	}
	handler, err := newIngressHandler(c, resolver, logger)
	if err != nil {
		return err
	}
	defer handler.transport.CloseIdleConnections()
	address := net.JoinHostPort(c.Host, strconv.Itoa(c.Port))
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return fmt.Errorf("could not listen on configured address: %w", err)
	}
	listeners := []net.Listener{listener}
	defer func() {
		for _, opened := range listeners {
			_ = opened.Close()
		}
	}()
	grpcAddress := net.JoinHostPort(c.Host, strconv.Itoa(c.GRPCPort))
	grpcListener, err := net.Listen("tcp", grpcAddress)
	if err != nil {
		return fmt.Errorf("could not listen on configured gRPC address: %w", err)
	}
	listeners = append(listeners, grpcListener)
	grpcServer := newModelConfigServer(resolver, handler.models)
	servers := []*http.Server{{
		Handler:           observability.HTTPHandler(handler),
		ReadHeaderTimeout: 10 * time.Second,
	}}
	httpListeners := []net.Listener{listener}
	tlsServers := []bool{false}
	if c.WorkloadTLS != nil {
		svidResolver, err := auth.NewKubernetesSVIDResolver(kubeConfig, c.AllowedServiceAccountNamespaces)
		if err != nil {
			return fmt.Errorf("could not create workload SVID resolver: %w", err)
		}
		tlsConfig, err := c.WorkloadTLS.WorkloadServerTLSConfig(environment_runtime_transport.LLMIngressURI)
		if err != nil {
			return fmt.Errorf("could not configure workload SVID listener: %w", err)
		}
		workloadListener, err := net.Listen("tcp", c.WorkloadListenAddress)
		if err != nil {
			return fmt.Errorf("could not listen on workload SVID address: %w", err)
		}
		servers = append(servers, &http.Server{
			Handler:           observability.HTTPHandler(handler.WorkloadHandler(svidResolver)),
			ReadHeaderTimeout: 10 * time.Second,
			TLSConfig:         tlsConfig,
			ConnState:         environment_runtime_transport.HTTPServerTLSExpiryConnState(nil),
		})
		listeners = append(listeners, workloadListener)
		httpListeners = append(httpListeners, workloadListener)
		tlsServers = append(tlsServers, true)
	}
	serveResult := make(chan error, len(servers)+1)
	for index, server := range servers {
		httpListener := httpListeners[index]
		serveTLS := tlsServers[index]
		go func(server *http.Server, httpListener net.Listener, serveTLS bool) {
			if serveTLS {
				serveResult <- server.ServeTLS(httpListener, "", "")
				return
			}
			serveResult <- server.Serve(httpListener)
		}(server, httpListener, serveTLS)
	}
	go func() { serveResult <- fmt.Errorf("gRPC listener: %w", grpcServer.Serve(grpcListener)) }()
	logger.Info("forwarding authenticated Sandbox model traffic to LiteLLM", "url", c.LiteLLMURL, "address", address)
	logger.Info("serving native model configuration gRPC", "address", grpcAddress)
	if len(servers) > 1 {
		logger.Info("serving Sandbox SVID listener", "address", c.WorkloadListenAddress)
	}
	shutdown := func() error {
		shutdownContext, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		var shutdownErrors []error
		for _, server := range servers {
			if err := server.Shutdown(shutdownContext); err != nil {
				_ = server.Close()
				shutdownErrors = append(shutdownErrors, err)
			}
		}
		graceful.Stop(shutdownContext, grpcServer)
		return errors.Join(shutdownErrors...)
	}
	select {
	case serveErr := <-serveResult:
		shutdownErr := shutdown()
		if errors.Is(serveErr, http.ErrServerClosed) || errors.Is(serveErr, grpc.ErrServerStopped) {
			return shutdownErr
		}
		return errors.Join(serveErr, shutdownErr)
	case <-ctx.Done():
		if err := shutdown(); err != nil {
			return fmt.Errorf("servers failed to shut down: %w", err)
		}
		for range len(servers) + 1 {
			if err := <-serveResult; err != nil && !errors.Is(err, http.ErrServerClosed) && !errors.Is(err, grpc.ErrServerStopped) {
				return fmt.Errorf("listener stopped with an error: %w", err)
			}
		}
		return nil
	}
}

func newModelConfigServer(resolver *auth.Resolver, models map[string]modelConfig) *grpc.Server {
	methods := auth.HealthMethods()
	methods[modelconfigpb.ModelConfigService_GetModelConfig_FullMethodName] = auth.AnyAccount(resolver)
	server := grpc.NewServer(append(methods.ServerOptions(10*time.Second), observability.GRPCServerOption())...)
	modelconfigpb.RegisterModelConfigServiceServer(server, &modelConfigService{models: models})
	healthpb.RegisterHealthServer(server, health.NewServer())
	return server
}

func kubernetesConfig(c config) (*rest.Config, error) {
	var kubeconfig *rest.Config
	var err error
	if c.Kubeconfig != "" {
		kubeconfig, err = clientcmd.BuildConfigFromFlags("", c.Kubeconfig)
	} else {
		kubeconfig, err = rest.InClusterConfig()
	}
	if err != nil {
		return nil, errors.New("could not load Kubernetes client configuration")
	}
	kubeconfig.ContentType = "application/json"
	kubeconfig.AcceptContentTypes = "application/json"
	kubeconfig.WrapTransport = observability.WrapHTTPTransport(kubeconfig.WrapTransport)
	return kubeconfig, nil
}

func configureLogging(levels map[string]string) *slog.Logger {
	levelName, found := levels["agentplane.llm_ingress"]
	if !found {
		levelName, found = levels["root"]
	}
	level := slog.LevelInfo
	if found {
		switch levelName {
		case "DEBUG":
			level = slog.LevelDebug
		case "WARNING":
			level = slog.LevelWarn
		case "ERROR":
			level = slog.LevelError
		}
	}
	logger := slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: level}))
	slog.SetDefault(logger)
	return logger
}
