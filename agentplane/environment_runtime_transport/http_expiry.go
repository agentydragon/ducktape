package environment_runtime_transport

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net"
	"net/http"
	"sync"
	"time"
)

// ClientTLSConnection handshakes with the supplied verified TLS configuration
// and closes the connection when either selected leaf expires. Proxy clients use
// it so a persistent HTTP connection reconnects with rotated file credentials.
func ClientTLSConnection(ctx context.Context, raw net.Conn, security *tls.Config) (net.Conn, error) {
	config, selected := trackedTLSConfig(security)
	connection := tls.Client(raw, config)
	if err := connection.HandshakeContext(ctx); err != nil {
		_ = connection.Close()
		return nil, err
	}
	localNotAfter, err := selected.notAfter()
	if err != nil {
		_ = connection.Close()
		return nil, err
	}
	return &expiringConn{Conn: connection, cancelExpiry: CloseTLSConnAtCertificateExpiry(connection, localNotAfter)}, nil
}

// HTTPServerTLSExpiryConnState closes TLS connections when their local or peer
// leaf expires. localExpiry returns the NotAfter time of the certificate the
// server selected for that connection; a zero time omits the local bound.
// StateActive is handled once, and the timer is stopped for closed or hijacked
// connections. The callback observes and closes the original *tls.Conn rather
// than wrapping it, as net/http requires that concrete connection type.
func HTTPServerTLSExpiryConnState(localExpiry func(*tls.Conn) time.Time) func(net.Conn, http.ConnState) {
	var mu sync.Mutex
	timers := map[net.Conn]func(){}
	return func(connection net.Conn, state http.ConnState) {
		mu.Lock()
		defer mu.Unlock()
		switch state {
		case http.StateActive:
			secured, ok := connection.(*tls.Conn)
			if !ok || timers[connection] != nil {
				return
			}
			var localNotAfter time.Time
			if localExpiry != nil {
				localNotAfter = localExpiry(secured)
			}
			timers[connection] = CloseTLSConnAtCertificateExpiry(secured, localNotAfter)
		case http.StateClosed, http.StateHijacked:
			if cancel, ok := timers[connection]; ok {
				cancel()
				delete(timers, connection)
			}
		}
	}
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


type certificateExpiry struct {
	mu      sync.Mutex
	leafEnd time.Time
	err     error
}

func (e *certificateExpiry) record(certificate *tls.Certificate) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if certificate == nil {
		e.err = fmt.Errorf("TLS handshake selected no local certificate")
		return
	}
	leaf := certificate.Leaf
	if leaf == nil && len(certificate.Certificate) > 0 {
		var err error
		leaf, err = x509.ParseCertificate(certificate.Certificate[0])
		if err != nil {
			e.err = fmt.Errorf("parse local leaf certificate: %w", err)
			return
		}
	}
	if leaf == nil {
		e.err = fmt.Errorf("TLS handshake selected a certificate without a leaf")
		return
	}
	e.leafEnd = leaf.NotAfter
}

func (e *certificateExpiry) notAfter() (time.Time, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.err != nil {
		return time.Time{}, e.err
	}
	if e.leafEnd.IsZero() {
		return time.Time{}, fmt.Errorf("TLS handshake did not select a local certificate")
	}
	return e.leafEnd, nil
}

func trackedTLSConfig(source *tls.Config) (*tls.Config, *certificateExpiry) {
	config := source.Clone()
	expiry := &certificateExpiry{}
	if len(config.Certificates) > 0 {
		expiry.record(&config.Certificates[0])
	}
	if getClientCertificate := config.GetClientCertificate; getClientCertificate != nil {
		config.GetClientCertificate = func(info *tls.CertificateRequestInfo) (*tls.Certificate, error) {
			certificate, err := getClientCertificate(info)
			if err == nil {
				expiry.record(certificate)
			}
			return certificate, err
		}
	}
	if getCertificate := config.GetCertificate; getCertificate != nil {
		config.GetCertificate = func(info *tls.ClientHelloInfo) (*tls.Certificate, error) {
			certificate, err := getCertificate(info)
			if err == nil {
				expiry.record(certificate)
			}
			return certificate, err
		}
	}
	if getConfigForClient := config.GetConfigForClient; getConfigForClient != nil {
		config.GetConfigForClient = func(info *tls.ClientHelloInfo) (*tls.Config, error) {
			selected, err := getConfigForClient(info)
			if err != nil || selected == nil {
				return selected, err
			}
			return trackedConfigCallbacks(selected, expiry), nil
		}
	}
	return config, expiry
}

func trackedConfigCallbacks(source *tls.Config, expiry *certificateExpiry) *tls.Config {
	config := source.Clone()
	if len(config.Certificates) > 0 {
		expiry.record(&config.Certificates[0])
	}
	if getCertificate := config.GetCertificate; getCertificate != nil {
		config.GetCertificate = func(info *tls.ClientHelloInfo) (*tls.Certificate, error) {
			certificate, err := getCertificate(info)
			if err == nil {
				expiry.record(certificate)
			}
			return certificate, err
		}
	}
	if getClientCertificate := config.GetClientCertificate; getClientCertificate != nil {
		config.GetClientCertificate = func(info *tls.CertificateRequestInfo) (*tls.Certificate, error) {
			certificate, err := getClientCertificate(info)
			if err == nil {
				expiry.record(certificate)
			}
			return certificate, err
		}
	}
	return config
}


func earliestExpiry(expiries ...time.Time) time.Time {
	var earliest time.Time
	for _, expiry := range expiries {
		if !expiry.IsZero() && (earliest.IsZero() || expiry.Before(earliest)) {
			earliest = expiry
		}
	}
	return earliest
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
	expiresAt := earliestExpiry(localNotAfter, peerNotAfter)
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
