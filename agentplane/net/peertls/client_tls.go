package peertls

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"path/filepath"

	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"github.com/spiffe/go-spiffe/v2/spiffetls/tlsconfig"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
)

// ClientTLS verifies an exact server SVID. The optional client certificate fields
// select service SVID authentication instead of a bearer token.
type ClientTLS struct {
	CAFile    string `mapstructure:"ca_file" json:"ca_file" yaml:"ca_file"`
	ServerURI string `mapstructure:"server_uri" json:"server_uri" yaml:"server_uri"`
	CertFile  string `mapstructure:"cert_file" json:"cert_file,omitempty" yaml:"cert_file,omitempty"`
	KeyFile   string `mapstructure:"key_file" json:"key_file,omitempty" yaml:"key_file,omitempty"`
	ClientURI string `mapstructure:"client_uri" json:"client_uri,omitempty" yaml:"client_uri,omitempty"`
}

func (c ClientTLS) Validate() error {
	if !filepath.IsAbs(c.CAFile) {
		return errors.New("client TLS requires an absolute ca_file")
	}
	id, err := spiffeid.FromString(c.ServerURI)
	if err != nil || !id.MemberOf(trustDomain) || !isServerURI(c.ServerURI) {
		return errors.New("client TLS server_uri must name a supported agentplane service")
	}
	if c.UsesSVID() {
		if !filepath.IsAbs(c.CertFile) || !filepath.IsAbs(c.KeyFile) || !IsServiceURI(c.ClientURI) {
			return errors.New("service SVID requires absolute cert_file and key_file paths and an exact service client_uri")
		}
	}
	return nil
}

// UsesSVID reports an explicitly configured client certificate source. Partial
// sources still select SVID mode so validation cannot fall back to a bearer.
func (c *ClientTLS) UsesSVID() bool {
	return c != nil && (c.CertFile != "" || c.KeyFile != "" || c.ClientURI != "")
}

// ValidateCaller requires exactly one caller authentication mode. Binary settings
// pass their role URI; shared dialers pass an empty expectedClientURI.
func (c *ClientTLS) ValidateCaller(tokenPath, expectedClientURI string) error {
	if c != nil {
		if err := c.Validate(); err != nil {
			return err
		}
	}
	if c.UsesSVID() {
		if tokenPath != "" {
			return errors.New("service SVID and bearer token authentication cannot be combined")
		}
		if expectedClientURI != "" && c.ClientURI != expectedClientURI {
			return fmt.Errorf("client_uri must be %q", expectedClientURI)
		}
	} else if tokenPath == "" {
		return errors.New("a bearer token file or service SVID is required")
	}
	return nil
}

// TLSConfig uses reloadable trust roots and pins the exact service URI and
// serverAuth EKU. SVID mode also pins the local service identity across rotations.
func (c ClientTLS) TLSConfig() (*tls.Config, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	if c.UsesSVID() {
		source, err := newFileSource(Config{CertFile: c.CertFile, KeyFile: c.KeyFile, CAFile: c.CAFile}, x509.ExtKeyUsageClientAuth)
		if err != nil {
			return nil, err
		}
		if source.identity.String() != c.ClientURI {
			return nil, fmt.Errorf("local certificate identity %q does not match client_uri %q", source.identity, c.ClientURI)
		}
		expected, err := spiffeid.FromString(c.ServerURI)
		if err != nil {
			return nil, err
		}
		return tlsconfig.MTLSClientConfig(source, source, authorize(expected, x509.ExtKeyUsageServerAuth)), nil
	}
	source, err := newFileBundleSource(c.CAFile)
	if err != nil {
		return nil, err
	}
	expected, err := spiffeid.FromString(c.ServerURI)
	if err != nil {
		return nil, fmt.Errorf("client TLS server URI: %w", err)
	}
	return tlsconfig.TLSClientConfig(source, authorize(expected, x509.ExtKeyUsageServerAuth)), nil
}

// Credentials uses plaintext only when TLS is explicitly absent from the peer
// configuration. An invalid TLS configuration always fails; it never downgrades.
func (c *ClientTLS) Credentials() (credentials.TransportCredentials, error) {
	if c == nil {
		return insecure.NewCredentials(), nil
	}
	config, err := c.TLSConfig()
	if err != nil {
		return nil, err
	}
	if c.UsesSVID() {
		return newExpiringCredentials(config, false), nil
	}
	return credentials.NewTLS(config), nil
}
