package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

const configFileEnv = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

type platformRouteConfig struct {
	ListenAddress string `yaml:"listen_address"`
	Upstream      string `yaml:"upstream"`
	TokenFile     string `yaml:"token_file,omitempty"`
}

type proxyTLSConfig struct {
	CertFile string `yaml:"cert_file"`
	KeyFile  string `yaml:"key_file"`
	CAFile   string `yaml:"ca_file"`
}

type config struct {
	WorkloadRoutes         map[string]workloadRouteConfig `yaml:"workload_routes"`
	ProxyHost              string                         `yaml:"proxy_host"`
	ProxyPort              int                            `yaml:"proxy_port"`
	ProxyMode              string                         `yaml:"proxy_mode"`
	TokenFile              string                         `yaml:"token_file,omitempty"`
	ProxyTLS               *proxyTLSConfig                `yaml:"proxy_tls,omitempty"`
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
		ProxyPort:              8889,
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
	if c.ProxyHost == "" {
		return errors.New("proxy_host is required")
	}
	switch c.ProxyMode {
	case "token":
		if c.ProxyTLS != nil {
			return errors.New("token mode forbids proxy_tls")
		}
	case "mtls":
		if c.TokenFile != "" {
			return errors.New("mtls mode forbids token_file")
		}
		if c.ProxyTLS == nil {
			return errors.New("mtls mode requires proxy_tls")
		}
		for _, file := range []string{c.ProxyTLS.CertFile, c.ProxyTLS.KeyFile, c.ProxyTLS.CAFile} {
			if file == "" || !filepath.IsAbs(file) {
				return errors.New("mtls mode requires absolute cert_file, key_file and ca_file paths")
			}
		}
	default:
		return fmt.Errorf("proxy_mode must be token or mtls: %q", c.ProxyMode)
	}
	if c.ProxyPort < 1 || c.ProxyPort > 65535 {
		return fmt.Errorf("proxy_port must be in 1..65535: %d", c.ProxyPort)
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
	for authority, route := range c.WorkloadRoutes {
		if err := validateWorkloadRoute(authority, route); err != nil {
			return err
		}
	}
	return nil
}
