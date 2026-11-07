package environment_runtime_transport

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net"
	"sync"
	"time"
)

// ClientTLSConnection handshakes with the supplied verified TLS configuration
// and closes the connection when either selected leaf expires. Proxy clients use
// it so a persistent HTTP connection reconnects with rotated file credentials.
func ClientTLSConnection(ctx context.Context, raw net.Conn, security *tls.Config) (net.Conn, error) {
	config := security.Clone()
	var selected *tls.Certificate
	if len(config.Certificates) > 0 {
		selected = &config.Certificates[0]
	}
	if getClientCertificate := config.GetClientCertificate; getClientCertificate != nil {
		config.GetClientCertificate = func(info *tls.CertificateRequestInfo) (*tls.Certificate, error) {
			certificate, err := getClientCertificate(info)
			if err != nil {
				return certificate, err
			}
			if certificate == nil {
				return nil, fmt.Errorf("TLS client certificate source returned no certificate")
			}
			selected = certificate
			return certificate, nil
		}
	}
	lifetime, cancelLifetime := context.WithCancel(context.Background())
	connection := tls.Client(&expiringConn{Conn: raw, cancelExpiry: cancelLifetime}, config)
	if err := connection.HandshakeContext(ctx); err != nil {
		_ = connection.Close()
		return nil, err
	}
	var localNotAfter time.Time
	if len(config.Certificates) > 0 || config.GetClientCertificate != nil {
		var err error
		localNotAfter, err = certificateNotAfter(selected)
		if err != nil {
			_ = connection.Close()
			return nil, err
		}
	}
	stopExpiry := CloseTLSConnAtCertificateExpiry(connection, localNotAfter)
	context.AfterFunc(lifetime, stopExpiry)
	return connection, nil
}

type expiringConn struct {
	net.Conn
	cancelExpiry func()
	closeOnce    sync.Once
	closeErr     error
}

func (c *expiringConn) Close() error {
	c.closeOnce.Do(func() {
		if c.cancelExpiry != nil {
			c.cancelExpiry()
		}
		c.closeErr = c.Conn.Close()
	})
	return c.closeErr
}

func certificateNotAfter(certificate *tls.Certificate) (time.Time, error) {
	if certificate == nil {
		return time.Time{}, fmt.Errorf("TLS handshake selected no local certificate")
	}
	leaf := certificate.Leaf
	if leaf == nil && len(certificate.Certificate) > 0 {
		var err error
		leaf, err = x509.ParseCertificate(certificate.Certificate[0])
		if err != nil {
			return time.Time{}, fmt.Errorf("parse local leaf certificate: %w", err)
		}
	}
	if leaf == nil {
		return time.Time{}, fmt.Errorf("TLS handshake selected a certificate without a leaf")
	}
	return leaf.NotAfter, nil
}

// CloseTLSConnAtCertificateExpiry closes conn at the earlier of its local leaf
// expiry and authenticated peer leaf expiry. The returned function cancels the
// timer. The TLS connection itself is not wrapped, which keeps net/http's
// concrete *tls.Conn checks working for custom DialTLSContext implementations.
func CloseTLSConnAtCertificateExpiry(conn *tls.Conn, localNotAfter time.Time) func() {
	state := conn.ConnectionState()
	var peerNotAfter time.Time
	if len(state.PeerCertificates) > 0 {
		peerNotAfter = state.PeerCertificates[0].NotAfter
	}
	expiresAt := localNotAfter
	if expiresAt.IsZero() || (!peerNotAfter.IsZero() && peerNotAfter.Before(expiresAt)) {
		expiresAt = peerNotAfter
	}
	if expiresAt.IsZero() {
		return func() {}
	}
	var once sync.Once
	timer := time.AfterFunc(max(time.Until(expiresAt), 0), func() {
		once.Do(func() { _ = conn.Close() })
	})
	return func() {
		timer.Stop()
	}
}
