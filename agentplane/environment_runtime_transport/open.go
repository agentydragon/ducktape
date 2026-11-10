package environment_runtime_transport

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/spiffe/go-spiffe/v2/bundle/x509bundle"
	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/spiffetls/tlsconfig"
	"github.com/spiffe/go-spiffe/v2/svid/x509svid"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
)

const workloadAPISourceStartupTimeout = 30 * time.Second

type credentialSource interface {
	GetX509SVID() (*x509svid.SVID, error)
	GetX509BundleForTrustDomain(spiffeid.TrustDomain) (*x509bundle.Bundle, error)
}

// OpenedSecurity owns a validated security source for one Runtime lifetime.
// Open it once and reuse it for local identity checks and all TLS handshakes.
type OpenedSecurity struct {
	security Security
	source   credentialSource
	// clients verifies the client certificates of peers this side serves.
	clients  x509bundle.Source
	identity spiffeid.ID
	cancel   context.CancelFunc
	closeFn  func() error

	closeOnce sync.Once
	closeErr  error
}

// Open validates security configuration and opens its local credential source.
// For Workload API security ctx bounds the initial stream update; Close stops
// the ongoing watch.
func (s Security) Open(ctx context.Context) (*OpenedSecurity, error) {
	if err := s.Validate(); err != nil {
		return nil, err
	}
	if ctx == nil {
		return nil, errors.New("security source context is required")
	}
	openedCtx, cancel := context.WithCancel(ctx)
	opened := &OpenedSecurity{security: s, cancel: cancel}
	if s.Mode == Insecure {
		return opened, nil
	}

	var err error
	if s.TLS.WorkloadAPISocket != "" {
		opened.source, opened.closeFn, err = newWorkloadAPISource(openedCtx, s.TLS.WorkloadAPISocket)
	} else {
		opened.source, err = newFileSource(*s.TLS, x509.ExtKeyUsageServerAuth)
	}
	if err != nil {
		cancel()
		return nil, err
	}
	if opened.clients, err = s.TLS.clientBundles(opened.source); err != nil {
		_ = opened.Close()
		return nil, err
	}
	svid, err := opened.source.GetX509SVID()
	if err != nil {
		_ = opened.Close()
		return nil, fmt.Errorf("load local certificate: %w", err)
	}
	if _, err := parseIdentity(svid.ID.String()); err != nil {
		_ = opened.Close()
		return nil, fmt.Errorf("local certificate identity: %w", err)
	}
	if len(svid.Certificates) == 0 {
		_ = opened.Close()
		return nil, errors.New("local certificate chain is empty")
	}
	certificateIdentity, err := x509svid.IDFromCert(svid.Certificates[0])
	if err != nil {
		_ = opened.Close()
		return nil, fmt.Errorf("local certificate identity: %w", err)
	}
	if certificateIdentity != svid.ID {
		_ = opened.Close()
		return nil, fmt.Errorf("local certificate identity %q does not match SVID identity %q", certificateIdentity, svid.ID)
	}
	if _, _, err := x509svid.Verify(svid.Certificates, opened.source); err != nil {
		_ = opened.Close()
		return nil, fmt.Errorf("verify local certificate chain: %w", err)
	}
	opened.identity = svid.ID
	return opened, nil
}

// Close stops a Workload API watch and its owned connection. It is safe to call
// more than once.
func (s *OpenedSecurity) Close() error {
	if s == nil {
		return nil
	}
	s.closeOnce.Do(func() {
		if s.closeFn != nil {
			s.closeErr = s.closeFn()
		}
		if s.cancel != nil {
			s.cancel()
		}
	})
	return s.closeErr
}

// LocalIdentity returns the currently provisioned local identity after
// checking the pinned identity, trust chain, validity, and requested EKU.
func (s *OpenedSecurity) LocalIdentity(usage x509.ExtKeyUsage) (string, error) {
	if s == nil {
		return "", errors.New("opened security is required")
	}
	if s.security.Mode == Insecure {
		return "", errors.New("insecure security has no local identity")
	}
	if _, err := s.localSVID(usage); err != nil {
		return "", err
	}
	return s.identity.String(), nil
}

// ClientCredentials creates client credentials backed by the shared source.
func (s *OpenedSecurity) ClientCredentials(expectedPeerURI string) (credentials.TransportCredentials, error) {
	if s == nil {
		return nil, errors.New("opened security is required")
	}
	if s.security.Mode == Insecure {
		return insecure.NewCredentials(), nil
	}
	expected, err := parseIdentity(expectedPeerURI)
	if err != nil {
		return nil, fmt.Errorf("invalid expected peer URI: %w", err)
	}
	source, err := s.purposeSource(x509.ExtKeyUsageClientAuth)
	if err != nil {
		return nil, err
	}
	return newExpiringCredentials(tlsconfig.MTLSClientConfig(source, source, authorize(expected, x509.ExtKeyUsageServerAuth)), false), nil
}

// ServerCredentials creates server credentials backed by the shared source.
func (s *OpenedSecurity) ServerCredentials(expectedPeerURI string) (credentials.TransportCredentials, error) {
	if s == nil {
		return nil, errors.New("opened security is required")
	}
	if s.security.Mode == Insecure {
		return insecure.NewCredentials(), nil
	}
	expected, err := parseIdentity(expectedPeerURI)
	if err != nil {
		return nil, fmt.Errorf("invalid expected peer URI: %w", err)
	}
	source, err := s.purposeSource(x509.ExtKeyUsageServerAuth)
	if err != nil {
		return nil, err
	}
	return newExpiringCredentials(tlsconfig.MTLSServerConfig(source, s.clients, authorize(expected, x509.ExtKeyUsageClientAuth)), true), nil
}

// AuthorizePeer checks the authenticated peer against the exact configured
// identity, preserving the Security configuration's authorization behavior.
func (s *OpenedSecurity) AuthorizePeer(ctx context.Context, expectedPeerURI string) error {
	if s == nil {
		return errors.New("opened security is required")
	}
	return s.security.AuthorizePeer(ctx, expectedPeerURI)
}

func (s *OpenedSecurity) purposeSource(usage x509.ExtKeyUsage) (*purposeSource, error) {
	if _, err := s.localSVID(usage); err != nil {
		return nil, err
	}
	return &purposeSource{source: s.source, identity: s.identity, usage: usage}, nil
}

func (s *OpenedSecurity) localSVID(usage x509.ExtKeyUsage) (*x509svid.SVID, error) {
	if s.source == nil {
		return nil, errors.New("local TLS source is unavailable")
	}
	return validateLocalSVID(s.source, s.identity, usage)
}

type purposeSource struct {
	source   credentialSource
	identity spiffeid.ID
	usage    x509.ExtKeyUsage
}

func (s *purposeSource) GetX509SVID() (*x509svid.SVID, error) {
	return validateLocalSVID(s.source, s.identity, s.usage)
}

func (s *purposeSource) GetX509BundleForTrustDomain(td spiffeid.TrustDomain) (*x509bundle.Bundle, error) {
	return s.source.GetX509BundleForTrustDomain(td)
}

func validateLocalSVID(source credentialSource, identity spiffeid.ID, usage x509.ExtKeyUsage) (*x509svid.SVID, error) {
	svid, err := source.GetX509SVID()
	if err != nil {
		return nil, fmt.Errorf("load local certificate: %w", err)
	}
	if svid.ID != identity {
		return nil, fmt.Errorf("local certificate identity changed from %q to %q", identity, svid.ID)
	}
	if _, err := parseIdentity(svid.ID.String()); err != nil {
		return nil, fmt.Errorf("local certificate identity: %w", err)
	}
	if len(svid.Certificates) == 0 {
		return nil, errors.New("local certificate chain is empty")
	}
	certificateIdentity, err := x509svid.IDFromCert(svid.Certificates[0])
	if err != nil {
		return nil, fmt.Errorf("local certificate identity: %w", err)
	}
	if certificateIdentity != identity {
		return nil, fmt.Errorf("local certificate identity %q does not match pinned identity %q", certificateIdentity, identity)
	}
	if err := requireEKU(svid.Certificates[0], usage); err != nil {
		return nil, err
	}
	if _, _, err := x509svid.Verify(svid.Certificates, source); err != nil {
		return nil, fmt.Errorf("verify local certificate chain: %w", err)
	}
	return svid, nil
}
