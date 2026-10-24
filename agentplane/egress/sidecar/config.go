package main

import (
	"errors"
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

const configFileEnv = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

type platformRouteConfig struct {
	ListenAddress string `yaml:"listen_address"`
	Upstream      string `yaml:"upstream"`
	TokenFile     string `yaml:"token_file,omitempty"`
}

type config struct {
	ProxyHost              string                         `yaml:"proxy_host"`
	ProxyPort              int                            `yaml:"proxy_port"`
	TokenFile              string                         `yaml:"token_file"`
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
		ProxyPort:              8888,
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
	if c.ProxyHost == "" || c.TokenFile == "" {
		return errors.New("proxy_host and token_file are required")
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
	return nil
}
