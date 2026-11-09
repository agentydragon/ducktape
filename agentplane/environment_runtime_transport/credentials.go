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
	"net/url"
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

// HistoryIngressURI is the History Service workload listener.
const HistoryIngressURI = "spiffe://agentplane/history-ingress"

// NotificationsURI is the Notification Service workload listener.
const NotificationsURI = "spiffe://agentplane/notifications"

// LLMIngressURI is the LLM ingress listener that authenticates Sandbox SVIDs.
const LLMIngressURI = "spiffe://agentplane/llm-ingress"

// SessionDispatcherURI is the Runner Gateway's Sandbox workload listener.
const SessionDispatcherURI = "spiffe://agentplane/session-dispatcher"

// ActionsURI is the Action Service workload listener.
const ActionsURI = "spiffe://agentplane/actions"

func isWorkloadServiceURI(uri string) bool {
	switch uri {
	case EgressProxyURI, HistoryIngressURI, NotificationsURI, LLMIngressURI, SessionDispatcherURI, ActionsURI:
		return true
	default:
		return false
	}
}

var trustDomain = spiffeid.RequireTrustDomainFromString("agentplane")

// Config names either the projected certificate, private key, and trust roots,
// or the local SPIFFE Workload API Unix socket.
//
// CAFile verifies the local certificate chain and every peer. ClientCAFile, when
// set, replaces it for peers this side serves: a server then verifies client
// certificates against ClientCAFile alone and keeps CAFile for the servers it dials.
type Config struct {
	CertFile          string `json:"cert_file,omitempty" yaml:"cert_file,omitempty" mapstructure:"cert_file,omitempty"`
	KeyFile           string `json:"key_file,omitempty" yaml:"key_file,omitempty" mapstructure:"key_file,omitempty"`
	CAFile            string `json:"ca_file,omitempty" yaml:"ca_file,omitempty" mapstructure:"ca_file,omitempty"`
	ClientCAFile      string `json:"client_ca_file,omitempty" yaml:"client_ca_file,omitempty" mapstructure:"client_ca_file,omitempty"`
	WorkloadAPISocket string `json:"workload_api_socket,omitempty" yaml:"workload_api_socket,omitempty" mapstructure:"workload_api_socket,omitempty"`
}

// Validate requires either a complete file-backed source or one Workload API
// Unix socket URI. The two source forms cannot be mixed.
func (c Config) Validate() error {
	if c.WorkloadAPISocket != "" {
		if _, err := parseWorkloadAPISocket(c.WorkloadAPISocket); err != nil {
			return err
		}
		if c.CertFile != "" || c.KeyFile != "" || c.CAFile != "" || c.ClientCAFile != "" {
			return errors.New("TLS workload_api_socket cannot be combined with cert_file, key_file, ca_file, or client_ca_file")
		}
		return nil
	}
	if c.ClientCAFile != "" && !filepath.IsAbs(c.ClientCAFile) {
		return errors.New("TLS client_ca_file must be an absolute path")
	}
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

func parseWorkloadAPISocket(raw string) (string, error) {
	endpoint, err := url.Parse(raw)
	if err != nil || !strings.HasPrefix(raw, "unix:///") || endpoint.Scheme != "unix" || endpoint.Host != "" || endpoint.Opaque != "" || endpoint.User != nil || endpoint.ForceQuery || endpoint.RawQuery != "" || endpoint.Fragment != "" || !filepath.IsAbs(endpoint.Path) || endpoint.String() != raw {
		return "", errors.New("TLS workload_api_socket must be a canonical unix:///absolute/path URI")
	}
	return raw, nil
}

// WorkloadID is the identity a WorkloadCredential's certificate carries: the record's namespace and
// name, which pick the record, and the credential ID its status must still hold.
type WorkloadID struct {
	Namespace, Name, CredentialID string
}

// URI is spiffe://agentplane/<namespace>/<name>/<credential-id>.
func (w WorkloadID) URI() (string, error) {
	if err := validateNamespace(w.Namespace); err != nil {
		return "", err
	}
	if !isDNSSubdomain(w.Name) {
		return "", errors.New("workload name must be a lowercase DNS subdomain")
	}
	// The issuer mints 32 hexadecimal digits; a verifier compares the whole URI with its record's.
	if len(w.CredentialID) > 63 || !isDNSSubdomain(w.CredentialID) || strings.Contains(w.CredentialID, ".") {
		return "", errors.New("credential ID must be a lowercase DNS label")
	}
	id, err := spiffeid.FromSegments(trustDomain, w.Namespace, w.Name, w.CredentialID)
	if err != nil {
		return "", fmt.Errorf("workload identity: %w", err)
	}
	return id.String(), nil
}

// ParseWorkloadURI accepts only the canonical form URI writes.
func ParseWorkloadURI(raw string) (WorkloadID, error) {
	id, err := spiffeid.FromString(raw)
	if err != nil {
		return WorkloadID{}, fmt.Errorf("parse workload identity URI: %w", err)
	}
	if !id.MemberOf(trustDomain) {
		return WorkloadID{}, errors.New("workload identity URI must use the agentplane SPIFFE trust domain")
	}
	segments := strings.Split(strings.TrimPrefix(id.Path(), "/"), "/")
	if len(segments) != 3 {
		return WorkloadID{}, errors.New("workload identity URI must contain namespace, name and credential ID")
	}
	workload := WorkloadID{Namespace: segments[0], Name: segments[1], CredentialID: segments[2]}
	canonical, err := workload.URI()
	if err != nil {
		return WorkloadID{}, err
	}
	if canonical != raw {
		return WorkloadID{}, errors.New("workload identity URI is not canonical")
	}
	return workload, nil
}

func validateNamespace(namespace string) error {
	if len(namespace) == 0 || len(namespace) > 63 {
		return errors.New("namespace must be 1 to 63 characters")
	}
	for _, r := range namespace {
		if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-') {
			return errors.New("namespace must be a lowercase DNS label")
		}
	}
	if namespace[0] == '-' || namespace[len(namespace)-1] == '-' {
		return errors.New("namespace must not start or end with a hyphen")
	}
	return nil
}

func isDNSSubdomain(name string) bool {
	if len(name) == 0 || len(name) > 253 {
		return false
	}
	for _, label := range strings.Split(name, ".") {
		if label == "" || label[0] == '-' || label[len(label)-1] == '-' {
			return false
		}
		for _, r := range label {
			if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-') {
				return false
			}
		}
	}
	return true
}

// ClientCredentials creates mTLS client credentials pinned to the exact
// GatewayURI or a workload URI supplied as expectedPeerURI.
func (c Config) ClientCredentials(expectedPeerURI string) (credentials.TransportCredentials, error) {
	if c.WorkloadAPISocket != "" {
		return nil, errors.New("Workload API TLS configuration requires Security.Open(ctx)")
	}
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
	if c.WorkloadAPISocket != "" {
		return nil, errors.New("Workload API TLS configuration requires Security.Open(ctx)")
	}
	expected, err := parseIdentity(expectedPeerURI)
	if err != nil {
		return nil, fmt.Errorf("invalid expected peer URI: %w", err)
	}
	source, err := newFileSource(c, x509.ExtKeyUsageServerAuth)
	if err != nil {
		return nil, err
	}
	clients, err := c.clientBundles(source)
	if err != nil {
		return nil, err
	}
	return newExpiringCredentials(tlsconfig.MTLSServerConfig(source, clients, authorize(expected, x509.ExtKeyUsageClientAuth)), true), nil
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
	if c.WorkloadAPISocket != "" {
		return nil, errors.New("Workload API TLS configuration requires Security.Open(ctx)")
	}
	if !isWorkloadServiceURI(expectedPeerURI) {
		return nil, errors.New("expected peer URI must name a supported first-party service")
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
	if c.WorkloadAPISocket != "" {
		return nil, errors.New("Workload API server TLS is only supported through Security.Open(ctx)")
	}
	if !isWorkloadServiceURI(localURI) {
		return nil, errors.New("local URI must name a supported first-party service")
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
		if _, err := ParseWorkloadURI(id.String()); err != nil {
			return fmt.Errorf("client identity must be a valid workload URI: %w", err)
		}
		if len(verifiedChains) == 0 || len(verifiedChains[0]) == 0 {
			return errors.New("client certificate chain is not verified")
		}
		if err := requireEKU(verifiedChains[0][0], x509.ExtKeyUsageClientAuth); err != nil {
			return fmt.Errorf("peer certificate: %w", err)
		}
		return nil
	}
	clients, err := c.clientBundles(source)
	if err != nil {
		return nil, err
	}
	config := tlsconfig.MTLSServerConfig(source, clients, authorizeRuntime)
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
	if c.CAFile != "" || c.ClientCAFile != "" || c.WorkloadAPISocket != "" || !filepath.IsAbs(c.CertFile) || !filepath.IsAbs(c.KeyFile) {
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
	if c.WorkloadAPISocket != "" {
		return "", errors.New("Workload API TLS configuration requires Security.Open(ctx)")
	}
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

// clientBundles is the trust a server verifies client certificates against:
// ClientCAFile when set, otherwise the local source's CAFile.
func (c Config) clientBundles(local x509bundle.Source) (x509bundle.Source, error) {
	if c.ClientCAFile == "" {
		return local, nil
	}
	return newFileBundleSource(c.ClientCAFile)
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

// parseIdentity accepts only GatewayURI or a canonical workload URI.
func parseIdentity(identity string) (spiffeid.ID, error) {
	if identity != GatewayURI && !isWorkloadServiceURI(identity) {
		if _, err := ParseWorkloadURI(identity); err != nil {
			return spiffeid.ID{}, fmt.Errorf("identity must be GatewayURI or a valid workload URI: %w", err)
		}
	}
	return spiffeid.FromString(identity)
}
