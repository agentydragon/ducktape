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
	"strings"
	"syscall"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/grpc/graceful"
	"github.com/agentydragon/ducktape/agentplane/kubeclient"
	modelconfigpb "github.com/agentydragon/ducktape/agentplane/llm_ingress/v1"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	authlistener "github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	authenticationclient "k8s.io/client-go/kubernetes/typed/authentication/v1"
	"k8s.io/client-go/rest"
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
	callers, err := auth.NewResolver(authentication.TokenReviews(), c.TokenAudience, c.AllowedServiceAccountNamespaces)
	if err != nil {
		return err
	}
	var svidResolver *auth.SVIDResolver
	if c.RPC.Has(authlistener.SPIFFE) {
		var identities *resolver.Identities
		if c.PolicyNamespace != "" {
			identities = &resolver.Identities{PolicyNamespace: c.PolicyNamespace}
		}
		svidResolver, err = auth.NewKubernetesSVIDResolver(kubeConfig, c.AllowedServiceAccountNamespaces, identities)
		if err != nil {
			return err
		}
	}
	callers, err = callers.WithListenerAuthentication(c.RPC, svidResolver)
	if err != nil {
		return err
	}
	handler, err := newIngressHandler(c, callers, logger)
	if err != nil {
		return err
	}
	defer handler.transport.CloseIdleConnections()
	tlsConfig, err := c.RPC.TLSConfig(environment_runtime_transport.LLMIngressURI)
	if err != nil {
		return err
	}
	if tlsConfig == nil {
		return errors.New("rpc.tls is required")
	}
	connState, err := environment_runtime_transport.ConfigureHTTPServerTLSExpiry(tlsConfig)
	if err != nil {
		return err
	}
	tlsConfig.NextProtos = []string{"h2", "http/1.1"}
	listener, err := net.Listen("tcp", c.RPC.ListenAddress)
	if err != nil {
		return err
	}
	defer listener.Close()
	models := make(map[string]modelConfig, len(c.Models))
	for _, model := range c.Models {
		models[model.Model] = model
	}
	grpcServer := newModelConfigServer(callers, &modelConfigService{models: models})
	server := &http.Server{
		Handler:           observability.HTTPHandler(rpcHandler(grpcServer, handler)),
		ReadHeaderTimeout: 10 * time.Second,
		TLSConfig:         tlsConfig,
		ConnState:         connState,
	}
	stopped := make(chan error, 1)
	go func() { stopped <- server.ServeTLS(listener, "", "") }()
	logger.Info("serving authenticated model inference and configuration", "address", c.RPC.ListenAddress)
	shutdown := func() error {
		shutdownContext, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		err := server.Shutdown(shutdownContext)
		if err != nil {
			_ = server.Close()
		}
		graceful.Stop(shutdownContext, grpcServer)
		return err
	}
	select {
	case err := <-stopped:
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		return errors.Join(err, shutdown())
	case <-ctx.Done():
		if err := shutdown(); err != nil {
			return err
		}
		err := <-stopped
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}

func rpcHandler(grpcServer *grpc.Server, httpHandler http.Handler) http.Handler {
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.ProtoMajor == 2 && strings.HasPrefix(request.Header.Get("Content-Type"), "application/grpc") {
			grpcServer.ServeHTTP(writer, request)
			return
		}
		httpHandler.ServeHTTP(writer, request)
	})
}

// newModelConfigServer serves budgets to any caller the resolver authenticates; the handler never
// reads the caller, so an Identity caller is served as a ServiceAccount one is.
func newModelConfigServer(callers *auth.Resolver, service modelconfigpb.ModelConfigServiceServer) *grpc.Server {
	methods := auth.HealthMethods()
	methods[modelconfigpb.ModelConfigService_GetModelConfig_FullMethodName] = auth.AnySubject(callers)
	server := grpc.NewServer(append(methods.ServerOptions(10*time.Second), observability.GRPCServerOption())...)
	modelconfigpb.RegisterModelConfigServiceServer(server, service)
	healthpb.RegisterHealthServer(server, health.NewServer())
	return server
}

func kubernetesConfig(c config) (*rest.Config, error) {
	kubeconfig, err := kubeclient.Config(c.Kubeconfig)
	if err != nil {
		return nil, errors.New("could not load Kubernetes client configuration")
	}
	kubeconfig.ContentType = "application/json"
	kubeconfig.AcceptContentTypes = "application/json"
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
