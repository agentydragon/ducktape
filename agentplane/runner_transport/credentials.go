// Package runner_transport provides explicit transport security for connections
// between the Runner and Runner Gateway.
package runner_transport

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"strings"
	"time"

	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
)

const (
	// GatewayURI is the fixed identity of the Runner Gateway in the runner
	// control mutual-TLS trust domain.
	GatewayURI = "spiffe://agentplane/runner-gateway"

	identityScheme = "spiffe"
	identityHost   = "agentplane"
	identityPrefix = "spiffe://agentplane/runner/"
)

// Config names the projected certificate, private key, and trust roots.
// The files are read again during every TLS handshake so projected-secret
// rotations are picked up without restarting the process.
type Config struct {
	CertFile string `json:"cert_file" yaml:"cert_file" mapstructure:"cert_file"`
	KeyFile  string `json:"key_file" yaml:"key_file" mapstructure:"key_file"`
	CAFile   string `json:"ca_file" yaml:"ca_file" mapstructure:"ca_file"`
}

// RunnerURI returns the identity assigned to one Runner credential. credentialID
// is a fresh opaque ID bound to the active Pod or VMI incarnation by its label.
func RunnerURI(namespace, sandboxUID, credentialID string) (string, error) {
	if !validPathSegment(namespace) {
		return "", errors.New("namespace must be a non-empty URI path segment")
	}
	if !validPathSegment(sandboxUID) {
		return "", errors.New("sandbox UID must be a non-empty URI path segment")
	}
	if !validPathSegment(credentialID) {
		return "", errors.New("credential ID must be a non-empty URI path segment")
	}
	if len(namespace) > 63 {
		return "", errors.New("namespace must be at most 63 characters")
	}
	for _, r := range namespace {
		if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-') {
			return "", errors.New("namespace must be a lowercase DNS label")
		}
	}
	if namespace[0] == '-' || namespace[len(namespace)-1] == '-' {
		return "", errors.New("namespace must not start or end with a hyphen")
	}
	return identityPrefix + namespace + "/" + sandboxUID + "/" + credentialID, nil
}

// ParseRunnerURI parses a Runner credential URI and returns its Kubernetes
// namespace, Sandbox UID, and credential ID, in that order.
func ParseRunnerURI(raw string) (namespace, sandboxUID, credentialID string, err error) {
	u, err := url.Parse(raw)
	if err != nil {
		return "", "", "", fmt.Errorf("parse runner identity URI: %w", err)
	}
	if u.Scheme != identityScheme || u.Host != identityHost || u.User != nil || u.Opaque != "" || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" {
		return "", "", "", errors.New("runner identity URI must use the agentplane SPIFFE trust domain")
	}
	if u.RawPath != "" || u.String() != raw {
		return "", "", "", errors.New("runner identity URI must use its canonical encoding")
	}
	segments := strings.Split(strings.TrimPrefix(u.Path, "/"), "/")
	if len(segments) != 4 || segments[0] != "runner" {
		return "", "", "", errors.New("runner identity URI must contain namespace, Sandbox UID, and credential ID")
	}
	namespace, sandboxUID, credentialID = segments[1], segments[2], segments[3]
	if !validPathSegment(sandboxUID) || !validPathSegment(credentialID) {
		return "", "", "", errors.New("runner identity URI contains an invalid UID or credential ID")
	}
	canonical, err := RunnerURI(namespace, sandboxUID, credentialID)
	if err != nil {
		return "", "", "", err
	}
	if canonical != raw {
		return "", "", "", errors.New("runner identity URI is not canonical")
	}
	return namespace, sandboxUID, credentialID, nil
}

// ClientCredentials creates mTLS client credentials pinned to the exact
// GatewayURI or RunnerURI supplied as expectedPeerURI. The expected URI is
// checked on every handshake against the peer leaf certificate's sole URI SAN.
func (c Config) ClientCredentials(expectedPeerURI string) (credentials.TransportCredentials, error) {
	if err := validateIdentityURI(expectedPeerURI); err != nil {
		return nil, fmt.Errorf("invalid expected peer URI: %w", err)
	}
	localIdentity, err := c.validateLocalCertificate(x509.ExtKeyUsageClientAuth)
	if err != nil {
		return nil, err
	}
	verify := func(peerCertificates []*x509.Certificate) ([][]*x509.Certificate, error) {
		return c.verifyPeer(peerCertificates, x509.ExtKeyUsageServerAuth, expectedPeerURI)
	}
	tlsConfig := &tls.Config{
		MinVersion: tls.VersionTLS12,
		// SPIFFE URI SANs are identities, not TLS DNS names. VerifyConnection
		// below performs full x509 chain and EKU verification before accepting
		// the exact URI SAN; this flag skips only hostname matching.
		InsecureSkipVerify: true,
		GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
			certificate, err := c.loadLocalKeyPair(x509.ExtKeyUsageClientAuth, localIdentity)
			if err != nil {
				return nil, err
			}
			return &certificate, nil
		},
		VerifyConnection: func(state tls.ConnectionState) error {
			_, err := verify(state.PeerCertificates)
			return err
		},
	}
	return &transportCredentials{
		inner:  credentials.NewTLS(tlsConfig),
		verify: verify,
	}, nil
}

// ServerCredentials creates mTLS server credentials pinned to the expected client URI.
func (c Config) ServerCredentials(expectedPeerURI string) (credentials.TransportCredentials, error) {
	if err := validateIdentityURI(expectedPeerURI); err != nil {
		return nil, fmt.Errorf("invalid expected peer URI: %w", err)
	}
	localIdentity, err := c.validateLocalCertificate(x509.ExtKeyUsageServerAuth)
	if err != nil {
		return nil, err
	}
	verify := func(peerCertificates []*x509.Certificate) ([][]*x509.Certificate, error) {
		return c.verifyPeer(peerCertificates, x509.ExtKeyUsageClientAuth, expectedPeerURI)
	}
	tlsConfig := &tls.Config{
		MinVersion: tls.VersionTLS12,
		ClientAuth: tls.RequireAnyClientCert,
		GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
			certificate, err := c.loadLocalKeyPair(x509.ExtKeyUsageServerAuth, localIdentity)
			if err != nil {
				return nil, err
			}
			return &certificate, nil
		},
		VerifyConnection: func(state tls.ConnectionState) error {
			_, err := verify(state.PeerCertificates)
			return err
		},
	}
	return &transportCredentials{
		inner:  credentials.NewTLS(tlsConfig),
		verify: verify,
	}, nil
}

// LocalIdentity returns the certificate URI after validating its chain and the
// caller's required authentication usage.
func (c Config) LocalIdentity(usage x509.ExtKeyUsage) (string, error) {
	return c.validateLocalCertificate(usage)
}

// PeerIdentity returns the sole verified SPIFFE URI SAN in a gRPC TLS peer.
// The certificate must be the leaf of a populated VerifiedChains entry; a
// caller-created TLSInfo with only PeerCertificates is not authenticated.
func PeerIdentity(ctx context.Context) (string, error) {
	remote, ok := peer.FromContext(ctx)
	if !ok || remote == nil {
		return "", errors.New("gRPC peer is missing")
	}
	var state tls.ConnectionState
	switch info := remote.AuthInfo.(type) {
	case credentials.TLSInfo:
		state = info.State
	case *credentials.TLSInfo:
		if info == nil {
			return "", errors.New("gRPC peer TLS info is missing")
		}
		state = info.State
	default:
		return "", errors.New("gRPC peer is not authenticated with TLS")
	}
	if len(state.PeerCertificates) == 0 || len(state.VerifiedChains) == 0 || len(state.VerifiedChains[0]) == 0 {
		return "", errors.New("gRPC peer TLS certificate has no verified chain")
	}
	leaf := state.PeerCertificates[0]
	if !bytes.Equal(leaf.Raw, state.VerifiedChains[0][0].Raw) {
		return "", errors.New("gRPC peer verified chain does not match its presented leaf")
	}
	now := time.Now()
	if now.Before(leaf.NotBefore) || now.After(leaf.NotAfter) {
		return "", errors.New("gRPC peer TLS certificate is outside its validity period")
	}
	identity, err := certificateIdentity(leaf)
	if err != nil {
		return "", err
	}
	if err := validateIdentityURI(identity); err != nil {
		return "", fmt.Errorf("gRPC peer has an unsupported identity: %w", err)
	}
	return identity, nil
}

func (c Config) validateLocalCertificate(usage x509.ExtKeyUsage) (string, error) {
	certificate, err := c.loadKeyPair(usage)
	if err != nil {
		return "", err
	}
	identity, err := certificateIdentity(certificate.Leaf)
	if err != nil {
		return "", fmt.Errorf("local certificate identity: %w", err)
	}
	if err := validateIdentityURI(identity); err != nil {
		return "", fmt.Errorf("local certificate identity: %w", err)
	}
	if err := c.verifyLocalChain(certificate, usage); err != nil {
		return "", err
	}
	return identity, nil
}

func (c Config) loadLocalKeyPair(usage x509.ExtKeyUsage, expectedIdentity string) (tls.Certificate, error) {
	certificate, err := c.loadKeyPair(usage)
	if err != nil {
		return tls.Certificate{}, err
	}
	identity, err := certificateIdentity(certificate.Leaf)
	if err != nil {
		return tls.Certificate{}, fmt.Errorf("local certificate identity: %w", err)
	}
	if identity != expectedIdentity {
		return tls.Certificate{}, fmt.Errorf("local certificate identity changed from %q to %q", expectedIdentity, identity)
	}
	if err := c.verifyLocalChain(certificate, usage); err != nil {
		return tls.Certificate{}, err
	}
	return certificate, nil
}

func (c Config) verifyLocalChain(certificate tls.Certificate, usage x509.ExtKeyUsage) error {
	if len(certificate.Certificate) == 0 {
		return errors.New("local certificate contains no leaf certificate")
	}
	chain := make([]*x509.Certificate, 0, len(certificate.Certificate))
	for _, der := range certificate.Certificate {
		parsed, err := x509.ParseCertificate(der)
		if err != nil {
			return fmt.Errorf("parse local certificate chain: %w", err)
		}
		chain = append(chain, parsed)
	}
	identity, err := certificateIdentity(chain[0])
	if err != nil {
		return err
	}
	_, err = c.verifyPeer(chain, usage, identity)
	return err
}

func (c Config) loadKeyPair(usage x509.ExtKeyUsage) (tls.Certificate, error) {
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		certificate, err := tls.LoadX509KeyPair(c.CertFile, c.KeyFile)
		if err == nil {
			if len(certificate.Certificate) == 0 {
				return tls.Certificate{}, errors.New("configured certificate contains no certificates")
			}
			leaf, parseErr := x509.ParseCertificate(certificate.Certificate[0])
			if parseErr != nil {
				return tls.Certificate{}, fmt.Errorf("parse configured leaf certificate: %w", parseErr)
			}
			certificate.Leaf = leaf
			if err := requireEKU(leaf, usage); err != nil {
				return tls.Certificate{}, err
			}
			return certificate, nil
		}
		lastErr = err
	}
	return tls.Certificate{}, fmt.Errorf("load configured certificate and key: %w", lastErr)
}

func (c Config) loadRoots() (*x509.CertPool, error) {
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		roots, err := readRoots(c.CAFile)
		if err == nil {
			return roots, nil
		}
		lastErr = err
	}
	return nil, fmt.Errorf("load configured CA roots: %w", lastErr)
}

func readRoots(path string) (*x509.CertPool, error) {
	contents, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	remaining := contents
	count := 0
	for len(bytes.TrimSpace(remaining)) > 0 {
		block, rest := pem.Decode(remaining)
		if block == nil {
			return nil, errors.New("CA file contains invalid PEM data")
		}
		remaining = rest
		if block.Type != "CERTIFICATE" {
			return nil, fmt.Errorf("CA file contains PEM block %q, expected CERTIFICATE", block.Type)
		}
		certificate, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("parse CA certificate: %w", err)
		}
		if !certificate.IsCA {
			return nil, errors.New("CA file contains a certificate that is not a CA")
		}
		pool.AddCert(certificate)
		count++
	}
	if count == 0 {
		return nil, errors.New("CA file contains no certificates")
	}
	return pool, nil
}

func (c Config) verifyPeer(certificates []*x509.Certificate, usage x509.ExtKeyUsage, expected string) ([][]*x509.Certificate, error) {
	if len(certificates) == 0 || certificates[0] == nil {
		return nil, errors.New("peer did not present a certificate")
	}
	leaf := certificates[0]
	if err := requireEKU(leaf, usage); err != nil {
		return nil, fmt.Errorf("peer certificate: %w", err)
	}
	identity, err := certificateIdentity(leaf)
	if err != nil {
		return nil, fmt.Errorf("peer certificate identity: %w", err)
	}
	if identity != expected {
		return nil, fmt.Errorf("peer identity %q does not match expected identity %q", identity, expected)
	}

	roots, err := c.loadRoots()
	if err != nil {
		return nil, err
	}
	intermediates := x509.NewCertPool()
	for _, intermediate := range certificates[1:] {
		if intermediate != nil {
			intermediates.AddCert(intermediate)
		}
	}
	chains, err := leaf.Verify(x509.VerifyOptions{
		Roots:         roots,
		Intermediates: intermediates,
		KeyUsages:     []x509.ExtKeyUsage{usage},
	})
	if err != nil {
		return nil, fmt.Errorf("verify peer certificate chain: %w", err)
	}
	return chains, nil
}

func requireEKU(certificate *x509.Certificate, usage x509.ExtKeyUsage) error {
	for _, candidate := range certificate.ExtKeyUsage {
		if candidate == usage {
			return nil
		}
	}
	return fmt.Errorf("certificate must explicitly allow extended key usage %s", usage)
}

func certificateIdentity(certificate *x509.Certificate) (string, error) {
	if certificate == nil || len(certificate.URIs) != 1 || certificate.URIs[0] == nil {
		return "", errors.New("certificate must contain exactly one URI SAN")
	}
	return certificate.URIs[0].String(), nil
}

func validateIdentityURI(identity string) error {
	if identity == GatewayURI {
		return nil
	}
	if _, _, _, err := ParseRunnerURI(identity); err != nil {
		return fmt.Errorf("identity must be GatewayURI or a valid RunnerURI: %w", err)
	}
	return nil
}

func validPathSegment(value string) bool {
	if value == "" || value == "." || value == ".." {
		return false
	}
	for _, r := range value {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_' || r == '.') {
			return false
		}
	}
	return true
}

type transportCredentials struct {
	inner  credentials.TransportCredentials
	verify func([]*x509.Certificate) ([][]*x509.Certificate, error)
}

func (c *transportCredentials) ClientHandshake(ctx context.Context, authority string, rawConn net.Conn) (net.Conn, credentials.AuthInfo, error) {
	conn, info, err := c.inner.ClientHandshake(ctx, authority, rawConn)
	if err != nil {
		return nil, nil, err
	}
	return c.attachVerifiedChain(conn, info)
}

func (c *transportCredentials) ServerHandshake(rawConn net.Conn) (net.Conn, credentials.AuthInfo, error) {
	conn, info, err := c.inner.ServerHandshake(rawConn)
	if err != nil {
		return nil, nil, err
	}
	return c.attachVerifiedChain(conn, info)
}

func (c *transportCredentials) attachVerifiedChain(conn net.Conn, info credentials.AuthInfo) (net.Conn, credentials.AuthInfo, error) {
	var tlsInfo credentials.TLSInfo
	switch value := info.(type) {
	case credentials.TLSInfo:
		tlsInfo = value
	case *credentials.TLSInfo:
		if value == nil {
			_ = conn.Close()
			return nil, nil, errors.New("gRPC TLS credentials returned nil TLSInfo")
		}
		tlsInfo = *value
	default:
		_ = conn.Close()
		return nil, nil, fmt.Errorf("gRPC TLS credentials returned unexpected auth info %T", info)
	}
	chains, err := c.verify(tlsInfo.State.PeerCertificates)
	if err != nil {
		_ = conn.Close()
		return nil, nil, fmt.Errorf("verify peer certificate after TLS handshake: %w", err)
	}
	tlsInfo.State.VerifiedChains = chains
	return conn, tlsInfo, nil
}

func (c *transportCredentials) Info() credentials.ProtocolInfo {
	return c.inner.Info()
}

func (c *transportCredentials) Clone() credentials.TransportCredentials {
	return &transportCredentials{inner: c.inner.Clone(), verify: c.verify}
}

func (c *transportCredentials) OverrideServerName(serverName string) error {
	return c.inner.OverrideServerName(serverName)
}
