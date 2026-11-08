package environment_runtime_transport

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

// ClientTLS verifies a service's server SVID without giving the caller a client
// certificate. Backend callers continue to authenticate with their own bearer.
type ClientTLS struct {
	CAFile    string `mapstructure:"ca_file" json:"ca_file"`
	ServerURI string `mapstructure:"server_uri" json:"server_uri"`
}

func (c ClientTLS) Validate() error {
	if !filepath.IsAbs(c.CAFile) {
		return errors.New("client TLS requires an absolute ca_file")
	}
	id, err := spiffeid.FromString(c.ServerURI)
	if err != nil || !id.MemberOf(trustDomain) || !(isWorkloadServiceURI(c.ServerURI) || c.ServerURI == GatewayURI) {
		return errors.New("client TLS server_uri must name a supported agentplane service")
	}
	return nil
}

// TLSConfig uses reloadable trust roots and pins the exact service URI and
// serverAuth EKU. It never presents a client certificate.
func (c ClientTLS) TLSConfig() (*tls.Config, error) {
	if err := c.Validate(); err != nil {
		return nil, err
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
	return credentials.NewTLS(config), nil
}
