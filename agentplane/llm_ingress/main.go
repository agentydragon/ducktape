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

	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
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
	defer listener.Close()
	server := &http.Server{
		Handler:           observability.HTTPHandler(handler),
		ReadHeaderTimeout: 10 * time.Second,
	}
	logger.Info("forwarding authenticated Sandbox model traffic to LiteLLM", "url", c.LiteLLMURL, "address", address)
	serveResult := make(chan error, 1)
	go func() { serveResult <- server.Serve(listener) }()
	select {
	case err := <-serveResult:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return fmt.Errorf("HTTP listener failed: %w", err)
	case <-ctx.Done():
		shutdownContext, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdownContext); err != nil {
			_ = server.Close()
			return errors.New("HTTP server shutdown exceeded five seconds")
		}
		if err := <-serveResult; !errors.Is(err, http.ErrServerClosed) && err != nil {
			return fmt.Errorf("HTTP listener stopped with an error: %w", err)
		}
		return nil
	}
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
