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

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	"github.com/spiffe/go-spiffe/v2/spiffeid"
	"gopkg.in/yaml.v3"
)

const configFileEnv = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

type platformRouteMode string

const (
	platformRoutePlaintext        platformRouteMode = "plaintext"
	platformRouteTLSConnect       platformRouteMode = "tls_connect"
	platformRouteTLSBearerConnect platformRouteMode = "tls_bearer_connect"
)

type platformRouteTransportConfig interface {
	mode() platformRouteMode
}

type plaintextRouteTransportConfig struct{ TokenFile string }

func (plaintextRouteTransportConfig) mode() platformRouteMode { return platformRoutePlaintext }

type tlsConnectRouteTransportConfig struct{ ServerURI string }

func (tlsConnectRouteTransportConfig) mode() platformRouteMode { return platformRouteTLSConnect }

type tlsBearerConnectRouteTransportConfig struct{ ServerURI, TokenFile string }

func (tlsBearerConnectRouteTransportConfig) mode() platformRouteMode {
	return platformRouteTLSBearerConnect
}

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
	case platformRouteTLSBearerConnect:
		serverURI, err := requiredYAMLString(&transport.ServerURI, "server_uri")
		if err != nil {
			return nil, err
		}
		tokenFile, err := requiredYAMLString(&transport.TokenFile, "token_file")
		if err != nil {
			return nil, err
		}
		return tlsBearerConnectRouteTransportConfig{ServerURI: serverURI, TokenFile: tokenFile}, nil
	default:
		return nil, fmt.Errorf("unsupported platform route mode %q", transport.Mode)
	}
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
		if err != nil {
			return nil, err
		}
		keyFile, err := requiredYAMLString(&auth.KeyFile, "key_file")
		if err != nil {
			return nil, err
		}
		return spiffeProxyAuth{CertFile: certFile, KeyFile: keyFile}, nil
	case listener.KubernetesBearer:
		if auth.CertFile.Kind != 0 || auth.KeyFile.Kind != 0 {
			return nil, errors.New("kubernetes_bearer authentication cannot set SPIFFE certificate files")
		}
		tokenFile, err := requiredYAMLString(&auth.TokenFile, "token_file")
		if err != nil {
			return nil, err
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
		if err != nil {
			return nil, err
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
		AudienceTokenFiles:     map[string]string{},
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
			proxyTransport, ok := c.Proxy.Transport.(tlsProxyTransportConfig)
			if !ok {
				return fmt.Errorf("platform route %q TLS CONNECT requires TLS proxy transport", name)
			}
			if _, ok := proxyTransport.Auth.(spiffeProxyAuth); !ok {
				return fmt.Errorf("platform route %q TLS CONNECT requires SPIFFE proxy authentication", name)
			}
			serverID, err := spiffeid.FromString(routeTransport.ServerURI)
			if err != nil || serverID.String() != routeTransport.ServerURI {
				return fmt.Errorf("platform route %q server_uri must be an exact SPIFFE ID: %q", name, routeTransport.ServerURI)
			}
		case tlsBearerConnectRouteTransportConfig:
			proxyTransport, ok := c.Proxy.Transport.(tlsProxyTransportConfig)
			if !ok {
				return fmt.Errorf("platform route %q TLS bearer CONNECT requires TLS proxy transport", name)
			}
			if !filepath.IsAbs(routeTransport.TokenFile) {
				return fmt.Errorf("platform route %q token_file must be absolute", name)
			}
			if err := (environment_runtime_transport.ClientTLS{CAFile: proxyTransport.CAFile, ServerURI: routeTransport.ServerURI}).Validate(); err != nil {
				return fmt.Errorf("platform route %q TLS: %w", name, err)
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
	if len(c.WorkloadRoutes) > 0 {
		transport, ok := c.Proxy.Transport.(tlsProxyTransportConfig)
		if !ok {
			return errors.New("workload routes require TLS proxy transport")
		}
		if _, ok := transport.Auth.(spiffeProxyAuth); !ok {
			return errors.New("workload routes require SPIFFE proxy authentication")
		}
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
