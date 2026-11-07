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
	"google.golang.org/grpc/credentials"
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

type proxyConnectionTransport interface {
	dial(context.Context, string) (net.Conn, error)
}

type plaintextProxyTransport struct{}

func (plaintextProxyTransport) dial(ctx context.Context, address string) (net.Conn, error) {
	return (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, "tcp", address)
}

type tlsProxyTransport struct {
	security        *tls.Config
	grpcCredentials credentials.TransportCredentials
}

func (transport tlsProxyTransport) dial(ctx context.Context, address string) (net.Conn, error) {
	connection, err := (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, "tcp", address)
	if err != nil {
		return nil, err
	}
	tlsConnection, err := environment_runtime_transport.ClientTLSConnection(ctx, connection, transport.security)
	if err != nil {
		_ = connection.Close()
		return nil, err
	}
	return tlsConnection, nil
}

type proxyHop struct {
	address   string
	auth      proxyAuth
	transport proxyConnectionTransport
}

func newProxyHop(config proxyConfig) (*proxyHop, error) {
	if err := config.validate(); err != nil {
		return nil, err
	}
	auth := config.Transport.auth()
	if _, err := auth.requestToken(); err != nil {
		return nil, fmt.Errorf("load proxy hop credential: %w", err)
	}
	var transport proxyConnectionTransport
	switch configured := config.Transport.(type) {
	case plaintextProxyTransportConfig:
		transport = plaintextProxyTransport{}
	case tlsProxyTransportConfig:
		var security *tls.Config
		var grpcCredentials credentials.TransportCredentials
		switch configuredAuth := configured.Auth.(type) {
		case spiffeProxyAuth:
			client := environment_runtime_transport.Config{
				CertFile: configuredAuth.CertFile,
				KeyFile:  configuredAuth.KeyFile,
				CAFile:   configured.CAFile,
			}
			var err error
			security, err = client.ClientTLSConfig(environment_runtime_transport.EgressProxyURI)
			if err != nil {
				return nil, fmt.Errorf("configure SPIFFE proxy authentication: %w", err)
			}
			grpcCredentials, err = client.ClientCredentials(environment_runtime_transport.EgressProxyURI)
			if err != nil {
				return nil, fmt.Errorf("configure SPIFFE gRPC authentication: %w", err)
			}
		case kubernetesBearerProxyAuth:
			var err error
			security, err = (environment_runtime_transport.Config{CAFile: configured.CAFile}).ProxyServerTLSConfig()
			if err != nil {
				return nil, fmt.Errorf("configure bearer proxy TLS: %w", err)
			}
		default:
			return nil, fmt.Errorf("unsupported proxy authentication type %T", configuredAuth)
		}
		transport = tlsProxyTransport{security: security, grpcCredentials: grpcCredentials}
	default:
		return nil, fmt.Errorf("unsupported proxy transport type %T", configured)
	}
	return &proxyHop{address: config.Address, auth: auth, transport: transport}, nil
}
