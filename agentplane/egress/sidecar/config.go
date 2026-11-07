package main

import (
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	workloadlistener "github.com/agentydragon/ducktape/agentplane/workload_auth/listener"

	"gopkg.in/yaml.v3"
)

const configFileEnv = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

type platformRouteConfig struct {
	ListenAddress string `yaml:"listen_address"`
	Upstream      string `yaml:"upstream"`
	TokenFile     string `yaml:"token_file,omitempty"`
}

type proxyTransportMode string

const proxyTransportPlaintext proxyTransportMode = "plaintext"

type proxyAuthConfig struct {
	Mode      workloadlistener.Mode `yaml:"mode"`
	TokenFile string                `yaml:"token_file"`
}

type proxyTransportConfig struct {
	Mode proxyTransportMode `yaml:"mode"`
	Auth proxyAuthConfig    `yaml:"auth"`
}

type proxyConfig struct {
	Address   string               `yaml:"address"`
	Transport proxyTransportConfig `yaml:"transport"`
}

type config struct {
	Proxy                  proxyConfig                    `yaml:"proxy"`
	AudienceTokenFiles     map[string]string              `yaml:"audience_token_files"`
	PlatformRoutes         map[string]platformRouteConfig `yaml:"platform_routes"`
	ListenAddress          string                         `yaml:"listen_address"`
	ReadinessListenAddress string                         `yaml:"readiness_listen_address"`
}

// loadConfig applies the settings file named by the configFileEnv variable over the defaults.
func loadConfig(getenv func(string) string) (config, error) {
	path := getenv(configFileEnv)
	if path == "" {
		return config{}, fmt.Errorf("%s must name the YAML settings file", configFileEnv)
	}
	c := config{
		AudienceTokenFiles:     map[string]string{},
		PlatformRoutes:         map[string]platformRouteConfig{},
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
	return nil
}

func (p proxyConfig) validate() error {
	if p.Address == "" || strings.ContainsAny(p.Address, "/\\?#@ \t\r\n") {
		return errors.New("proxy.address must be an exact host:port")
	}
	host, port, err := net.SplitHostPort(p.Address)
	if err != nil || host == "" {
		return fmt.Errorf("proxy.address must be an exact host:port: %q", p.Address)
	}
	if strings.HasPrefix(p.Address, "[") {
		if !strings.Contains(host, ":") || net.ParseIP(host) == nil {
			return fmt.Errorf("bracketed proxy.address must contain an IPv6 host: %q", p.Address)
		}
	} else if strings.Contains(host, ":") {
		return fmt.Errorf("IPv6 proxy.address must use brackets: %q", p.Address)
	}
	if port == "" || strings.Trim(port, "0123456789") != "" {
		return fmt.Errorf("proxy.address port must be in 1..65535: %q", p.Address)
	}
	portNumber, err := strconv.Atoi(port)
	if err != nil || portNumber < 1 || portNumber > 65535 {
		return fmt.Errorf("proxy.address port must be in 1..65535: %q", p.Address)
	}
	if p.Transport.Mode != proxyTransportPlaintext {
		return fmt.Errorf("proxy.transport.mode must be %q: %q", proxyTransportPlaintext, p.Transport.Mode)
	}
	if p.Transport.Auth.Mode != workloadlistener.KubernetesBearer {
		return fmt.Errorf("proxy.transport.auth.mode must be %q: %q", workloadlistener.KubernetesBearer, p.Transport.Auth.Mode)
	}
	if p.Transport.Auth.TokenFile == "" || !filepath.IsAbs(p.Transport.Auth.TokenFile) {
		return errors.New("kubernetes_bearer authentication requires an absolute token_file")
	}
	return nil
}
