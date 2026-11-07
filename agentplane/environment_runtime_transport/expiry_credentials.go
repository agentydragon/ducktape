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
	config *tls.Config
	server bool
}

func newExpiringCredentials(config *tls.Config, server bool) credentials.TransportCredentials {
	return &expiringCredentials{config: config.Clone(), server: server}
}

func (c *expiringCredentials) Info() credentials.ProtocolInfo {
	return credentials.NewTLS(c.config).Info()
}

func (c *expiringCredentials) Clone() credentials.TransportCredentials {
	return newExpiringCredentials(c.config, c.server)
}

func (c *expiringCredentials) OverrideServerName(name string) error {
	c.config.ServerName = name
	return nil
}

func (c *expiringCredentials) ClientHandshake(ctx context.Context, authority string, raw net.Conn) (net.Conn, credentials.AuthInfo, error) {
	config, selected := c.handshakeConfig()
	conn, info, err := credentials.NewTLS(config).ClientHandshake(ctx, authority, raw)
	return expireConnection(conn, info, selected, err)
}

func (c *expiringCredentials) ServerHandshake(raw net.Conn) (net.Conn, credentials.AuthInfo, error) {
	config, selected := c.handshakeConfig()
	conn, info, err := credentials.NewTLS(config).ServerHandshake(raw)
	return expireConnection(conn, info, selected, err)
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

func expireConnection(conn net.Conn, auth credentials.AuthInfo, selected **tls.Certificate, err error) (net.Conn, credentials.AuthInfo, error) {
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
	if !ok || len(info.State.PeerCertificates) == 0 {
		return fail(errors.New("TLS handshake did not authenticate a peer certificate"))
	}
	expiry := leaf.NotAfter
	if peerExpiry := info.State.PeerCertificates[0].NotAfter; peerExpiry.Before(expiry) {
		expiry = peerExpiry
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
