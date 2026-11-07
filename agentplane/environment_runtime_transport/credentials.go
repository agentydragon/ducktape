// Package environment_runtime_transport provides explicit transport security for connections
// between the Runner and Runner Gateway.
package environment_runtime_transport

import (
	"context"
	"crypto"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/spiffe/go-spiffe/v2/bundle/x509bundle"
	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/spiffetls/tlsconfig"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
)

// GatewayURI is the fixed identity of the Runner Gateway in the runner
// control mutual-TLS trust domain.
const GatewayURI = "spiffe://agentplane/runner-gateway"

// EgressProxyURI is the fixed identity of the optional workload-authenticated
// forward-proxy listener.
const EgressProxyURI = "spiffe://agentplane/egress-proxy"

var trustDomain = spiffeid.RequireTrustDomainFromString("agentplane")

// Config names the projected certificate, private key, and trust roots.
type Config struct {
	CertFile string `json:"cert_file,omitempty" yaml:"cert_file,omitempty" mapstructure:"cert_file,omitempty"`
	KeyFile  string `json:"key_file,omitempty" yaml:"key_file,omitempty" mapstructure:"key_file,omitempty"`
	CAFile   string `json:"ca_file,omitempty" yaml:"ca_file,omitempty" mapstructure:"ca_file,omitempty"`
}

// Validate requires a complete file-backed source.
func (c Config) Validate() error {
	for _, file := range []struct{ name, path string }{
		{name: "cert_file", path: c.CertFile},
		{name: "key_file", path: c.KeyFile},
		{name: "ca_file", path: c.CAFile},
	} {
		if file.path == "" {
			return fmt.Errorf("TLS %s is required for file-backed mtls security", file.name)
		}
		if !filepath.IsAbs(file.path) {
			return fmt.Errorf("TLS %s must be an absolute path", file.name)
		}
	}
	return nil
}

// EnvironmentRuntimeURI returns the identity assigned to one Runner credential. credentialID
// is a fresh opaque ID bound to the active Pod or VMI incarnation by its label.
func EnvironmentRuntimeURI(namespace, sandboxUID, credentialID string) (string, error) {
	if len(namespace) == 0 || len(namespace) > 63 {
		return "", errors.New("namespace must be 1 to 63 characters")
	}
	for _, r := range namespace {
		if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-') {
			return "", errors.New("namespace must be a lowercase DNS label")
		}
	}
	if namespace[0] == '-' || namespace[len(namespace)-1] == '-' {
		return "", errors.New("namespace must not start or end with a hyphen")
	}
	id, err := spiffeid.FromSegments(trustDomain, "environment-runtime", namespace, sandboxUID, credentialID)
	if err != nil {
		return "", fmt.Errorf("runner identity: %w", err)
	}
	return id.String(), nil
}

// ParseEnvironmentRuntimeURI parses a Runner credential URI and returns its Kubernetes
// namespace, Sandbox UID, and credential ID, in that order.
func ParseEnvironmentRuntimeURI(raw string) (namespace, sandboxUID, credentialID string, err error) {
	id, err := spiffeid.FromString(raw)
	if err != nil {
		return "", "", "", fmt.Errorf("parse runner identity URI: %w", err)
	}
	if !id.MemberOf(trustDomain) {
		return "", "", "", errors.New("runner identity URI must use the agentplane SPIFFE trust domain")
	}
	segments := strings.Split(strings.TrimPrefix(id.Path(), "/"), "/")
	if len(segments) != 4 || segments[0] != "environment-runtime" {
		return "", "", "", errors.New("runner identity URI must contain namespace, Sandbox UID, and credential ID")
	}
	namespace, sandboxUID, credentialID = segments[1], segments[2], segments[3]
	canonical, err := EnvironmentRuntimeURI(namespace, sandboxUID, credentialID)
	if err != nil {
		return "", "", "", err
	}
	if canonical != raw {
		return "", "", "", errors.New("runner identity URI is not canonical")
	}
	return namespace, sandboxUID, credentialID, nil
}

// ClientCredentials creates mTLS client credentials pinned to the exact
// GatewayURI or EnvironmentRuntimeURI supplied as expectedPeerURI.
func (c Config) ClientCredentials(expectedPeerURI string) (credentials.TransportCredentials, error) {
	expected, err := parseIdentity(expectedPeerURI)
	if err != nil {
		return nil, fmt.Errorf("invalid expected peer URI: %w", err)
	}
	source, err := newFileSource(c, x509.ExtKeyUsageClientAuth)
	if err != nil {
		return nil, err
	}
	return newExpiringCredentials(tlsconfig.MTLSClientConfig(source, source, authorize(expected, x509.ExtKeyUsageServerAuth)), false), nil
}

// ServerCredentials creates mTLS server credentials pinned to the expected client URI.
func (c Config) ServerCredentials(expectedPeerURI string) (credentials.TransportCredentials, error) {
	expected, err := parseIdentity(expectedPeerURI)
	if err != nil {
		return nil, fmt.Errorf("invalid expected peer URI: %w", err)
	}
	source, err := newFileSource(c, x509.ExtKeyUsageServerAuth)
	if err != nil {
		return nil, err
	}
	return newExpiringCredentials(tlsconfig.MTLSServerConfig(source, source, authorize(expected, x509.ExtKeyUsageClientAuth)), true), nil
}

// WorkloadServerCredentials creates mTLS server credentials for the fixed
// egress identity and a live EnvironmentRuntime peer. Established connections
// close when either selected leaf certificate expires.
func (c Config) WorkloadServerCredentials(localURI string) (credentials.TransportCredentials, error) {
	config, err := c.WorkloadServerTLSConfig(localURI)
	if err != nil {
		return nil, err
	}
	return newExpiringCredentials(config, true), nil
}

// ClientTLSConfig creates HTTPS proxy credentials pinned to the egress proxy's
// fixed SPIFFE identity. The file source reloads projected certificate, key,
// and CA bundle replacements while keeping the client identity pinned.
func (c Config) ClientTLSConfig(expectedPeerURI string) (*tls.Config, error) {
	if expectedPeerURI != EgressProxyURI {
		return nil, fmt.Errorf("expected peer URI must be %q", EgressProxyURI)
	}
	if err := c.Validate(); err != nil {
		return nil, err
	}
	source, err := newFileSource(c, x509.ExtKeyUsageClientAuth)
	if err != nil {
		return nil, err
	}
	expected, err := spiffeid.FromString(expectedPeerURI)
	if err != nil {
		return nil, fmt.Errorf("invalid expected peer URI: %w", err)
	}
	return tlsconfig.MTLSClientConfig(source, source, authorize(expected, x509.ExtKeyUsageServerAuth)), nil
}

// ProxyServerTLSConfig creates verified TLS credentials for the egress proxy
// without presenting a client certificate. The CA bundle is the same
// reloadable file source used by file-backed SVID credentials, and the peer
// must present the fixed egress-proxy SVID with serverAuth usage.
func (c Config) ProxyServerTLSConfig() (*tls.Config, error) {
	if c.CertFile != "" || c.KeyFile != "" {
		return nil, errors.New("proxy server TLS without a client certificate accepts only ca_file")
	}
	source, err := newFileBundleSource(c.CAFile)
	if err != nil {
		return nil, err
	}
	expected, err := spiffeid.FromString(EgressProxyURI)
	if err != nil {
		return nil, fmt.Errorf("invalid expected peer URI: %w", err)
	}
	return tlsconfig.TLSClientConfig(source, authorize(expected, x509.ExtKeyUsageServerAuth)), nil
}

// WorkloadServerTLSConfig creates mTLS server credentials for the egress
// listener. It accepts only a current EnvironmentRuntime identity with an
// explicit clientAuth EKU; callers resolve the URI to live Sandbox state.
func (c Config) WorkloadServerTLSConfig(localURI string) (*tls.Config, error) {
	return c.workloadServerTLSConfig(localURI, false)
}

// WorkloadServerTLSConfigWithBearer permits a missing client certificate for a
// listener that also authenticates Kubernetes bearers. Every certificate that
// is presented still passes the same SPIFFE chain, identity and client EKU checks.
func (c Config) WorkloadServerTLSConfigWithBearer(localURI string) (*tls.Config, error) {
	return c.workloadServerTLSConfig(localURI, true)
}

func (c Config) workloadServerTLSConfig(localURI string, allowBearer bool) (*tls.Config, error) {
	if localURI != EgressProxyURI {
		return nil, fmt.Errorf("local URI must be %q", EgressProxyURI)
	}
	if err := c.Validate(); err != nil {
		return nil, err
	}
	source, err := newFileSource(c, x509.ExtKeyUsageServerAuth)
	if err != nil {
		return nil, err
	}
	if source.identity.String() != localURI {
		return nil, fmt.Errorf("local certificate identity %q does not match expected identity %q", source.identity, localURI)
	}
	authorizeRuntime := func(id spiffeid.ID, verifiedChains [][]*x509.Certificate) error {
		if _, _, _, err := ParseEnvironmentRuntimeURI(id.String()); err != nil {
			return fmt.Errorf("client identity must be a valid EnvironmentRuntimeURI: %w", err)
		}
		if len(verifiedChains) == 0 || len(verifiedChains[0]) == 0 {
			return errors.New("client certificate chain is not verified")
		}
		if err := requireEKU(verifiedChains[0][0], x509.ExtKeyUsageClientAuth); err != nil {
			return fmt.Errorf("peer certificate: %w", err)
		}
		return nil
	}
	config := tlsconfig.MTLSServerConfig(source, source, authorizeRuntime)
	if allowBearer {
		config.ClientAuth = tls.RequestClientCert
		verify := config.VerifyPeerCertificate
		config.VerifyPeerCertificate = func(rawCerts [][]byte, chains [][]*x509.Certificate) error {
			if len(rawCerts) == 0 {
				return nil
			}
			return verify(rawCerts, chains)
		}
	}
	return config, nil
}

// BearerServerTLSConfig serves TLS without admitting certificate authentication.
// Its certificate reloads with the same identity and server EKU checks as mTLS.
// A client presenting a certificate is refused because that method is disabled.
func (c Config) BearerServerTLSConfig(localURI string) (*tls.Config, error) {
	if c.CAFile != "" || !filepath.IsAbs(c.CertFile) || !filepath.IsAbs(c.KeyFile) {
		return nil, errors.New("bearer TLS requires absolute cert_file and key_file paths only")
	}
	svid, err := loadSVID(c, x509.ExtKeyUsageServerAuth)
	if err != nil {
		return nil, err
	}
	if svid.ID.String() != localURI {
		return nil, fmt.Errorf("local certificate identity %q does not match expected identity %q", svid.ID, localURI)
	}
	source := &fileSource{config: c, usage: x509.ExtKeyUsageServerAuth, identity: svid.ID}
	config := tlsconfig.TLSServerConfig(source)
	config.ClientAuth = tls.RequestClientCert
	config.VerifyPeerCertificate = func(rawCerts [][]byte, _ [][]*x509.Certificate) error {
		if len(rawCerts) != 0 {
			return errors.New("certificate authentication is not enabled")
		}
		return nil
	}
	return config, nil
}

// LocalIdentity returns the certificate URI after validating its chain and the
// caller's required authentication usage.
func (c Config) LocalIdentity(usage x509.ExtKeyUsage) (string, error) {
	source, err := newFileSource(c, usage)
	if err != nil {
		return "", err
	}
	return source.identity.String(), nil
}

// PeerIdentity returns the SPIFFE ID of a gRPC TLS peer and rejects a peer whose
// leaf has expired since the handshake.
//
// It reads the leaf from credentials.TLSInfo rather than using
// grpccredentials.PeerIDFromContext, whose AuthInfo hides the certificate
// needed for that expiry check. The leaf is authenticated because every TLS
// credential on these connections comes from this package, whose handshake
// fails unless go-spiffe verified and authorized the presented chain.
func PeerIdentity(ctx context.Context) (string, error) {
	remote, ok := peer.FromContext(ctx)
	if !ok {
		return "", errors.New("gRPC peer is missing")
	}
	info, ok := remote.AuthInfo.(credentials.TLSInfo)
	if !ok || len(info.State.PeerCertificates) == 0 {
		return "", errors.New("gRPC peer is not authenticated with TLS")
	}
	return peerIdentityFromTLSState(info.State, nil)
}

// PeerIdentityFromTLSState extracts an authenticated SPIFFE peer identity
// from a TLS connection and requires the peer certificate to explicitly allow
// the requested EKU. The caller must use the state from a TLS configuration
// that verified the peer chain.
func PeerIdentityFromTLSState(state tls.ConnectionState, usage x509.ExtKeyUsage) (string, error) {
	return peerIdentityFromTLSState(state, &usage)
}

func peerIdentityFromTLSState(state tls.ConnectionState, usage *x509.ExtKeyUsage) (string, error) {
	if len(state.PeerCertificates) == 0 {
		return "", errors.New("TLS peer certificate is missing")
	}
	leaf := state.PeerCertificates[0]
	now := time.Now()
	if now.Before(leaf.NotBefore) || now.After(leaf.NotAfter) {
		return "", errors.New("TLS peer certificate is outside its validity period")
	}
	if usage != nil {
		if err := requireEKU(leaf, *usage); err != nil {
			return "", fmt.Errorf("TLS peer certificate: %w", err)
		}
	}
	id, err := x509svid.IDFromCert(leaf)
	if err != nil {
		return "", fmt.Errorf("TLS peer identity: %w", err)
	}
	if _, err := parseIdentity(id.String()); err != nil {
		if id.String() != EgressProxyURI {
			return "", fmt.Errorf("TLS peer has an unsupported identity: %w", err)
		}
	}
	return id.String(), nil
}

// authorize accepts exactly the expected peer ID with the EKU its role needs;
// go-spiffe's chain verification accepts any EKU.
func authorize(expected spiffeid.ID, usage x509.ExtKeyUsage) tlsconfig.Authorizer {
	matchID := tlsconfig.AuthorizeID(expected)
	return func(id spiffeid.ID, verifiedChains [][]*x509.Certificate) error {
		if err := matchID(id, verifiedChains); err != nil {
			return err
		}
		if err := requireEKU(verifiedChains[0][0], usage); err != nil {
			return fmt.Errorf("peer certificate: %w", err)
		}
		return nil
	}
}

// fileSource serves the local X509-SVID and trust bundle from the mounted
// cert-manager and trust-manager files. Each Get stats the files and parses
// them again only when one was replaced (kubelet and Secret volume updates swap
// files atomically); a failed load fails that handshake and is retried on the
// next one.
type fileSource struct {
	config   Config
	usage    x509.ExtKeyUsage
	identity spiffeid.ID
	bundles  *fileBundleSource

	mu        sync.Mutex
	svid      *x509svid.SVID
	svidFiles []os.FileInfo
}

// newFileSource loads the local credentials and requires the certificate to
// chain to the configured roots with the given EKU. Rotation must keep the
// identity loaded here.
func newFileSource(config Config, usage x509.ExtKeyUsage) (*fileSource, error) {
	svid, err := loadSVID(config, usage)
	if err != nil {
		return nil, err
	}
	bundles, err := newFileBundleSource(config.CAFile)
	if err != nil {
		return nil, err
	}
	source := &fileSource{config: config, usage: usage, identity: svid.ID, bundles: bundles}
	if _, err := parseIdentity(svid.ID.String()); err != nil {
		return nil, fmt.Errorf("local certificate identity: %w", err)
	}
	if _, _, err := x509svid.Verify(svid.Certificates, source); err != nil {
		return nil, fmt.Errorf("verify local certificate chain: %w", err)
	}
	return source, nil
}

func (s *fileSource) GetX509SVID() (*x509svid.SVID, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	files, err := statFiles(s.config.CertFile, s.config.KeyFile)
	if err != nil {
		return nil, err
	}
	if s.svid != nil && sameFiles(files, s.svidFiles) {
		return s.svid, nil
	}
	svid, err := loadSVID(s.config, s.usage)
	if err != nil {
		return nil, err
	}
	if svid.ID != s.identity {
		return nil, fmt.Errorf("local certificate identity changed from %q to %q", s.identity, svid.ID)
	}
	s.svid, s.svidFiles = svid, files
	return svid, nil
}

func (s *fileSource) GetX509BundleForTrustDomain(td spiffeid.TrustDomain) (*x509bundle.Bundle, error) {
	return s.bundles.GetX509BundleForTrustDomain(td)
}

// fileBundleSource loads and reloads only the mounted trust bundle. Bearer
// TLS clients use it without needing a local SVID, while fileSource embeds the
// same behavior for mTLS clients and servers.
type fileBundleSource struct {
	caFile string
	mu     sync.Mutex
	bundle *x509bundle.Bundle
	files  []os.FileInfo
}

func newFileBundleSource(caFile string) (*fileBundleSource, error) {
	if caFile == "" || !filepath.IsAbs(caFile) {
		return nil, errors.New("CA file must be an absolute path")
	}
	source := &fileBundleSource{caFile: caFile}
	if _, err := source.GetX509BundleForTrustDomain(trustDomain); err != nil {
		return nil, err
	}
	return source, nil
}

func (s *fileBundleSource) GetX509BundleForTrustDomain(td spiffeid.TrustDomain) (*x509bundle.Bundle, error) {
	if td != trustDomain {
		return nil, fmt.Errorf("no trust bundle for trust domain %q", td)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	files, err := statFiles(s.caFile)
	if err != nil {
		return nil, err
	}
	if s.bundle != nil && sameFiles(files, s.files) {
		return s.bundle, nil
	}
	bundle, err := x509bundle.Load(trustDomain, s.caFile)
	if err != nil {
		return nil, fmt.Errorf("load configured CA roots: %w", err)
	}
	if bundle.Empty() {
		return nil, errors.New("CA file contains no certificates")
	}
	s.bundle, s.files = bundle, files
	return bundle, nil
}

// loadSVID reads the key pair with crypto/tls, which accepts the SEC 1 and
// PKCS #1 keys cert-manager writes by default; x509svid.Load takes only PKCS #8.
func loadSVID(config Config, usage x509.ExtKeyUsage) (*x509svid.SVID, error) {
	pair, err := tls.LoadX509KeyPair(config.CertFile, config.KeyFile)
	if err != nil {
		return nil, fmt.Errorf("load configured certificate and key: %w", err)
	}
	certificates := make([]*x509.Certificate, 0, len(pair.Certificate))
	for _, der := range pair.Certificate {
		certificate, err := x509.ParseCertificate(der)
		if err != nil {
			return nil, fmt.Errorf("parse configured certificate chain: %w", err)
		}
		certificates = append(certificates, certificate)
	}
	if err := requireEKU(certificates[0], usage); err != nil {
		return nil, err
	}
	id, err := x509svid.IDFromCert(certificates[0])
	if err != nil {
		return nil, fmt.Errorf("local certificate identity: %w", err)
	}
	signer, ok := pair.PrivateKey.(crypto.Signer)
	if !ok {
		return nil, fmt.Errorf("configured private key %T is not a signer", pair.PrivateKey)
	}
	return &x509svid.SVID{ID: id, Certificates: certificates, PrivateKey: signer}, nil
}

func statFiles(paths ...string) ([]os.FileInfo, error) {
	files := make([]os.FileInfo, 0, len(paths))
	for _, path := range paths {
		info, err := os.Stat(path)
		if err != nil {
			return nil, err
		}
		files = append(files, info)
	}
	return files, nil
}

func sameFiles(current, loaded []os.FileInfo) bool {
	for i := range current {
		if !os.SameFile(current[i], loaded[i]) || !current[i].ModTime().Equal(loaded[i].ModTime()) || current[i].Size() != loaded[i].Size() {
			return false
		}
	}
	return true
}

func requireEKU(certificate *x509.Certificate, usage x509.ExtKeyUsage) error {
	for _, candidate := range certificate.ExtKeyUsage {
		if candidate == usage {
			return nil
		}
	}
	return fmt.Errorf("certificate must explicitly allow extended key usage %s", usage)
}

// parseIdentity accepts only GatewayURI or a canonical EnvironmentRuntimeURI.
func parseIdentity(identity string) (spiffeid.ID, error) {
	if identity != GatewayURI && identity != EgressProxyURI {
		if _, _, _, err := ParseEnvironmentRuntimeURI(identity); err != nil {
			return spiffeid.ID{}, fmt.Errorf("identity must be GatewayURI or a valid EnvironmentRuntimeURI: %w", err)
		}
	}
	return spiffeid.FromString(identity)
}
