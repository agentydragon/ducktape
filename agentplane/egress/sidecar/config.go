package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"strconv"
)

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

func loadConfig(args []string, getenv func(string) string) (config, error) {
	c := config{
		ProxyPort:     8888,
		ListenHost:    "127.0.0.1",
		ListenPort:    3128,
		ReadinessHost: "0.0.0.0",
		ReadinessPort: 3129,
	}
	var err error
	if c.ProxyPort, err = envInt(getenv, "AGENTPLANE_EGRESS_SIDECAR_PROXY_PORT", c.ProxyPort); err != nil {
		return config{}, err
	}
	if c.ListenPort, err = envInt(getenv, "AGENTPLANE_EGRESS_SIDECAR_LISTEN_PORT", c.ListenPort); err != nil {
		return config{}, err
	}
	if c.ReadinessPort, err = envInt(getenv, "AGENTPLANE_EGRESS_SIDECAR_READINESS_PORT", c.ReadinessPort); err != nil {
		return config{}, err
	}
	c.ProxyHost = getenv("AGENTPLANE_EGRESS_SIDECAR_PROXY_HOST")
	c.TokenFile = getenv("AGENTPLANE_EGRESS_SIDECAR_TOKEN_FILE")
	c.ListenHost = envString(getenv, "AGENTPLANE_EGRESS_SIDECAR_LISTEN_HOST", c.ListenHost)
	c.ReadinessHost = envString(getenv, "AGENTPLANE_EGRESS_SIDECAR_READINESS_HOST", c.ReadinessHost)
	c.AudienceTokenFiles, err = decodeJSON[map[string]string](getenv("AGENTPLANE_EGRESS_SIDECAR_AUDIENCE_TOKEN_FILES"))
	if err != nil {
		return config{}, fmt.Errorf("AGENTPLANE_EGRESS_SIDECAR_AUDIENCE_TOKEN_FILES: %w", err)
	}
	c.PlatformRoutes, err = decodePlatformRoutes(getenv("AGENTPLANE_EGRESS_SIDECAR_PLATFORM_ROUTES"))
	if err != nil {
		return config{}, fmt.Errorf("AGENTPLANE_EGRESS_SIDECAR_PLATFORM_ROUTES: %w", err)
	}
	if c.AudienceTokenFiles == nil {
		c.AudienceTokenFiles = map[string]string{}
	}
	if c.PlatformRoutes == nil {
		c.PlatformRoutes = map[string]platformRouteConfig{}
	}

	flags := flag.NewFlagSet("agentplane-egress-sidecar", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	flags.StringVar(&c.ProxyHost, "proxy-host", c.ProxyHost, "Host of the central egress proxy")
	flags.IntVar(&c.ProxyPort, "proxy-port", c.ProxyPort, "Port of the central egress proxy")
	flags.StringVar(&c.TokenFile, "token-file", c.TokenFile, "Projected token file for the central egress proxy")
	audienceFiles := flags.String("audience-token-files", "", "JSON object mapping audiences to projected token files")
	routes := flags.String("platform-routes", "", "JSON object mapping route names to route configuration")
	flags.StringVar(&c.ListenHost, "listen-host", c.ListenHost, "HTTP proxy listener address")
	flags.IntVar(&c.ListenPort, "listen-port", c.ListenPort, "HTTP proxy listener port")
	flags.StringVar(&c.ReadinessHost, "readiness-host", c.ReadinessHost, "Readiness listener address")
	flags.IntVar(&c.ReadinessPort, "readiness-port", c.ReadinessPort, "Readiness listener port")
	if err := flags.Parse(args); err != nil {
		return config{}, err
	}
	if flags.NArg() != 0 {
		return config{}, errors.New("unexpected positional arguments")
	}
	if *audienceFiles != "" {
		c.AudienceTokenFiles, err = decodeJSON[map[string]string](*audienceFiles)
		if err != nil {
			return config{}, fmt.Errorf("--audience-token-files: %w", err)
		}
	}
	if *routes != "" {
		c.PlatformRoutes, err = decodePlatformRoutes(*routes)
		if err != nil {
			return config{}, fmt.Errorf("--platform-routes: %w", err)
		}
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

func envString(getenv func(string) string, name, fallback string) string {
	if value := getenv(name); value != "" {
		return value
	}
	return fallback
}

func envInt(getenv func(string) string, name string, fallback int) (int, error) {
	value := getenv(name)
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return 0, fmt.Errorf("%s must be an integer: %w", name, err)
	}
	return parsed, nil
}

func decodeJSON[T any](value string) (T, error) {
	var decoded T
	if value == "" {
		return decoded, nil
	}
	if bytes.Equal(bytes.TrimSpace([]byte(value)), []byte("null")) {
		return decoded, errors.New("expected a JSON object")
	}
	if err := json.Unmarshal([]byte(value), &decoded); err != nil {
		return decoded, err
	}
	return decoded, nil
}

func decodePlatformRoutes(value string) (map[string]platformRouteConfig, error) {
	if value == "" {
		return map[string]platformRouteConfig{}, nil
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal([]byte(value), &raw); err != nil {
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
		return errors.New("proxy-host and token-file are required")
	}
	if c.ProxyPort < 1 || c.ProxyPort > 65535 {
		return fmt.Errorf("proxy-port must be in 1..65535: %d", c.ProxyPort)
	}
	if c.ListenHost == "" || c.ReadinessHost == "" || c.ListenPort < 0 || c.ListenPort > 65535 ||
		c.ReadinessPort < 0 || c.ReadinessPort > 65535 {
		return errors.New("listener hosts are required and ports must be in 0..65535")
	}
	for name, route := range c.PlatformRoutes {
		if route.ListenPort < 0 || route.ListenPort > 65535 || route.Upstream == "" || route.TokenFile == "" {
			return fmt.Errorf("platform route %q requires upstream and token-file and a listen-port in 0..65535", name)
		}
	}
	return nil
}
