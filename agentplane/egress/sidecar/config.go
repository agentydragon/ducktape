package main

import (
	"bytes"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	"gopkg.in/yaml.v3"
)

const configFileEnv = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

type platformRouteConfig struct {
	ListenAddress string `yaml:"listen_address"`
	Upstream      string `yaml:"upstream"`
	TokenFile     string `yaml:"token_file,omitempty"`
}

type proxyAuth interface {
	mode() listener.Mode
	requestToken() (string, error)
}

type spiffeProxyAuth struct {
	CertFile string
	KeyFile  string
}

func (spiffeProxyAuth) mode() listener.Mode           { return listener.SPIFFE }
func (spiffeProxyAuth) requestToken() (string, error) { return "", nil }

type kubernetesBearerProxyAuth struct {
	TokenFile string
}

func (kubernetesBearerProxyAuth) mode() listener.Mode { return listener.KubernetesBearer }
func (auth kubernetesBearerProxyAuth) requestToken() (string, error) {
	return readToken(auth.TokenFile)
}

type proxyTransportMode string

const (
	proxyTransportPlaintext proxyTransportMode = "plaintext"
	proxyTransportTLS       proxyTransportMode = "tls"
)

type proxyTransportConfig interface {
	mode() proxyTransportMode
	auth() proxyAuth
}

type plaintextProxyTransportConfig struct {
	Auth kubernetesBearerProxyAuth
}

func (plaintextProxyTransportConfig) mode() proxyTransportMode  { return proxyTransportPlaintext }
func (transport plaintextProxyTransportConfig) auth() proxyAuth { return transport.Auth }

type tlsProxyTransportConfig struct {
	CAFile string
	Auth   proxyAuth
}

func (tlsProxyTransportConfig) mode() proxyTransportMode  { return proxyTransportTLS }
func (transport tlsProxyTransportConfig) auth() proxyAuth { return transport.Auth }

type proxyConfig struct {
	Address   string
	Transport proxyTransportConfig
}

// UnmarshalYAML is the narrow boundary that converts the tagged YAML auth and
// transport objects into sealed Go variants. Variant fields remain strict.
func (c *proxyConfig) UnmarshalYAML(node *yaml.Node) error {
	var raw struct {
		Address   string    `yaml:"address"`
		Transport yaml.Node `yaml:"transport"`
	}
	if err := decodeKnownFields(node, &raw); err != nil {
		return err
	}
	if raw.Address == "" {
		return errors.New("proxy.address is required")
	}
	if raw.Transport.Kind == 0 {
		return errors.New("proxy.transport is required")
	}
	transport, err := decodeProxyTransport(&raw.Transport)
	if err != nil {
		return fmt.Errorf("proxy.transport: %w", err)
	}
	*c = proxyConfig{Address: raw.Address, Transport: transport}
	return nil
}

func decodeKnownFields(node *yaml.Node, result any) error {
	var contents bytes.Buffer
	if err := yaml.NewEncoder(&contents).Encode(node); err != nil {
		return err
	}
	decoder := yaml.NewDecoder(&contents)
	decoder.KnownFields(true)
	return decoder.Decode(result)
}

func decodeProxyAuth(node *yaml.Node) (proxyAuth, error) {
	var auth struct {
		Mode      listener.Mode `yaml:"mode"`
		CertFile  yaml.Node     `yaml:"cert_file"`
		KeyFile   yaml.Node     `yaml:"key_file"`
		TokenFile yaml.Node     `yaml:"token_file"`
	}
	if err := decodeKnownFields(node, &auth); err != nil {
		return nil, err
	}
	switch auth.Mode {
	case listener.SPIFFE:
		if auth.TokenFile.Kind != 0 {
			return nil, errors.New("SPIFFE authentication cannot set token_file")
		}
		certFile, err := requiredYAMLString(&auth.CertFile, "cert_file")
		if err != nil || !filepath.IsAbs(certFile) {
			return nil, errors.New("SPIFFE authentication requires an absolute cert_file")
		}
		keyFile, err := requiredYAMLString(&auth.KeyFile, "key_file")
		if err != nil || !filepath.IsAbs(keyFile) {
			return nil, errors.New("SPIFFE authentication requires an absolute key_file")
		}
		return spiffeProxyAuth{CertFile: certFile, KeyFile: keyFile}, nil
	case listener.KubernetesBearer:
		if auth.CertFile.Kind != 0 || auth.KeyFile.Kind != 0 {
			return nil, errors.New("kubernetes_bearer authentication cannot set SPIFFE certificate files")
		}
		tokenFile, err := requiredYAMLString(&auth.TokenFile, "token_file")
		if err != nil || !filepath.IsAbs(tokenFile) {
			return nil, errors.New("kubernetes_bearer authentication requires an absolute token_file")
		}
		return kubernetesBearerProxyAuth{TokenFile: tokenFile}, nil
	default:
		return nil, fmt.Errorf("mode must be %q or %q: %q", listener.SPIFFE, listener.KubernetesBearer, auth.Mode)
	}
}

func requiredYAMLString(node *yaml.Node, name string) (string, error) {
	if node.Kind == 0 {
		return "", fmt.Errorf("%s is required", name)
	}
	var value string
	if err := node.Decode(&value); err != nil {
		return "", fmt.Errorf("%s must be a string: %w", name, err)
	}
	if value == "" {
		return "", fmt.Errorf("%s must not be empty", name)
	}
	return value, nil
}

func decodeProxyTransport(node *yaml.Node) (proxyTransportConfig, error) {
	var transport struct {
		Mode   proxyTransportMode `yaml:"mode"`
		CAFile yaml.Node          `yaml:"ca_file"`
		Auth   yaml.Node          `yaml:"auth"`
	}
	if err := decodeKnownFields(node, &transport); err != nil {
		return nil, err
	}
	switch transport.Mode {
	case proxyTransportPlaintext:
		if transport.CAFile.Kind != 0 {
			return nil, errors.New("plaintext transport cannot set ca_file")
		}
		if transport.Auth.Kind == 0 {
			return nil, errors.New("plaintext transport requires bearer auth")
		}
		auth, err := decodeProxyAuth(&transport.Auth)
		if err != nil {
			return nil, fmt.Errorf("auth: %w", err)
		}
		bearer, okay := auth.(kubernetesBearerProxyAuth)
		if !okay {
			return nil, errors.New("plaintext transport only supports kubernetes_bearer auth")
		}
		return plaintextProxyTransportConfig{Auth: bearer}, nil
	case proxyTransportTLS:
		caFile, err := requiredYAMLString(&transport.CAFile, "ca_file")
		if err != nil || !filepath.IsAbs(caFile) {
			return nil, errors.New("TLS transport requires an absolute ca_file")
		}
		if transport.Auth.Kind == 0 {
			return nil, errors.New("TLS transport requires auth")
		}
		auth, err := decodeProxyAuth(&transport.Auth)
		if err != nil {
			return nil, fmt.Errorf("auth: %w", err)
		}
		return tlsProxyTransportConfig{CAFile: caFile, Auth: auth}, nil
	default:
		return nil, fmt.Errorf("mode must be %q or %q: %q", proxyTransportPlaintext, proxyTransportTLS, transport.Mode)
	}
}

type config struct {
	Proxy                  proxyConfig                    `yaml:"proxy"`
	AudienceTokenFiles     map[string]string              `yaml:"audience_token_files"`
	RulesAPI               string                         `yaml:"rules_api,omitempty"`
	PlatformRoutes         map[string]platformRouteConfig `yaml:"platform_routes"`
	ListenAddress          string                         `yaml:"listen_address"`
	ReadinessListenAddress string                         `yaml:"readiness_listen_address"`
}

// loadConfig reads the only settings source used by the process.
func loadConfig(getenv func(string) string) (config, error) {
	path := getenv(configFileEnv)
	if path == "" {
		return config{}, fmt.Errorf("%s must name the YAML settings file", configFileEnv)
	}
	c := config{
		AudienceTokenFiles:     make(map[string]string),
		PlatformRoutes:         make(map[string]platformRouteConfig),
		ListenAddress:          "127.0.0.1:3128",
		ReadinessListenAddress: "0.0.0.0:3129",
	}
	file, err := os.Open(path)
	if err != nil {
		return config{}, err
	}
	defer file.Close()
	decoder := yaml.NewDecoder(file)
	decoder.KnownFields(true)
	if err := decoder.Decode(&c); err != nil {
		return config{}, fmt.Errorf("%s: %w", path, err)
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

func (c config) validate() error {
	if err := c.Proxy.validate(); err != nil {
		return err
	}
	if c.ListenAddress == "" || c.ReadinessListenAddress == "" {
		return errors.New("listen_address and readiness_listen_address are required")
	}
	if c.PlatformRoutes == nil {
		return errors.New("platform_routes must be an object")
	}
	for name, route := range c.PlatformRoutes {
		if route.ListenAddress == "" || route.Upstream == "" {
			return fmt.Errorf("platform route %q requires listen_address and upstream", name)
		}
	}
	if c.RulesAPI != "" {
		if _, ok := c.Proxy.Transport.(tlsProxyTransportConfig); !ok {
			return errors.New("rules_api requires TLS proxy transport")
		}
		if err := validateAuthority(c.RulesAPI); err != nil {
			return fmt.Errorf("rules_api: %w", err)
		}
	}
	return nil
}

func validateAuthority(address string) error {
	if strings.ContainsAny(address, "/\\?#@ \t\r\n") {
		return fmt.Errorf("expected an exact host:port: %q", address)
	}
	host, port, err := net.SplitHostPort(address)
	if err != nil || host == "" {
		return fmt.Errorf("expected an exact host:port: %q", address)
	}
	number, err := strconv.Atoi(port)
	if err != nil || number < 1 || number > 65535 {
		return fmt.Errorf("port must be in 1..65535: %q", address)
	}
	return nil
}

func (p proxyConfig) validate() error {
	if err := validateAuthority(p.Address); err != nil {
		return fmt.Errorf("proxy.address: %w", err)
	}
	if p.Transport == nil {
		return errors.New("proxy.transport is required")
	}
	switch transport := p.Transport.(type) {
	case plaintextProxyTransportConfig:
		auth := transport.Auth
		if auth.TokenFile == "" || !filepath.IsAbs(auth.TokenFile) {
			return errors.New("kubernetes_bearer authentication requires an absolute token_file")
		}
	case tlsProxyTransportConfig:
		if transport.CAFile == "" || !filepath.IsAbs(transport.CAFile) {
			return errors.New("TLS transport requires an absolute ca_file")
		}
		switch auth := transport.Auth.(type) {
		case spiffeProxyAuth:
			for name, path := range map[string]string{"cert_file": auth.CertFile, "key_file": auth.KeyFile} {
				if path == "" || !filepath.IsAbs(path) {
					return fmt.Errorf("SPIFFE authentication requires an absolute %s", name)
				}
			}
		case kubernetesBearerProxyAuth:
			if auth.TokenFile == "" || !filepath.IsAbs(auth.TokenFile) {
				return errors.New("kubernetes_bearer authentication requires an absolute token_file")
			}
		default:
			return fmt.Errorf("unsupported proxy authentication type %T", auth)
		}
	default:
		return fmt.Errorf("unsupported proxy transport type %T", transport)
	}
	return nil
}
