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
	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"gopkg.in/yaml.v3"
)

const configFileEnv = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

type platformRouteMode string

const (
	platformRoutePlaintext  platformRouteMode = "plaintext"
	platformRouteTLSConnect platformRouteMode = "tls_connect"
)

type platformRouteTransportConfig interface {
	mode() platformRouteMode
}

type plaintextRouteTransportConfig struct{ TokenFile string }

func (plaintextRouteTransportConfig) mode() platformRouteMode { return platformRoutePlaintext }

type tlsConnectRouteTransportConfig struct{ ServerURI string }

func (tlsConnectRouteTransportConfig) mode() platformRouteMode { return platformRouteTLSConnect }

type platformRouteConfig struct {
	ServicePrefixes []string                     `yaml:"service_prefixes"`
	Upstream        string                       `yaml:"upstream"`
	Transport       platformRouteTransportConfig `yaml:"transport"`
}

type platformRoutesConfig struct {
	ListenAddress string                         `yaml:"listen_address"`
	Routes        map[string]platformRouteConfig `yaml:"routes"`
}

// UnmarshalYAML keeps route transport and its permitted fields as a sealed variant.
func (c *platformRouteConfig) UnmarshalYAML(node *yaml.Node) error {
	var raw struct {
		ServicePrefixes []string  `yaml:"service_prefixes"`
		Upstream        string    `yaml:"upstream"`
		Transport       yaml.Node `yaml:"transport"`
	}
	if err := decodeKnownFields(node, &raw); err != nil {
		return err
	}
	if raw.Transport.Kind == 0 {
		return errors.New("transport is required")
	}
	transport, err := decodePlatformRouteTransport(&raw.Transport)
	if err != nil {
		return fmt.Errorf("transport: %w", err)
	}
	*c = platformRouteConfig{ServicePrefixes: raw.ServicePrefixes, Upstream: raw.Upstream, Transport: transport}
	return nil
}

func decodePlatformRouteTransport(node *yaml.Node) (platformRouteTransportConfig, error) {
	var transport struct {
		Mode      platformRouteMode `yaml:"mode"`
		TokenFile yaml.Node         `yaml:"token_file"`
		ServerURI yaml.Node         `yaml:"server_uri"`
	}
	if err := decodeKnownFields(node, &transport); err != nil {
		return nil, err
	}
	switch transport.Mode {
	case platformRoutePlaintext:
		if transport.ServerURI.Kind != 0 {
			return nil, errors.New("plaintext route cannot set server_uri")
		}
		var tokenFile string
		if transport.TokenFile.Kind != 0 {
			var err error
			tokenFile, err = requiredYAMLString(&transport.TokenFile, "token_file")
			if err != nil {
				return nil, err
			}
		}
		return plaintextRouteTransportConfig{TokenFile: tokenFile}, nil
	case platformRouteTLSConnect:
		if transport.TokenFile.Kind != 0 {
			return nil, errors.New("TLS CONNECT route cannot set token_file")
		}
		serverURI, err := requiredYAMLString(&transport.ServerURI, "server_uri")
		if err != nil {
			return nil, err
		}
		return tlsConnectRouteTransportConfig{ServerURI: serverURI}, nil
	default:
		return nil, fmt.Errorf("unsupported platform route mode %q", transport.Mode)
	}
}

// spiffeProxyAuth is the workload SVID the sidecar presents to the central proxy, the only hop
// authentication it accepts.
type spiffeProxyAuth struct {
	CertFile string
	KeyFile  string
}

// tlsProxyTransportConfig keeps the YAML's tagged `transport.mode: tls` and `auth.mode: spiffe`, the
// only variants, so existing settings files load unchanged.
type tlsProxyTransportConfig struct {
	CAFile string
	Auth   spiffeProxyAuth
}

type proxyConfig struct {
	Address   string
	Transport tlsProxyTransportConfig
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

func decodeProxyAuth(node *yaml.Node) (spiffeProxyAuth, error) {
	var auth struct {
		Mode     listener.Mode `yaml:"mode"`
		CertFile yaml.Node     `yaml:"cert_file"`
		KeyFile  yaml.Node     `yaml:"key_file"`
	}
	if err := decodeKnownFields(node, &auth); err != nil {
		return spiffeProxyAuth{}, err
	}
	if auth.Mode != listener.SPIFFE {
		return spiffeProxyAuth{}, fmt.Errorf("mode must be %q: %q", listener.SPIFFE, auth.Mode)
	}
	certFile, err := requiredYAMLString(&auth.CertFile, "cert_file")
	if err != nil {
		return spiffeProxyAuth{}, err
	}
	keyFile, err := requiredYAMLString(&auth.KeyFile, "key_file")
	if err != nil {
		return spiffeProxyAuth{}, err
	}
	return spiffeProxyAuth{CertFile: certFile, KeyFile: keyFile}, nil
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

const proxyTransportTLS = "tls"

func decodeProxyTransport(node *yaml.Node) (tlsProxyTransportConfig, error) {
	var transport struct {
		Mode   string    `yaml:"mode"`
		CAFile yaml.Node `yaml:"ca_file"`
		Auth   yaml.Node `yaml:"auth"`
	}
	if err := decodeKnownFields(node, &transport); err != nil {
		return tlsProxyTransportConfig{}, err
	}
	if transport.Mode != proxyTransportTLS {
		return tlsProxyTransportConfig{}, fmt.Errorf("mode must be %q: %q", proxyTransportTLS, transport.Mode)
	}
	caFile, err := requiredYAMLString(&transport.CAFile, "ca_file")
	if err != nil {
		return tlsProxyTransportConfig{}, err
	}
	if transport.Auth.Kind == 0 {
		return tlsProxyTransportConfig{}, errors.New("TLS transport requires auth")
	}
	auth, err := decodeProxyAuth(&transport.Auth)
	if err != nil {
		return tlsProxyTransportConfig{}, fmt.Errorf("auth: %w", err)
	}
	return tlsProxyTransportConfig{CAFile: caFile, Auth: auth}, nil
}

type config struct {
	Proxy                  proxyConfig                    `yaml:"proxy"`
	WorkloadRoutes         map[string]workloadRouteConfig `yaml:"workload_routes"`
	PlatformRoutes         *platformRoutesConfig          `yaml:"platform_routes"`
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
		WorkloadRoutes:         map[string]workloadRouteConfig{},
		PlatformRoutes:         &platformRoutesConfig{Routes: map[string]platformRouteConfig{}},
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
	return c, nil
}

func (c config) validate() error {
	if err := c.Proxy.validate(); err != nil {
		return err
	}
	if c.ListenAddress == "" || c.ReadinessListenAddress == "" {
		return errors.New("listen_address and readiness_listen_address are required")
	}
	if c.PlatformRoutes == nil || c.PlatformRoutes.Routes == nil {
		return errors.New("platform_routes.routes must be an object")
	}
	if len(c.PlatformRoutes.Routes) > 0 && c.PlatformRoutes.ListenAddress == "" {
		return errors.New("platform_routes.listen_address is required when routes are configured")
	}
	seenPrefixes := map[string]string{}
	for name, route := range c.PlatformRoutes.Routes {
		if route.Upstream == "" || route.Transport == nil {
			return fmt.Errorf("platform route %q requires upstream and transport", name)
		}
		if len(route.ServicePrefixes) == 0 {
			return fmt.Errorf("platform route %q requires service_prefixes", name)
		}
		for _, prefix := range route.ServicePrefixes {
			if !validPlatformServicePrefix(prefix) {
				return fmt.Errorf("platform route %q has invalid service prefix %q", name, prefix)
			}
			if previous, exists := seenPrefixes[prefix]; exists {
				return fmt.Errorf("platform service prefix %q is claimed by routes %q and %q", prefix, previous, name)
			}
			seenPrefixes[prefix] = name
		}
		if err := validateAuthority(route.Upstream); err != nil {
			return fmt.Errorf("platform route %q upstream: %w", name, err)
		}
		switch routeTransport := route.Transport.(type) {
		case tlsConnectRouteTransportConfig:
			serverID, err := spiffeid.FromString(routeTransport.ServerURI)
			if err != nil || serverID.String() != routeTransport.ServerURI {
				return fmt.Errorf("platform route %q server_uri must be an exact SPIFFE ID: %q", name, routeTransport.ServerURI)
			}
		case plaintextRouteTransportConfig:
			if routeTransport.TokenFile != "" && !filepath.IsAbs(routeTransport.TokenFile) {
				return fmt.Errorf("platform route %q plaintext token_file must be an absolute path", name)
			}
		default:
			return fmt.Errorf("platform route %q has unsupported transport %T", name, route.Transport)
		}
	}
	if c.WorkloadRoutes == nil {
		return errors.New("workload_routes must be an object")
	}
	for authority, route := range c.WorkloadRoutes {
		if err := validateWorkloadRoute(authority, route); err != nil {
			return fmt.Errorf("workload route %q: %w", authority, err)
		}
		serverID, err := spiffeid.FromString(route.ServerURI)
		if err != nil || serverID.String() != route.ServerURI {
			return fmt.Errorf("workload route %q server_uri must be an exact SPIFFE ID: %q", authority, route.ServerURI)
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
	for name, path := range map[string]string{
		"ca_file": p.Transport.CAFile, "cert_file": p.Transport.Auth.CertFile, "key_file": p.Transport.Auth.KeyFile,
	} {
		if path == "" || !filepath.IsAbs(path) {
			return fmt.Errorf("TLS proxy transport requires an absolute %s", name)
		}
	}
	return nil
}
