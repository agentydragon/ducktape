package environment_runtime_transport

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net"
	"net/url"
	"slices"
	"sync"
	"syscall"
	"time"

	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
	"google.golang.org/grpc/credentials"
)

// expiringCredentials performs the TLS handshake itself so it can close the
// established connection when either authenticated leaf certificate expires.
// The returned AuthInfo remains credentials.TLSInfo, including the verified
// peer certificates used by gRPC resolvers.
type expiringCredentials struct {
	config *tls.Config
}

func newExpiringCredentials(config *tls.Config) credentials.TransportCredentials {
	return &expiringCredentials{config: grpcTLSDefaults(config)}
}

func (c *expiringCredentials) Info() credentials.ProtocolInfo {
	return credentials.ProtocolInfo{SecurityProtocol: "tls", SecurityVersion: "1.2", ServerName: c.config.ServerName}
}

func (c *expiringCredentials) Clone() credentials.TransportCredentials {
	return &expiringCredentials{config: c.config.Clone()}
}

func (c *expiringCredentials) OverrideServerName(serverName string) error {
	c.config.ServerName = serverName
	return nil
}

func (c *expiringCredentials) ClientHandshake(ctx context.Context, authority string, rawConn net.Conn) (net.Conn, credentials.AuthInfo, error) {
	config, localLeaf := trackedTLSConfig(c.config)
	host, _, err := net.SplitHostPort(authority)
	if err != nil {
		host = authority
	}
	config.ServerName = host
	secured := tls.Client(rawConn, config)
	if err := secured.HandshakeContext(ctx); err != nil {
		_ = secured.Close()
		return nil, nil, err
	}
	return finishTLSHandshake(rawConn, secured, localLeaf)
}

func (c *expiringCredentials) ServerHandshake(rawConn net.Conn) (net.Conn, credentials.AuthInfo, error) {
	config, localLeaf := trackedTLSConfig(c.config)
	secured := tls.Server(rawConn, config)
	if err := secured.Handshake(); err != nil {
		_ = secured.Close()
		return nil, nil, err
	}
	return finishTLSHandshake(rawConn, secured, localLeaf)
}

func finishTLSHandshake(rawConn net.Conn, secured *tls.Conn, localLeaf *certificateExpiry) (net.Conn, credentials.AuthInfo, error) {
	state := secured.ConnectionState()
	if state.NegotiatedProtocol != "h2" {
		_ = secured.Close()
		return nil, nil, fmt.Errorf("gRPC TLS handshake negotiated %q instead of h2", state.NegotiatedProtocol)
	}
	localNotAfter, err := localLeaf.notAfter()
	if err != nil {
		_ = secured.Close()
		return nil, nil, fmt.Errorf("local TLS certificate expiry: %w", err)
	}
	if len(state.PeerCertificates) == 0 {
		_ = secured.Close()
		return nil, nil, fmt.Errorf("TLS peer certificate is missing")
	}
	if expiry := earliestExpiry(localNotAfter, state.PeerCertificates[0].NotAfter); !expiry.After(time.Now()) {
		_ = secured.Close()
		return nil, nil, fmt.Errorf("TLS connection certificate expired at %s", expiry.UTC().Format(time.RFC3339Nano))
	}

	cancelExpiry := CloseTLSConnAtCertificateExpiry(secured, localNotAfter)
	conn := net.Conn(&expiringConn{Conn: secured, cancelExpiry: cancelExpiry})
	if syscallConn, ok := rawConn.(syscall.Conn); ok {
		conn = &expiringSyscallConn{Conn: conn, syscallConn: syscallConn}
	}
	info := credentials.TLSInfo{
		State:          state,
		CommonAuthInfo: credentials.CommonAuthInfo{SecurityLevel: credentials.PrivacyAndIntegrity},
	}
	if id, err := x509svid.IDFromCert(state.PeerCertificates[0]); err == nil {
		info.SPIFFEID, _ = url.Parse(id.String())
	}
	return conn, info, nil
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

// expiringSyscallConn preserves optional access to the underlying socket,
// matching gRPC's TLS credentials behavior for net.Conn implementations that
// support syscall.Conn.
type expiringSyscallConn struct {
	net.Conn
	syscallConn syscall.Conn
}

func (c *expiringSyscallConn) SyscallConn() (syscall.RawConn, error) {
	return c.syscallConn.SyscallConn()
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

func grpcTLSDefaults(source *tls.Config) *tls.Config {
	config := source.Clone()
	if !slices.Contains(config.NextProtos, "h2") {
		config.NextProtos = append(config.NextProtos, "h2")
	}
	if config.MinVersion == 0 && (config.MaxVersion == 0 || config.MaxVersion >= tls.VersionTLS12) {
		config.MinVersion = tls.VersionTLS12
	}
	if config.CipherSuites == nil {
		for _, suite := range tls.CipherSuites() {
			if !grpcForbiddenCipherSuite(suite.ID) {
				config.CipherSuites = append(config.CipherSuites, suite.ID)
			}
		}
	}
	return config
}

func grpcForbiddenCipherSuite(id uint16) bool {
	switch id {
	case tls.TLS_RSA_WITH_AES_128_CBC_SHA,
		tls.TLS_RSA_WITH_AES_256_CBC_SHA,
		tls.TLS_RSA_WITH_AES_128_GCM_SHA256,
		tls.TLS_RSA_WITH_AES_256_GCM_SHA384,
		tls.TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA,
		tls.TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA,
		tls.TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA,
		tls.TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA:
		return true
	default:
		return false
	}
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

var (
	_ credentials.TransportCredentials = (*expiringCredentials)(nil)
	_ syscall.Conn                     = (*expiringSyscallConn)(nil)
)
