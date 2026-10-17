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
  https://kubernetes.test: /var/run/api/token
platform_routes:
  control:
    listen_port: 3130
    upstream: gateway:80
    token_file: /var/run/control/token
listen_port: 3333
`)
	config, err := loadConfig(settingsFileEnv(path))
	if err != nil {
		t.Fatal(err)
	}
	if config.ProxyHost != "egress.example" || config.ProxyPort != 9001 || config.TokenFile != "/var/run/egress/token" {
		t.Fatalf("unexpected proxy settings: %+v", config)
	}
	if config.ListenPort != 3333 {
		t.Fatalf("listen_port = %d, want the file's value", config.ListenPort)
	}
	if config.ListenHost != "127.0.0.1" || config.ReadinessHost != "0.0.0.0" || config.ReadinessPort != 3129 {
		t.Fatalf("keys the file omits must keep their defaults: %+v", config)
	}
	if config.AudienceTokenFiles["https://kubernetes.test"] != "/var/run/api/token" {
		t.Fatalf("audience token files = %#v", config.AudienceTokenFiles)
	}
	if route := config.PlatformRoutes["control"]; route.ListenPort != 3130 || route.Upstream != "gateway:80" || route.TokenFile != "/var/run/control/token" {
		t.Fatalf("platform route = %+v", route)
	}
}

func TestLoadConfigRefusesWhatTheSettingsFileGetsWrong(t *testing.T) {
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
		"proxy host missing":    "token_file: /token\n",
		"token file missing":    "proxy_host: p.example\n",
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
