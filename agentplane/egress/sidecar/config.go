package main

import (
	"errors"
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

const configFileEnv = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

// A pointer distinguishes a missing listen_port from an explicit ephemeral port (0).
type platformRouteConfig struct {
	ListenPort *int   `yaml:"listen_port"`
	Upstream   string `yaml:"upstream"`
	TokenFile  string `yaml:"token_file"`
}

type config struct {
	ProxyHost          string                         `yaml:"proxy_host"`
	ProxyPort          int                            `yaml:"proxy_port"`
	TokenFile          string                         `yaml:"token_file"`
	AudienceTokenFiles map[string]string              `yaml:"audience_token_files"`
	PlatformRoutes     map[string]platformRouteConfig `yaml:"platform_routes"`
	ListenHost         string                         `yaml:"listen_host"`
	ListenPort         int                            `yaml:"listen_port"`
	ReadinessHost      string                         `yaml:"readiness_host"`
	ReadinessPort      int                            `yaml:"readiness_port"`
}

// loadConfig applies the settings file named by the configFileEnv variable over the defaults.
func loadConfig(getenv func(string) string) (config, error) {
	path := getenv(configFileEnv)
	if path == "" {
		return config{}, fmt.Errorf("%s must name the YAML settings file", configFileEnv)
	}
	c := config{
		AudienceTokenFiles: map[string]string{},
		PlatformRoutes:     map[string]platformRouteConfig{},
		ProxyPort:          8888,
		ListenHost:         "127.0.0.1",
		ListenPort:         3128,
		ReadinessHost:      "0.0.0.0",
		ReadinessPort:      3129,
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
	if c.ProxyHost == "" || c.TokenFile == "" {
		return errors.New("proxy_host and token_file are required")
	}
	if c.ProxyPort < 1 || c.ProxyPort > 65535 {
		return fmt.Errorf("proxy_port must be in 1..65535: %d", c.ProxyPort)
	}
	if c.ListenHost == "" || c.ReadinessHost == "" || c.ListenPort < 0 || c.ListenPort > 65535 ||
		c.ReadinessPort < 0 || c.ReadinessPort > 65535 {
		return errors.New("listener hosts are required and ports must be in 0..65535")
	}
	if c.PlatformRoutes == nil {
		return errors.New("platform_routes must be an object")
	}
	for name, route := range c.PlatformRoutes {
		if route.ListenPort == nil || *route.ListenPort < 0 || *route.ListenPort > 65535 || route.Upstream == "" || route.TokenFile == "" {
			return fmt.Errorf("platform route %q requires upstream and token_file and a listen_port in 0..65535", name)
		}
	}
	return nil
}
