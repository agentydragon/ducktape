package environment_runtime_transport

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"syscall"
	"time"

	"google.golang.org/grpc/credentials"
)

// expiringCredentials delegates TLS to gRPC while retaining the local leaf
// selected by this handshake. Each side independently enforces both expiries.
type expiringCredentials struct {
	config                      *tls.Config
	server                      bool
	allowMissingPeerCertificate bool
}

func newExpiringCredentials(config *tls.Config, server bool) credentials.TransportCredentials {
	return &expiringCredentials{config: config.Clone(), server: server}
}

// ServerCredentials adapts a TLS configuration from the shared listener
// builder to native gRPC credentials and closes each connection when either
// presented certificate expires. The builder selects the local certificate
// through GetCertificate so the wrapper can track its expiry. Server
// configurations that intentionally admit a bearer-only TLS call may omit a
// peer certificate; their local certificate is still bounded by its expiry.
func ServerCredentials(config *tls.Config) (credentials.TransportCredentials, error) {
	if config == nil {
		return nil, errors.New("server TLS configuration is required")
	}
	if config.GetCertificate == nil {
		return nil, errors.New("server TLS configuration must select its local certificate with GetCertificate")
	}
	allowMissingPeerCertificate := config.ClientAuth == tls.NoClientCert ||
		config.ClientAuth == tls.RequestClientCert || config.ClientAuth == tls.VerifyClientCertIfGiven
	return &expiringCredentials{
		config: config.Clone(), server: true, allowMissingPeerCertificate: allowMissingPeerCertificate,
	}, nil
}

func (c *expiringCredentials) Info() credentials.ProtocolInfo {
	return credentials.NewTLS(c.config).Info()
}

func (c *expiringCredentials) Clone() credentials.TransportCredentials {
	return &expiringCredentials{
		config: c.config.Clone(), server: c.server, allowMissingPeerCertificate: c.allowMissingPeerCertificate,
	}
}

func (c *expiringCredentials) OverrideServerName(name string) error {
	c.config.ServerName = name
	return nil
}

func (c *expiringCredentials) ClientHandshake(ctx context.Context, authority string, raw net.Conn) (net.Conn, credentials.AuthInfo, error) {
	config, selected := c.handshakeConfig()
	conn, info, err := credentials.NewTLS(config).ClientHandshake(ctx, authority, raw)
	return expireConnection(conn, info, selected, false, err)
}

func (c *expiringCredentials) ServerHandshake(raw net.Conn) (net.Conn, credentials.AuthInfo, error) {
	config, selected := c.handshakeConfig()
	conn, info, err := credentials.NewTLS(config).ServerHandshake(raw)
	return expireConnection(conn, info, selected, c.allowMissingPeerCertificate, err)
}

// The go-spiffe mTLS configurations always select the local certificate via
// these callbacks. A separate config per handshake avoids mixing rotations
// across concurrent connections.
func (c *expiringCredentials) handshakeConfig() (*tls.Config, **tls.Certificate) {
	config := c.config.Clone()
	var selected *tls.Certificate
	if c.server {
		get := config.GetCertificate
		config.GetCertificate = func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
			certificate, err := get(hello)
			selected = certificate
			return certificate, err
		}
	} else {
		get := config.GetClientCertificate
		config.GetClientCertificate = func(request *tls.CertificateRequestInfo) (*tls.Certificate, error) {
			certificate, err := get(request)
			selected = certificate
			return certificate, err
		}
	}
	return config, &selected
}

func expireConnection(conn net.Conn, auth credentials.AuthInfo, selected **tls.Certificate, allowMissingPeerCertificate bool, err error) (net.Conn, credentials.AuthInfo, error) {
	if err != nil {
		return nil, nil, err
	}
	fail := func(err error) (net.Conn, credentials.AuthInfo, error) {
		_ = conn.Close()
		return nil, nil, err
	}
	if *selected == nil || len((*selected).Certificate) == 0 {
		return fail(errors.New("TLS handshake did not select a local certificate"))
	}
	leaf := (*selected).Leaf
	if leaf == nil {
		leaf, err = x509.ParseCertificate((*selected).Certificate[0])
		if err != nil {
			return fail(fmt.Errorf("parse local TLS certificate: %w", err))
		}
	}
	info, ok := auth.(credentials.TLSInfo)
	if !ok || !allowMissingPeerCertificate && len(info.State.PeerCertificates) == 0 {
		return fail(errors.New("TLS handshake did not authenticate a peer certificate"))
	}
	expiry := leaf.NotAfter
	if len(info.State.PeerCertificates) > 0 {
		if peerExpiry := info.State.PeerCertificates[0].NotAfter; peerExpiry.Before(expiry) {
			expiry = peerExpiry
		}
	}
	if !expiry.After(time.Now()) {
		return fail(fmt.Errorf("TLS certificate expired at %s", expiry.UTC().Format(time.RFC3339Nano)))
	}
	wrapped := &expiryConn{Conn: conn}
	wrapped.timer = time.AfterFunc(time.Until(expiry), func() { _ = conn.Close() })
	if socket, ok := conn.(syscall.Conn); ok {
		return &expirySyscallConn{expiryConn: wrapped, socket: socket}, auth, nil
	}
	return wrapped, auth, nil
}

type expiryConn struct {
	net.Conn
	timer *time.Timer
}

func (c *expiryConn) Close() error {
	c.timer.Stop()
	return c.Conn.Close()
}

type expirySyscallConn struct {
	*expiryConn
	socket syscall.Conn
}

func (c *expirySyscallConn) SyscallConn() (syscall.RawConn, error) {
	return c.socket.SyscallConn()
}
