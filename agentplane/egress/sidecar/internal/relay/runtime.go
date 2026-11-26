package relay

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

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/observability"
)

// Run loads the sidecar configuration, initializes its transports, and serves until shutdown.
// Its exit codes are consumed by the process wrapper in cmd/main.go.
func Run() int {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo})))
	c, err := loadConfig(os.Getenv(configFileEnv))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	s, err := newSidecar(c)
	if err != nil {
		slog.Error("egress sidecar transport setup failed", "error", err)
		return 1
	}
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	shutdownTracing, err := observability.Init(ctx, "egress-sidecar")
	if err != nil {
		slog.Error("egress sidecar tracing setup failed", "error", err)
		return 1
	}
	err = s.run(ctx)
	shutdownTracing()
	if err != nil {
		slog.Error("egress sidecar stopped", "error", err)
		return 1
	}
	return 0
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
	svid     peertls.Config
}

func (transport tlsProxyTransport) dial(ctx context.Context, address string) (net.Conn, error) {
	dialContext, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	connection, err := (&net.Dialer{}).DialContext(dialContext, "tcp", address)
	if err != nil {
		return nil, err
	}
	tlsConnection, err := peertls.ClientTLSConnection(dialContext, connection, transport.security)
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
	security, err := config.SVID.ClientTLSConfig(peertls.EgressProxyURI)
	if err != nil {
		return nil, fmt.Errorf("configure SPIFFE proxy authentication: %w", err)
	}
	return &proxyHop{address: config.Address, transport: tlsProxyTransport{security: security, svid: config.SVID}}, nil
}
