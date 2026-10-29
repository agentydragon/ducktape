package runner_transport

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"

	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
)

// SecurityMode selects the authentication used on the Runner control
// connection. The zero value is invalid so callers must configure a mode.
type SecurityMode string

const (
	MutualTLS SecurityMode = "mtls"
	Insecure  SecurityMode = "insecure"
)

// Security configures transport authentication. TLS is present only when
// Mode is MutualTLS.
type Security struct {
	Mode SecurityMode `json:"mode" yaml:"mode" mapstructure:"mode"`
	TLS  *Config      `json:"tls,omitempty" yaml:"tls,omitempty" mapstructure:"tls,omitempty"`
}

// Validate rejects absent, unknown, incomplete, or contradictory security
// configuration.
func (s Security) Validate() error {
	switch s.Mode {
	case MutualTLS:
		if s.TLS == nil {
			return errors.New("TLS configuration is required for mtls security")
		}
		for _, file := range []struct{ name, path string }{
			{name: "cert_file", path: s.TLS.CertFile},
			{name: "key_file", path: s.TLS.KeyFile},
			{name: "ca_file", path: s.TLS.CAFile},
		} {
			if file.path == "" {
				return fmt.Errorf("TLS %s is required for mtls security", file.name)
			}
			if !filepath.IsAbs(file.path) {
				return fmt.Errorf("TLS %s must be an absolute path", file.name)
			}
		}
	case Insecure:
		if s.TLS != nil {
			return errors.New("TLS configuration must be omitted for insecure security")
		}
	default:
		if s.Mode == "" {
			return errors.New("security mode is required")
		}
		return fmt.Errorf("unsupported security mode %q", s.Mode)
	}
	return nil
}

// ClientCredentials creates credentials for an outbound Runner control
// connection. Insecure credentials are returned only for explicit Insecure
// mode; mTLS setup errors are returned unchanged and never downgrade.
func (s Security) ClientCredentials(expectedPeerURI string) (credentials.TransportCredentials, error) {
	if err := s.Validate(); err != nil {
		return nil, err
	}
	switch s.Mode {
	case MutualTLS:
		return s.TLS.ClientCredentials(expectedPeerURI)
	case Insecure:
		return insecure.NewCredentials(), nil
	default:
		return nil, fmt.Errorf("unsupported security mode %q", s.Mode)
	}
}

// ServerCredentials creates credentials for an inbound Runner control
// connection. Insecure credentials are returned only for explicit Insecure
// mode; mTLS setup errors are returned unchanged and never downgrade.
func (s Security) ServerCredentials(expectedPeerURI string) (credentials.TransportCredentials, error) {
	if err := s.Validate(); err != nil {
		return nil, err
	}
	switch s.Mode {
	case MutualTLS:
		return s.TLS.ServerCredentials(expectedPeerURI)
	case Insecure:
		return insecure.NewCredentials(), nil
	default:
		return nil, fmt.Errorf("unsupported security mode %q", s.Mode)
	}
}

// AuthorizePeer requires an authenticated peer with exactly expectedPeerURI
// in mTLS mode. Explicit insecure mode skips peer authentication.
func (s Security) AuthorizePeer(ctx context.Context, expectedPeerURI string) error {
	if err := s.Validate(); err != nil {
		return err
	}
	switch s.Mode {
	case Insecure:
		return nil
	case MutualTLS:
		if err := validateIdentityURI(expectedPeerURI); err != nil {
			return fmt.Errorf("invalid expected peer URI: %w", err)
		}
		identity, err := PeerIdentity(ctx)
		if err != nil {
			return err
		}
		if identity != expectedPeerURI {
			return fmt.Errorf("peer identity %q does not match expected identity %q", identity, expectedPeerURI)
		}
		return nil
	default:
		return fmt.Errorf("unsupported security mode %q", s.Mode)
	}
}
