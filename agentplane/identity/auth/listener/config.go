// Package listener configures the SPIFFE authentication for one listener.
package listener

import (
	"crypto/tls"
	"errors"
	"fmt"
	"path/filepath"

	"google.golang.org/grpc/credentials"

	"github.com/agentydragon/ducktape/agentplane/configfile"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
)

const SPIFFE = "spiffe"

type TLS struct {
	CertFile string `mapstructure:"cert_file"`
	KeyFile  string `mapstructure:"key_file"`
}

type SPIFFEAuth struct {
	CAFile string
}

// UnmarshalMapstructure keeps auth entries exact and strictly typed even when
// the enclosing service permits case-insensitive keys or weak conversions.
func (a *SPIFFEAuth) UnmarshalMapstructure(input any) error {
	fields, ok := input.(map[string]any)
	if !ok {
		return errors.New("authentication must be a mapping with string keys")
	}
	modeValue, ok := fields["mode"]
	if !ok {
		return errors.New("authentication mode is required")
	}
	mode, ok := modeValue.(string)
	if !ok {
		return errors.New("authentication mode must be a string")
	}
	if mode != SPIFFE {
		return fmt.Errorf("unknown authentication mode %q", mode)
	}
	var auth struct {
		Mode   string `mapstructure:"mode"`
		CAFile string `mapstructure:"ca_file"`
	}
	if err := configfile.DecodeMapstructure(fields, &auth, configfile.MapstructureOptions{ExactFieldNames: true}); err != nil {
		return err
	}
	if auth.CAFile == "" {
		return errors.New("authentication field \"ca_file\" must be a non-empty string")
	}
	a.CAFile = auth.CAFile
	return nil
}

type Config struct {
	ListenAddress string        `mapstructure:"listen_address"`
	TLS           *TLS          `mapstructure:"tls"`
	Auth          []*SPIFFEAuth `mapstructure:"auth"`
}

func (c Config) Validate() error {
	if c.ListenAddress == "" {
		return errors.New("listen_address is required")
	}
	if len(c.Auth) == 0 {
		return errors.New("auth must enable at least one mode")
	}
	for i, method := range c.Auth {
		if method == nil {
			return errors.New("authentication mode is required")
		}
		if i > 0 {
			return fmt.Errorf("duplicate authentication mode %q", SPIFFE)
		}
		if c.TLS == nil || !filepath.IsAbs(method.CAFile) {
			return errors.New("spiffe requires tls and an absolute ca_file")
		}
	}
	if !filepath.IsAbs(c.TLS.CertFile) || !filepath.IsAbs(c.TLS.KeyFile) {
		return errors.New("tls requires absolute cert_file and key_file paths")
	}
	return nil
}

// HasSPIFFE reports whether auth is configured before full validation.
func (c Config) HasSPIFFE() bool {
	for _, method := range c.Auth {
		if method != nil {
			return true
		}
	}
	return false
}

func (c Config) TLSConfig(localURI string) (*tls.Config, error) {
	return c.tlsConfig(localURI, false, false)
}

func (c Config) tlsConfig(localURI string, publicRPCs, serviceClients bool) (*tls.Config, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	files := peertls.Config{CertFile: c.TLS.CertFile, KeyFile: c.TLS.KeyFile, CAFile: c.Auth[0].CAFile}
	if serviceClients {
		return files.WorkloadServerTLSConfigWithServices(localURI, publicRPCs)
	}
	if publicRPCs {
		return files.WorkloadServerTLSConfigWithPublicHealth(localURI)
	}
	return files.WorkloadServerTLSConfig(localURI)
}

// ServerCredentials uses the listener's verified TLS identity for native RPCs.
// A missing certificate reaches public health methods; private methods still
// require an admitted client certificate. Every presented leaf is verified.
func (c Config) ServerCredentials(localURI string) (credentials.TransportCredentials, error) {
	config, err := c.tlsConfig(localURI, true, false)
	if err != nil {
		return nil, err
	}
	return peertls.ServerCredentials(config)
}

// ServerCredentialsWithServices also verifies first-party service client certificates.
// Every protected method still needs its own exact service allowlist.
func (c Config) ServerCredentialsWithServices(localURI string) (credentials.TransportCredentials, error) {
	config, err := c.tlsConfig(localURI, true, true)
	if err != nil {
		return nil, err
	}
	return peertls.ServerCredentials(config)
}

// TLSConfigWithServices is the HTTP equivalent of ServerCredentialsWithServices.
// Missing certificates reach public health handlers; protected handlers require
// authentication, and every presented client certificate is verified.
func (c Config) TLSConfigWithServices(localURI string) (*tls.Config, error) {
	return c.tlsConfig(localURI, true, true)
}
