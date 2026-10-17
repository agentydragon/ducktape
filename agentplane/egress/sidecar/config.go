package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"

	"gopkg.in/yaml.v3"
)

const configFileEnv = "AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE"

type platformRouteConfig struct {
	ListenPort int    `json:"listen_port"`
	Upstream   string `json:"upstream"`
	TokenFile  string `json:"token_file"`
}

type config struct {
	ProxyHost          string
	ProxyPort          int
	TokenFile          string
	AudienceTokenFiles map[string]string
	PlatformRoutes     map[string]platformRouteConfig
	ListenHost         string
	ListenPort         int
	ReadinessHost      string
	ReadinessPort      int
}

// fileConfig is the settings file named by AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE; its keys are the
// fields of Settings in agentplane/egress/sidecar_config.py, which cdk8s renders it from. A key the
// file omits keeps its default.
type fileConfig struct {
	ProxyHost          *string           `json:"proxy_host"`
	ProxyPort          *int              `json:"proxy_port"`
	TokenFile          *string           `json:"token_file"`
	AudienceTokenFiles map[string]string `json:"audience_token_files"`
	PlatformRoutes     json.RawMessage   `json:"platform_routes"`
	ListenHost         *string           `json:"listen_host"`
	ListenPort         *int              `json:"listen_port"`
	ReadinessHost      *string           `json:"readiness_host"`
	ReadinessPort      *int              `json:"readiness_port"`
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
	if err := readSettingsFile(path, &c); err != nil {
		return config{}, err
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

// readSettingsFile overlays the YAML object in path onto c. The file is converted to JSON and read
// strictly: an unknown key, an unknown route key or a route missing a key is an error.
func readSettingsFile(path string, c *config) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var document any
	if err := yaml.Unmarshal(data, &document); err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	if _, isObject := document.(map[string]any); !isObject {
		return fmt.Errorf("%s: expected a YAML object", path)
	}
	asJSON, err := json.Marshal(document)
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	var file fileConfig
	decoder := json.NewDecoder(bytes.NewReader(asJSON))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&file); err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	overlay(&c.ProxyHost, file.ProxyHost)
	overlay(&c.ProxyPort, file.ProxyPort)
	overlay(&c.TokenFile, file.TokenFile)
	overlay(&c.ListenHost, file.ListenHost)
	overlay(&c.ListenPort, file.ListenPort)
	overlay(&c.ReadinessHost, file.ReadinessHost)
	overlay(&c.ReadinessPort, file.ReadinessPort)
	if file.AudienceTokenFiles != nil {
		c.AudienceTokenFiles = file.AudienceTokenFiles
	}
	if file.PlatformRoutes != nil {
		if c.PlatformRoutes, err = decodePlatformRoutes(file.PlatformRoutes); err != nil {
			return fmt.Errorf("%s: platform_routes: %w", path, err)
		}
	}
	return nil
}

func overlay[T any](to, from *T) {
	if from != nil {
		*to = *from
	}
}

func decodePlatformRoutes(value json.RawMessage) (map[string]platformRouteConfig, error) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(value, &raw); err != nil {
		return nil, err
	}
	if raw == nil {
		return nil, errors.New("expected a JSON object")
	}
	routes := make(map[string]platformRouteConfig, len(raw))
	for name, data := range raw {
		var route struct {
			ListenPort *int    `json:"listen_port"`
			Upstream   *string `json:"upstream"`
			TokenFile  *string `json:"token_file"`
		}
		decoder := json.NewDecoder(bytes.NewReader(data))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&route); err != nil {
			return nil, fmt.Errorf("route %q: %w", name, err)
		}
		if err := decoder.Decode(new(any)); err != io.EOF {
			return nil, fmt.Errorf("route %q: trailing JSON data", name)
		}
		if route.ListenPort == nil || route.Upstream == nil || route.TokenFile == nil {
			return nil, fmt.Errorf("route %q requires listen_port, upstream and token_file", name)
		}
		routes[name] = platformRouteConfig{ListenPort: *route.ListenPort, Upstream: *route.Upstream, TokenFile: *route.TokenFile}
	}
	return routes, nil
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
	for name, route := range c.PlatformRoutes {
		if route.ListenPort < 0 || route.ListenPort > 65535 || route.Upstream == "" || route.TokenFile == "" {
			return fmt.Errorf("platform route %q requires upstream and token_file and a listen_port in 0..65535", name)
		}
	}
	return nil
}
