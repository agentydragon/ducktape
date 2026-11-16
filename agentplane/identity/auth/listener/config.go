// Package listener configures the authentication methods admitted on one listener.
package listener

import (
	"crypto/tls"
	"errors"
	"fmt"
	"path/filepath"
	"reflect"

	"google.golang.org/grpc/credentials"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/go-viper/mapstructure/v2"
)

type Mode string

const SPIFFE Mode = "spiffe"

type TLS struct {
	CertFile string `mapstructure:"cert_file"`
	KeyFile  string `mapstructure:"key_file"`
}

type Authentication interface {
	Mode() Mode
	authentication()
}

type SPIFFEAuth struct {
	CAFile string
}

func (*SPIFFEAuth) authentication() {}
func (*SPIFFEAuth) Mode() Mode      { return SPIFFE }

// AuthenticationDecodeHook maps the YAML discriminator and variant fields to
// the sealed authentication union. Keeping this at the config boundary lets
// the runtime model omit fields that do not apply to a mode.
func AuthenticationDecodeHook(from, to reflect.Type, data any) (any, error) {
	if to != reflect.TypeOf((*Authentication)(nil)).Elem() || from == nil || from.Kind() != reflect.Map {
		return data, nil
	}
	fields, ok := data.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("authentication must be a mapping with string keys")
	}
	modeValue, ok := fields["mode"]
	if !ok {
		return nil, errors.New("authentication mode is required")
	}
	modeText, ok := modeValue.(string)
	if !ok {
		return nil, errors.New("authentication mode must be a string")
	}
	mode := Mode(modeText)
	switch mode {
	case SPIFFE:
		var auth struct {
			Mode   Mode   `mapstructure:"mode"`
			CAFile string `mapstructure:"ca_file"`
		}
		if err := decodeAuthenticationFields(fields, &auth); err != nil {
			return nil, err
		}
		if auth.CAFile == "" {
			return nil, errors.New("authentication field \"ca_file\" must be a non-empty string")
		}
		return &SPIFFEAuth{CAFile: auth.CAFile}, nil
	default:
		return nil, fmt.Errorf("unknown authentication mode %q", mode)
	}
}

func decodeAuthenticationFields(fields map[string]any, result any) error {
	decoder, err := mapstructure.NewDecoder(&mapstructure.DecoderConfig{
		Result: result, TagName: "mapstructure", ErrorUnused: true,
		MatchName: func(mapKey, fieldName string) bool { return mapKey == fieldName },
	})
	if err != nil {
		return err
	}
	return decoder.Decode(fields)
}

type Config struct {
	ListenAddress string           `mapstructure:"listen_address"`
	TLS           *TLS             `mapstructure:"tls"`
	Auth          []Authentication `mapstructure:"auth"`
}

func (c Config) Validate() error {
	if c.ListenAddress == "" {
		return errors.New("listen_address is required")
	}
	if len(c.Auth) == 0 {
		return errors.New("auth must enable at least one mode")
	}
	seen := make(map[Mode]bool, len(c.Auth))
	for _, method := range c.Auth {
		if method == nil {
			return errors.New("authentication mode is required")
		}
		mode := method.Mode()
		if seen[mode] {
			return fmt.Errorf("duplicate authentication mode %q", mode)
		}
		seen[mode] = true
		switch method := method.(type) {
		case *SPIFFEAuth:
			if method == nil || c.TLS == nil || !filepath.IsAbs(method.CAFile) {
				return errors.New("spiffe requires tls and an absolute ca_file")
			}
		default:
			return fmt.Errorf("unknown authentication mode %q", mode)
		}
	}
	if c.TLS != nil && (!filepath.IsAbs(c.TLS.CertFile) || !filepath.IsAbs(c.TLS.KeyFile)) {
		return errors.New("tls requires absolute cert_file and key_file paths")
	}
	return nil
}

func (c Config) Has(mode Mode) bool {
	for _, method := range c.Auth {
		if method != nil && method.Mode() == mode {
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
	if c.TLS == nil {
		return nil, nil
	}
	files := peertls.Config{CertFile: c.TLS.CertFile, KeyFile: c.TLS.KeyFile}
	for _, method := range c.Auth {
		if spiffe, ok := method.(*SPIFFEAuth); ok && spiffe != nil {
			files.CAFile = spiffe.CAFile
			if serviceClients {
				return files.WorkloadServerTLSConfigWithServices(localURI, publicRPCs)
			}
			if publicRPCs {
				return files.WorkloadServerTLSConfigWithPublicHealth(localURI)
			}
			return files.WorkloadServerTLSConfig(localURI)
		}
	}
	return nil, errors.New("spiffe authentication is required")
}

// ServerCredentials uses the listener's verified TLS identity for native RPCs.
// A missing certificate reaches public health methods; private methods still
// require an admitted client certificate. Every presented leaf is verified.
func (c Config) ServerCredentials(localURI string) (credentials.TransportCredentials, error) {
	config, err := c.tlsConfig(localURI, true, false)
	if err != nil || config == nil {
		return nil, err
	}
	return peertls.ServerCredentials(config)
}

// ServerCredentialsWithServices also verifies first-party service client certificates.
// Every protected method still needs its own exact service allowlist.
func (c Config) ServerCredentialsWithServices(localURI string) (credentials.TransportCredentials, error) {
	config, err := c.tlsConfig(localURI, true, true)
	if err != nil || config == nil {
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
