package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/observability"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo})))
	c, err := loadConfig(os.Getenv)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	s, err := newSidecar(c)
	if err != nil {
		slog.Error("egress sidecar transport setup failed", "error", err)
		os.Exit(1)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	shutdownTracing, err := observability.Init(ctx, "egress-sidecar")
	if err != nil {
		slog.Error("egress sidecar tracing setup failed", "error", err)
		os.Exit(1)
	}
	err = s.run(ctx)
	shutdownTracing()
	if err != nil {
		slog.Error("egress sidecar stopped", "error", err)
		os.Exit(1)
	}
}

func newSidecar(c config) (*sidecar, error) {
	if err := c.validate(); err != nil {
		return nil, err
	}
	s := &sidecar{config: c}
	if c.ProxyMode == "mtls" {
		tlsConfig, err := (environment_runtime_transport.Config{
			CertFile: c.ProxyTLS.CertFile,
			KeyFile:  c.ProxyTLS.KeyFile,
			CAFile:   c.ProxyTLS.CAFile,
		}).ClientTLSConfig(environment_runtime_transport.EgressProxyURI)
		if err != nil {
			return nil, fmt.Errorf("configure central proxy mTLS: %w", err)
		}
		s.proxyTLS = tlsConfig
	}
	return s, nil
}
