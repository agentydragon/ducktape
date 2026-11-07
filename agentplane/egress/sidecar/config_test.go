package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	workloadlistener "github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
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
proxy:
  address: egress.example:9001
  transport:
    mode: plaintext
    auth:
      mode: kubernetes_bearer
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
	if config.Proxy.Address != "egress.example:9001" ||
		config.Proxy.Transport.Mode != proxyTransportPlaintext ||
		config.Proxy.Transport.Auth.Mode != workloadlistener.KubernetesBearer ||
		config.Proxy.Transport.Auth.TokenFile != "/var/run/egress/token" {
		t.Fatalf("unexpected proxy settings: %+v", config)
	}
	if config.ListenAddress != "127.0.0.1:0" {
		t.Fatalf("listen_address = %q, want the file's value", config.ListenAddress)
	}
	if config.ReadinessListenAddress != "0.0.0.0:3129" {
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
	const required = "" +
		"proxy:\n" +
		"  address: p.example:8888\n" +
		"  transport:\n" +
		"    mode: plaintext\n" +
		"    auth:\n" +
		"      mode: kubernetes_bearer\n" +
		"      token_file: /token\n"
	for name, content := range map[string]string{
		"unknown key":            required + "proxy_hots: typo\n",
		"legacy proxy field":     "proxy_host: p.example\ntoken_file: /token\n",
		"proxy unknown key":      required + "  extra: true\n",
		"transport unknown key":  strings.Replace(required, "    auth:", "    ca_file: /ca\n    auth:", 1),
		"auth unknown key":       strings.Replace(required, "      token_file:", "      cert_file: /cert\n      token_file:", 1),
		"unsupported transport":  strings.Replace(required, "mode: plaintext", "mode: tls", 1),
		"unsupported auth":       strings.Replace(required, "mode: kubernetes_bearer", "mode: spiffe", 1),
		"missing transport mode": strings.Replace(required, "    mode: plaintext\n", "", 1),
		"missing auth mode":      strings.Replace(required, "      mode: kubernetes_bearer\n", "", 1),
		"route unknown key":      required + "platform_routes: {control: {listen_address: 127.0.0.1:3130, upstream: u, token_file: /t, extra: true}}\n",
		"route missing address":  required + "platform_routes: {control: {upstream: u, token_file: /t}}\n",
		"route empty address":    required + "platform_routes: {control: {listen_address: '', upstream: u, token_file: /t}}\n",
		"port not an integer":    strings.Replace(required, "p.example:8888", "p.example:eight", 1),
		"not an object":          "- proxy_host\n",
		"empty":                  "",
		"explicit null routes":   required + "platform_routes: null\n",
		"routes not an object":   required + "platform_routes: [control]\n",
		"audience value object":  required + "audience_token_files: {a: {nested: 1}}\n",
		"proxy address missing":  strings.Replace(required, "  address: p.example:8888\n", "", 1),
		"token file missing":     strings.Replace(required, "      token_file: /token\n", "", 1),
		"relative token file":    strings.Replace(required, "      token_file: /token\n", "      token_file: token\n", 1),
		"invalid proxy port":     strings.Replace(required, "p.example:8888", "p.example:65536", 1),
		"invalid bracketed host": strings.Replace(required, "p.example:8888", "[p.example]:8888", 1),
		"unbracketed ipv6":       strings.Replace(required, "p.example:8888", "2001:db8::1:8888", 1),
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
