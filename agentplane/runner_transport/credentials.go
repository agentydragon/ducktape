// Package runner_transport provides explicit transport security for connections
// between the Runner and Runner Gateway.
package runner_transport

import (
	"context"
	"crypto"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"os"
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

var trustDomain = spiffeid.RequireTrustDomainFromString("agentplane")

// Config names the projected certificate, private key, and trust roots.
// Credentials reload a file when it is replaced, so projected-secret rotations
// are picked up at the next TLS handshake without restarting the process.
type Config struct {
	CertFile string `json:"cert_file" yaml:"cert_file" mapstructure:"cert_file"`
	KeyFile  string `json:"key_file" yaml:"key_file" mapstructure:"key_file"`
	CAFile   string `json:"ca_file" yaml:"ca_file" mapstructure:"ca_file"`
}

// RunnerURI returns the identity assigned to one Runner credential. credentialID
// is a fresh opaque ID bound to the active Pod or VMI incarnation by its label.
func RunnerURI(namespace, sandboxUID, credentialID string) (string, error) {
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
	id, err := spiffeid.FromSegments(trustDomain, "runner", namespace, sandboxUID, credentialID)
	if err != nil {
		return "", fmt.Errorf("runner identity: %w", err)
	}
	return id.String(), nil
}

// ParseRunnerURI parses a Runner credential URI and returns its Kubernetes
// namespace, Sandbox UID, and credential ID, in that order.
func ParseRunnerURI(raw string) (namespace, sandboxUID, credentialID string, err error) {
	id, err := spiffeid.FromString(raw)
	if err != nil {
		return "", "", "", fmt.Errorf("parse runner identity URI: %w", err)
	}
	if !id.MemberOf(trustDomain) {
		return "", "", "", errors.New("runner identity URI must use the agentplane SPIFFE trust domain")
	}
	segments := strings.Split(strings.TrimPrefix(id.Path(), "/"), "/")
	if len(segments) != 4 || segments[0] != "runner" {
		return "", "", "", errors.New("runner identity URI must contain namespace, Sandbox UID, and credential ID")
	}
	namespace, sandboxUID, credentialID = segments[1], segments[2], segments[3]
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
// GatewayURI or RunnerURI supplied as expectedPeerURI.
func (c Config) ClientCredentials(expectedPeerURI string) (credentials.TransportCredentials, error) {
	expected, err := parseIdentity(expectedPeerURI)
	if err != nil {
		return nil, fmt.Errorf("invalid expected peer URI: %w", err)
	}
	source, err := newFileSource(c, x509.ExtKeyUsageClientAuth)
	if err != nil {
		return nil, err
	}
	return credentials.NewTLS(tlsconfig.MTLSClientConfig(source, source, authorize(expected, x509.ExtKeyUsageServerAuth))), nil
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
	return credentials.NewTLS(tlsconfig.MTLSServerConfig(source, source, authorize(expected, x509.ExtKeyUsageClientAuth))), nil
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
	leaf := info.State.PeerCertificates[0]
	now := time.Now()
	if now.Before(leaf.NotBefore) || now.After(leaf.NotAfter) {
		return "", errors.New("gRPC peer TLS certificate is outside its validity period")
	}
	id, err := x509svid.IDFromCert(leaf)
	if err != nil {
		return "", fmt.Errorf("gRPC peer identity: %w", err)
	}
	if _, err := parseIdentity(id.String()); err != nil {
		return "", fmt.Errorf("gRPC peer has an unsupported identity: %w", err)
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

	mu          sync.Mutex
	svid        *x509svid.SVID
	svidFiles   []os.FileInfo
	bundle      *x509bundle.Bundle
	bundleFiles []os.FileInfo
}

// newFileSource loads the local credentials and requires the certificate to
// chain to the configured roots with the given EKU. Rotation must keep the
// identity loaded here.
func newFileSource(config Config, usage x509.ExtKeyUsage) (*fileSource, error) {
	svid, err := loadSVID(config, usage)
	if err != nil {
		return nil, err
	}
	source := &fileSource{config: config, usage: usage, identity: svid.ID}
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
	if td != trustDomain {
		return nil, fmt.Errorf("no trust bundle for trust domain %q", td)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	files, err := statFiles(s.config.CAFile)
	if err != nil {
		return nil, err
	}
	if s.bundle != nil && sameFiles(files, s.bundleFiles) {
		return s.bundle, nil
	}
	bundle, err := x509bundle.Load(trustDomain, s.config.CAFile)
	if err != nil {
		return nil, fmt.Errorf("load configured CA roots: %w", err)
	}
	if bundle.Empty() {
		return nil, errors.New("CA file contains no certificates")
	}
	s.bundle, s.bundleFiles = bundle, files
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

// parseIdentity accepts only GatewayURI or a canonical RunnerURI.
func parseIdentity(identity string) (spiffeid.ID, error) {
	if identity != GatewayURI {
		if _, _, _, err := ParseRunnerURI(identity); err != nil {
			return spiffeid.ID{}, fmt.Errorf("identity must be GatewayURI or a valid RunnerURI: %w", err)
		}
	}
	return spiffeid.FromString(identity)
}
