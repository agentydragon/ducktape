package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeSettingsFile(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "settings.yaml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

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

func TestSettingsFileIsOverriddenByEnvironmentThenFlags(t *testing.T) {
	path := writeSettingsFile(t, `
proxy_host: file.example
proxy_port: 9001
token_file: /var/run/egress/token
audience_token_files:
  https://kubernetes.test: /var/run/api/token
platform_routes:
  control:
    listen_port: 3130
    upstream: gateway:80
    token_file: /var/run/control/token
listen_port: 3333
`)
	values := map[string]string{
		configFileEnv:                          path,
		"AGENTPLANE_EGRESS_SIDECAR_PROXY_HOST": "env.example",
		"AGENTPLANE_EGRESS_SIDECAR_PROXY_PORT": "9002",
	}
	config, err := loadConfig([]string{"--proxy-port=9003"}, func(name string) string { return values[name] })
	if err != nil {
		t.Fatal(err)
	}
	if config.ProxyHost != "env.example" || config.ProxyPort != 9003 {
		t.Fatalf("flag over environment over file: %+v", config)
	}
	if config.TokenFile != "/var/run/egress/token" || config.ListenPort != 3333 || config.ListenHost != "127.0.0.1" {
		t.Fatalf("file values and defaults for the keys nothing overrides: %+v", config)
	}
	if config.AudienceTokenFiles["https://kubernetes.test"] != "/var/run/api/token" {
		t.Fatalf("audience token files = %#v", config.AudienceTokenFiles)
	}
	if route := config.PlatformRoutes["control"]; route.ListenPort != 3130 || route.Upstream != "gateway:80" || route.TokenFile != "/var/run/control/token" {
		t.Fatalf("platform route = %+v", route)
	}
}

func TestSettingsFileRefusesWhatTheEnvironmentFormRefuses(t *testing.T) {
	const required = "proxy_host: p.example\ntoken_file: /token\n"
	for name, content := range map[string]string{
		"unknown key":           required + "proxy_hots: typo\n",
		"route unknown key":     required + "platform_routes: {control: {listen_port: 1, upstream: u, token_file: /t, extra: true}}\n",
		"route missing key":     required + "platform_routes: {control: {listen_port: 1, upstream: u}}\n",
		"port not an integer":   required + "proxy_port: eight\n",
		"not an object":         "- proxy_host\n",
		"empty":                 "",
		"explicit null routes":  required + "platform_routes: null\n",
		"routes not an object":  required + "platform_routes: [control]\n",
		"audiences not strings": required + "audience_token_files: {a: 1}\n",
	} {
		t.Run(name, func(t *testing.T) {
			values := map[string]string{configFileEnv: writeSettingsFile(t, content)}
			if _, err := loadConfig(nil, func(name string) string { return values[name] }); err == nil {
				t.Fatal("loadConfig succeeded")
			}
		})
	}
	values := map[string]string{configFileEnv: filepath.Join(t.TempDir(), "absent.yaml")}
	if _, err := loadConfig(nil, func(name string) string { return values[name] }); err == nil {
		t.Fatal("a settings file that is not there must be an error, not defaults")
	}
}
