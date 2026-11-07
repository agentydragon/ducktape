package main

import (
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
	if err := ensureKnownKeys(node, "address", "transport"); err != nil {
		return err
	}
	var raw struct {
		Address   string    `yaml:"address"`
		Transport yaml.Node `yaml:"transport"`
	}
	if err := node.Decode(&raw); err != nil {
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

func ensureKnownKeys(node *yaml.Node, allowed ...string) error {
	if node.Kind != yaml.MappingNode {
		return errors.New("must be an object")
	}
	known := make(map[string]struct{}, len(allowed))
	for _, key := range allowed {
		known[key] = struct{}{}
	}
	seen := make(map[string]struct{}, len(node.Content)/2)
	for index := 0; index < len(node.Content); index += 2 {
		key := node.Content[index].Value
		if _, okay := known[key]; !okay {
			return fmt.Errorf("unknown field %q", key)
		}
		if _, okay := seen[key]; okay {
			return fmt.Errorf("duplicate field %q", key)
		}
		seen[key] = struct{}{}
	}
	return nil
}

func decodeProxyAuth(node *yaml.Node) (proxyAuth, error) {
	var tag struct {
		Mode listener.Mode `yaml:"mode"`
	}
	if err := node.Decode(&tag); err != nil {
		return nil, err
	}
	switch tag.Mode {
	case listener.SPIFFE:
		if err := ensureKnownKeys(node, "mode", "cert_file", "key_file"); err != nil {
			return nil, err
		}
		var auth struct {
			Mode     listener.Mode `yaml:"mode"`
			CertFile string        `yaml:"cert_file"`
			KeyFile  string        `yaml:"key_file"`
		}
		if err := node.Decode(&auth); err != nil {
			return nil, err
		}
		for name, path := range map[string]string{
			"cert_file": auth.CertFile,
			"key_file":  auth.KeyFile,
		} {
			if path == "" || !filepath.IsAbs(path) {
				return nil, fmt.Errorf("SPIFFE authentication requires an absolute %s", name)
			}
		}
		return spiffeProxyAuth{CertFile: auth.CertFile, KeyFile: auth.KeyFile}, nil
	case listener.KubernetesBearer:
		if err := ensureKnownKeys(node, "mode", "token_file"); err != nil {
			return nil, err
		}
		var auth struct {
			Mode      listener.Mode `yaml:"mode"`
			TokenFile string        `yaml:"token_file"`
		}
		if err := node.Decode(&auth); err != nil {
			return nil, err
		}
		if auth.TokenFile == "" || !filepath.IsAbs(auth.TokenFile) {
			return nil, errors.New("kubernetes_bearer authentication requires an absolute token_file")
		}
		return kubernetesBearerProxyAuth{TokenFile: auth.TokenFile}, nil
	default:
		return nil, fmt.Errorf("mode must be %q or %q: %q", listener.SPIFFE, listener.KubernetesBearer, tag.Mode)
	}
}

func decodeProxyTransport(node *yaml.Node) (proxyTransportConfig, error) {
	var tag struct {
		Mode proxyTransportMode `yaml:"mode"`
	}
	if err := node.Decode(&tag); err != nil {
		return nil, err
	}
	switch tag.Mode {
	case proxyTransportPlaintext:
		if err := ensureKnownKeys(node, "mode", "auth"); err != nil {
			return nil, err
		}
		var transport struct {
			Mode proxyTransportMode `yaml:"mode"`
			Auth yaml.Node          `yaml:"auth"`
		}
		if err := node.Decode(&transport); err != nil {
			return nil, err
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
		if err := ensureKnownKeys(node, "mode", "ca_file", "auth"); err != nil {
			return nil, err
		}
		var transport struct {
			Mode   proxyTransportMode `yaml:"mode"`
			CAFile string             `yaml:"ca_file"`
			Auth   yaml.Node          `yaml:"auth"`
		}
		if err := node.Decode(&transport); err != nil {
			return nil, err
		}
		if transport.CAFile == "" || !filepath.IsAbs(transport.CAFile) {
			return nil, errors.New("TLS transport requires an absolute ca_file")
		}
		if transport.Auth.Kind == 0 {
			return nil, errors.New("TLS transport requires auth")
		}
		auth, err := decodeProxyAuth(&transport.Auth)
		if err != nil {
			return nil, fmt.Errorf("auth: %w", err)
		}
		return tlsProxyTransportConfig{CAFile: transport.CAFile, Auth: auth}, nil
	default:
		return nil, fmt.Errorf("mode must be %q or %q: %q", proxyTransportPlaintext, proxyTransportTLS, tag.Mode)
	}
}

type config struct {
	Proxy                  proxyConfig                    `yaml:"proxy"`
	AudienceTokenFiles     map[string]string              `yaml:"audience_token_files"`
	WorkloadRoutes         map[string]workloadRouteConfig `yaml:"workload_routes"`
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
		WorkloadRoutes:         make(map[string]workloadRouteConfig),
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
	if c.WorkloadRoutes == nil {
		return errors.New("workload_routes must be an object")
	}
	for authority, route := range c.WorkloadRoutes {
		if err := validateWorkloadRoute(authority, route); err != nil {
			return fmt.Errorf("workload route %q: %w", authority, err)
		}
	}
	return nil
}

func (p proxyConfig) validate() error {
	if p.Address == "" {
		return errors.New("proxy.address is required")
	}
	if strings.ContainsAny(p.Address, "/\\?#@ \t\r\n") {
		return fmt.Errorf("proxy.address must be a host:port: %q", p.Address)
	}
	host, port, err := net.SplitHostPort(p.Address)
	if err != nil || host == "" {
		return fmt.Errorf("proxy.address must be a host:port: %q", p.Address)
	}
	number, err := strconv.Atoi(port)
	if err != nil || number < 1 || number > 65535 {
		return fmt.Errorf("proxy.address port must be in 1..65535: %q", p.Address)
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
