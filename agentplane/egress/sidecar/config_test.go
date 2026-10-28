package main

import (
	"os"
	"path/filepath"
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

func settingsFileEnv(path string) func(string) string {
	return func(name string) string {
		if name == configFileEnv {
			return path
		}
		return ""
	}
}

func TestLoadConfigReadsTheSettingsFile(t *testing.T) {
	path := writeSettingsFile(t, `
proxy_host: egress.example
proxy_port: 9001
token_file: /var/run/egress/token
audience_token_files:
  https://kubernetes.test/Audience: /var/run/api/upper-token
  https://kubernetes.test/audience: /var/run/api/lower-token
platform_routes:
  Control.Route:
    listen_address: 127.0.0.1:3130
    upstream: gateway:80
    token_file: /var/run/control/token
listen_address: 127.0.0.1:0
`)
	config, err := loadConfig(settingsFileEnv(path))
	if err != nil {
		t.Fatal(err)
	}
	if config.ProxyHost != "egress.example" || config.GetProxyPort() != 9001 || config.TokenFile != "/var/run/egress/token" {
		t.Fatalf("unexpected proxy settings: %+v", config)
	}
	if config.GetListenAddress() != "127.0.0.1:0" {
		t.Fatalf("listen_address = %q, want the file's value", config.GetListenAddress())
	}
	if config.GetReadinessListenAddress() != "0.0.0.0:3129" {
		t.Fatalf("keys the file omits must keep their defaults: %+v", config)
	}
	if len(config.AudienceTokenFiles) != 2 ||
		config.AudienceTokenFiles["https://kubernetes.test/Audience"] != "/var/run/api/upper-token" ||
		config.AudienceTokenFiles["https://kubernetes.test/audience"] != "/var/run/api/lower-token" {
		t.Fatalf("audience token files = %#v", config.AudienceTokenFiles)
	}
	if route := config.PlatformRoutes["Control.Route"]; route.ListenAddress != "127.0.0.1:3130" || route.Upstream != "gateway:80" || route.TokenFile != "/var/run/control/token" {
		t.Fatalf("platform route = %+v", route)
	}
}

func TestLoadConfigRefusesWhatTheSettingsFileGetsWrong(t *testing.T) {
	const required = "proxy_host: p.example\ntoken_file: /token\n"
	for name, content := range map[string]string{
		"duplicate key":           required + "proxy_host: other.example\n",
		"duplicate route key":     required + "platform_routes: {control: {listen_address: '127.0.0.1:3130', upstream: u, upstream: v}}\n",
		"unknown key":             required + "proxy_hots: typo\n",
		"route unknown key":       required + "platform_routes: {control: {listen_address: 127.0.0.1:3130, upstream: u, token_file: /t, extra: true}}\n",
		"route missing address":   required + "platform_routes: {control: {upstream: u, token_file: /t}}\n",
		"route empty address":     required + "platform_routes: {control: {listen_address: '', upstream: u, token_file: /t}}\n",
		"port zero":               required + "proxy_port: 0\n",
		"port negative":           required + "proxy_port: -1\n",
		"port too large":          required + "proxy_port: 65536\n",
		"empty listen address":    required + "listen_address: ''\n",
		"empty readiness address": required + "readiness_listen_address: ''\n",
		"route missing upstream":  required + "platform_routes: {control: {listen_address: '127.0.0.1:3130'}}\n",
		"port not an integer":     required + "proxy_port: eight\n",
		"not an object":           "- proxy_host\n",
		"empty":                   "",
		"explicit null routes":    required + "platform_routes: null\n",
		"routes not an object":    required + "platform_routes: [control]\n",
		"audience value object":   required + "audience_token_files: {a: {nested: 1}}\n",
		"proxy host missing":      "token_file: /token\n",
		"token file missing":      "proxy_host: p.example\n",
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := loadConfig(settingsFileEnv(writeSettingsFile(t, content))); err == nil {
				t.Fatal("loadConfig succeeded")
			}
		})
	}
}

func TestLoadConfigRefusesAMissingSettingsFile(t *testing.T) {
	absent := filepath.Join(t.TempDir(), "absent.yaml")
	if _, err := loadConfig(settingsFileEnv(absent)); err == nil {
		t.Fatal("a settings file that is not there must be an error, not defaults")
	}
}

func TestLoadConfigRequiresTheSettingsFileVariable(t *testing.T) {
	if _, err := loadConfig(func(string) string { return "" }); err == nil {
		t.Fatalf("loadConfig succeeded without %s", configFileEnv)
	}
}

func TestLoadConfigDefaultsOnlyOmittedSettings(t *testing.T) {
	c, err := loadConfig(settingsFileEnv(writeSettingsFile(t, "proxy_host: p.example\ntoken_file: /token\n")))
	if err != nil {
		t.Fatal(err)
	}
	if c.GetProxyPort() != 8888 || c.GetListenAddress() != "127.0.0.1:3128" || c.GetReadinessListenAddress() != "0.0.0.0:3129" {
		t.Fatalf("unexpected defaults: %v", c)
	}
}

func TestLoadConfigReadsJSON(t *testing.T) {
	c, err := loadConfig(settingsFileEnv(writeSettingsFile(t, `{
		"proxy_host": "p.example", "token_file": "/token", "proxy_port": 9001,
		"platform_routes": {"control": {"listen_address": "127.0.0.1:3130", "upstream": "gateway:80"}}
	}`)))
	if err != nil {
		t.Fatal(err)
	}
	if c.GetProxyPort() != 9001 || c.GetPlatformRoutes()["control"].GetUpstream() != "gateway:80" {
		t.Fatalf("unexpected JSON settings: %v", c)
	}
}
