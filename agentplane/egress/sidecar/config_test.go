package main

import (
	"strings"
	"testing"
)

func TestLoadConfigUsesExistingEnvironmentSchema(t *testing.T) {
	values := map[string]string{
		"AGENTPLANE_EGRESS_SIDECAR_PROXY_HOST":           "egress.example",
		"AGENTPLANE_EGRESS_SIDECAR_TOKEN_FILE":           "/var/run/egress/token",
		"AGENTPLANE_EGRESS_SIDECAR_AUDIENCE_TOKEN_FILES": `{"https://kubernetes.test":"/var/run/api/token"}`,
		"AGENTPLANE_EGRESS_SIDECAR_PLATFORM_ROUTES":      `{"control":{"listen_port":3130,"upstream":"gateway:80","token_file":"/var/run/control/token"}}`,
	}
	config, err := loadConfig(nil, func(name string) string { return values[name] })
	if err != nil {
		t.Fatal(err)
	}
	if config.ProxyHost != "egress.example" || config.ProxyPort != 8888 || config.TokenFile != "/var/run/egress/token" {
		t.Fatalf("unexpected proxy settings: %+v", config)
	}
	if config.ListenHost != "127.0.0.1" || config.ListenPort != 3128 || config.ReadinessHost != "0.0.0.0" || config.ReadinessPort != 3129 {
		t.Fatalf("unexpected listener defaults: %+v", config)
	}
	if config.AudienceTokenFiles["https://kubernetes.test"] != "/var/run/api/token" {
		t.Fatalf("audience token files = %#v", config.AudienceTokenFiles)
	}
	if route := config.PlatformRoutes["control"]; route.ListenPort != 3130 || route.Upstream != "gateway:80" || route.TokenFile != "/var/run/control/token" {
		t.Fatalf("platform route = %+v", route)
	}
}

func TestCommandLineOverridesEnvironment(t *testing.T) {
	values := map[string]string{
		"AGENTPLANE_EGRESS_SIDECAR_PROXY_HOST": "env.example",
		"AGENTPLANE_EGRESS_SIDECAR_PROXY_PORT": "8888",
		"AGENTPLANE_EGRESS_SIDECAR_TOKEN_FILE": "/var/run/egress/token",
	}
	config, err := loadConfig([]string{"--proxy-host=flag.example", "--proxy-port=9999", "--listen-port=0"}, func(name string) string {
		return values[name]
	})
	if err != nil {
		t.Fatal(err)
	}
	if config.ProxyHost != "flag.example" || config.ProxyPort != 9999 || config.ListenPort != 0 {
		t.Fatalf("command line values did not override environment: %+v", config)
	}
}

func TestPlatformRoutesRejectUnknownAndMissingFields(t *testing.T) {
	for _, value := range []string{
		`{"control":{"listen_port":3130,"upstream":"gateway:80","token_file":"/token","extra":true}}`,
		`{"control":{"listen_port":3130,"upstream":"gateway:80"}}`,
	} {
		_, err := decodePlatformRoutes(value)
		if err == nil {
			t.Fatalf("decodePlatformRoutes(%s) succeeded", value)
		}
	}
	_, err := decodePlatformRoutes(`{"control":{"listen_port":3130,"upstream":"gateway:80","token_file":"/token","value":true} trailing`)
	if err == nil || !strings.Contains(err.Error(), "invalid character") {
		t.Fatalf("malformed route JSON error = %v", err)
	}
}
