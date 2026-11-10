package main

import (
	"context"
	"crypto/tls"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

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
	hop, err := newProxyHop(c.Proxy)
	if err != nil {
		return nil, err
	}
	return &sidecar{config: c, proxyHop: hop}, nil
}

// tlsProxyTransport dials the central proxy presenting the workload SVID.
type tlsProxyTransport struct {
	security *tls.Config
	svid     environment_runtime_transport.Config
}

func (transport tlsProxyTransport) dial(ctx context.Context, address string) (net.Conn, error) {
	dialContext, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	connection, err := (&net.Dialer{}).DialContext(dialContext, "tcp", address)
	if err != nil {
		return nil, err
	}
	tlsConnection, err := environment_runtime_transport.ClientTLSConnection(dialContext, connection, transport.security)
	if err != nil {
		_ = connection.Close()
		return nil, err
	}
	return tlsConnection, nil
}

type proxyHop struct {
	address   string
	transport tlsProxyTransport
}

func newProxyHop(config proxyConfig) (*proxyHop, error) {
	client := environment_runtime_transport.Config{
		CertFile: config.Transport.Auth.CertFile,
		KeyFile:  config.Transport.Auth.KeyFile,
		CAFile:   config.Transport.CAFile,
	}
	security, err := client.ClientTLSConfig(environment_runtime_transport.EgressProxyURI)
	if err != nil {
		return nil, fmt.Errorf("configure SPIFFE proxy authentication: %w", err)
	}
	return &proxyHop{address: config.Address, transport: tlsProxyTransport{security: security, svid: client}}, nil
}
