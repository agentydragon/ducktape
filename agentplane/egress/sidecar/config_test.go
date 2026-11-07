package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
)

func writeSettingsFile(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "settings.yaml")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
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

func TestLoadConfigReadsNestedBearerProxySettings(t *testing.T) {
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
    transport:
      mode: plaintext
      token_file: /var/run/control/token
listen_address: 127.0.0.1:0
`)
	config, err := loadConfig(settingsFileEnv(path))
	if err != nil {
		t.Fatal(err)
	}
	if config.Proxy.Address != "egress.example:9001" {
		t.Fatalf("proxy address = %q", config.Proxy.Address)
	}
	transport, okay := config.Proxy.Transport.(plaintextProxyTransportConfig)
	if !okay || transport.Auth.mode() != listener.KubernetesBearer || transport.Auth.TokenFile != "/var/run/egress/token" {
		t.Fatalf("proxy transport = %#v, want bearer plaintext", config.Proxy.Transport)
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
	route := config.PlatformRoutes["Control.Route"]
	routeTransport, routeTransportOkay := route.Transport.(plaintextRouteTransportConfig)
	if route.ListenAddress != "127.0.0.1:3130" || route.Upstream != "gateway:80" || !routeTransportOkay || routeTransport.TokenFile != "/var/run/control/token" {
		t.Fatalf("platform route = %+v", route)
	}
}

func TestLoadConfigReadsSpiffeAuthAndTransport(t *testing.T) {
	path := writeSettingsFile(t, `
proxy:
  address: egress.example:8888
  transport:
    mode: tls
    ca_file: /run/runner/ca.crt
    auth:
      mode: spiffe
      cert_file: /run/runner/tls.crt
      key_file: /run/runner/tls.key
audience_token_files:
  agentplane-workload: /var/run/agentplane-egress/token
  https://kubernetes.test: /var/run/egress/kubernetes-token
platform_routes:
  egress-rules:
    listen_address: 127.0.0.1:3133
    upstream: egress-admin.agentplane.svc.cluster.local:8081
    transport: {mode: tls_connect, server_uri: 'spiffe://agentplane/egress-proxy'}
`)
	config, err := loadConfig(settingsFileEnv(path))
	if err != nil {
		t.Fatal(err)
	}
	if config.Proxy.Address != "egress.example:8888" {
		t.Fatalf("proxy address = %q", config.Proxy.Address)
	}
	transport, okay := config.Proxy.Transport.(tlsProxyTransportConfig)
	if !okay || transport.CAFile != "/run/runner/ca.crt" {
		t.Fatalf("proxy transport = %#v, want TLS with CA", config.Proxy.Transport)
	}
	auth, okay := transport.Auth.(spiffeProxyAuth)
	if !okay || auth.mode() != listener.SPIFFE || auth.CertFile != "/run/runner/tls.crt" || auth.KeyFile != "/run/runner/tls.key" {
		t.Fatalf("proxy auth = %#v, want SPIFFE files", transport.Auth)
	}
	if config.AudienceTokenFiles["agentplane-workload"] != "/var/run/agentplane-egress/token" {
		t.Fatalf("destination audience token file = %#v", config.AudienceTokenFiles)
	}
	route := config.PlatformRoutes["egress-rules"]
	if route.ListenAddress != "127.0.0.1:3133" || route.Upstream != "egress-admin.agentplane.svc.cluster.local:8081" {
		t.Fatalf("Rules route = %+v", route)
	}
	if _, okay := route.Transport.(tlsConnectRouteTransportConfig); !okay {
		t.Fatalf("Rules route transport = %T, want TLS CONNECT", route.Transport)
	}
}

func TestLoadConfigAllowsBearerWithServerAuthenticatedTLS(t *testing.T) {
	path := writeSettingsFile(t, `
proxy:
  address: egress.example:8888
  transport:
    mode: tls
    ca_file: /run/cluster/ca.crt
    auth: {mode: kubernetes_bearer, token_file: /token}
platform_routes: {}
`)
	config, err := loadConfig(settingsFileEnv(path))
	if err != nil {
		t.Fatal(err)
	}
	transport := config.Proxy.Transport.(tlsProxyTransportConfig)
	if transport.CAFile != "/run/cluster/ca.crt" {
		t.Fatalf("proxy transport CA file = %q", transport.CAFile)
	}
	if _, okay := transport.Auth.(kubernetesBearerProxyAuth); !okay {
		t.Fatalf("proxy auth = %T, want bearer without a client certificate", transport.Auth)
	}
}

func TestLoadConfigRefusesMalformedOrContradictorySettings(t *testing.T) {
	const required = `proxy: {address: p.example:8888, transport: {mode: plaintext, auth: {mode: kubernetes_bearer, token_file: /token}}}` + "\n"
	for name, content := range map[string]string{
		"unknown key":              required + "proxy_hots: typo\n",
		"unknown nested proxy key": strings.Replace(required, "transport:", "extra: true, transport:", 1),
		"duplicate proxy field":    strings.Replace(required, "address: p.example:8888", "address: p.example:8888, address: p.example:8889", 1),
		"unknown auth key":         strings.Replace(required, "token_file: /token", "token_file: /token, extra: true", 1),
		"duplicate auth mode":      strings.Replace(required, "mode: kubernetes_bearer", "mode: kubernetes_bearer, mode: spiffe", 1),
		"missing proxy":            "platform_routes: {}\n",
		"missing address":          "proxy: {transport: {mode: plaintext, auth: {mode: kubernetes_bearer, token_file: /token}}}\n",
		"malformed address":        strings.Replace(required, "p.example:8888", "p.example/path:8888", 1),
		"invalid port":             strings.Replace(required, "8888", "70000", 1),
		"missing transport":        "proxy: {address: p.example:8888}\n",
		"missing transport mode":   strings.Replace(required, "mode: plaintext, ", "", 1),
		"missing auth mode":        strings.Replace(required, "mode: kubernetes_bearer, ", "", 1),
		"unknown auth mode":        strings.Replace(required, "kubernetes_bearer", "token", 1),
		"bearer token missing":     strings.Replace(required, ", token_file: /token", "", 1),
		"bearer token relative":    strings.Replace(required, "/token", "token", 1),
		"plaintext cannot take SVID": strings.Replace(required,
			"auth: {mode: kubernetes_bearer, token_file: /token}",
			"auth: {mode: spiffe, cert_file: /c, key_file: /k}", 1),
		"unknown transport":   strings.Replace(required, "plaintext", "insecure", 1),
		"plaintext extra key": strings.Replace(required, "mode: plaintext", "mode: plaintext, ca_file: /ca", 1),
		"plaintext empty CA":  strings.Replace(required, "mode: plaintext", "mode: plaintext, ca_file: ''", 1),
		"TLS missing CA":      strings.Replace(required, "mode: plaintext", "mode: tls", 1),
		"TLS relative CA":     strings.Replace(required, "mode: plaintext", "mode: tls, ca_file: ca.pem", 1),
		"SPIFFE missing key": strings.Replace(required,
			"mode: plaintext, auth: {mode: kubernetes_bearer, token_file: /token}",
			"mode: tls, ca_file: /ca, auth: {mode: spiffe, cert_file: /c}", 1),
		"SPIFFE extra token": strings.Replace(required,
			"auth: {mode: kubernetes_bearer, token_file: /token}",
			"auth: {mode: spiffe, cert_file: /c, key_file: /k, token_file: /token}", 1),
		"SPIFFE empty token": strings.Replace(required,
			"auth: {mode: kubernetes_bearer, token_file: /token}",
			"auth: {mode: spiffe, cert_file: /c, key_file: /k, token_file: ''}", 1),
		"workload routes not an object":    required + "workload_routes: [control]\n",
		"plaintext route rejects identity": required + "platform_routes: {rules: {listen_address: '127.0.0.1:3133', upstream: 'rules:8081', transport: {mode: plaintext, server_uri: 'spiffe://agentplane/egress-proxy'}}}\n",
		"TLS route requires identity":      required + "platform_routes: {rules: {listen_address: '127.0.0.1:3133', upstream: 'rules:8081', transport: {mode: tls_connect}}}\n",
		"top-level auth is unknown":        required + "auth: {mode: spiffe}\n",
		"not an object":                    "- proxy\n",
		"empty":                            "",
		"explicit null routes":             required + "platform_routes: null\n",
		"routes not an object":             required + "platform_routes: [control]\n",
		"audience value object":            required + "audience_token_files: {a: {nested: 1}}\n",
		"removed Rules HTTP setting":       required + "rules_api: rules.test:80\n",
		"route missing transport":          required + "platform_routes: {rules: {listen_address: 127.0.0.1:3133, upstream: 'rules:8081'}}\n",
		"unknown route transport":          required + "platform_routes: {rules: {listen_address: 127.0.0.1:3133, upstream: 'rules:8081', transport: {mode: http}}}\n",
		"unknown route transport key":      required + "platform_routes: {rules: {listen_address: 127.0.0.1:3133, upstream: 'rules:8081', transport: {mode: plaintext, extra: true}}}\n",
		"plaintext route token relative":   required + "platform_routes: {rules: {listen_address: 127.0.0.1:3133, upstream: 'rules:8081', transport: {mode: plaintext, token_file: token}}}\n",
		"TLS route rejects token":          required + "platform_routes: {rules: {listen_address: 127.0.0.1:3133, upstream: 'rules:8081', transport: {mode: tls_connect, token_file: /token}}}\n",
		"TLS route requires mTLS proxy":    required + "platform_routes: {rules: {listen_address: 127.0.0.1:3133, upstream: 'rules:8081', transport: {mode: tls_connect, server_uri: 'spiffe://agentplane/egress-proxy'}}}\n",
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := loadConfig(settingsFileEnv(writeSettingsFile(t, content))); err == nil {
				t.Fatal("loadConfig succeeded")
			}
		})
	}
}

func TestLoadConfigRejectsTLSConnectRouteWithBearerTLSProxy(t *testing.T) {
	contents := `
proxy:
  address: egress.example:8888
  transport:
    mode: tls
    ca_file: /run/cluster/ca.crt
    auth: {mode: kubernetes_bearer, token_file: /token}
platform_routes:
  egress-rules:
    listen_address: 127.0.0.1:3133
    upstream: egress-admin.agentplane.svc.cluster.local:8081
    transport: {mode: tls_connect, server_uri: 'spiffe://agentplane/egress-proxy'}
`
	if _, err := loadConfig(settingsFileEnv(writeSettingsFile(t, contents))); err == nil {
		t.Fatal("TLS CONNECT route accepted a TLS proxy configured only with bearer authentication")
	}
}

func TestLoadConfigRefusesAMissingSettingsFile(t *testing.T) {
	absent := filepath.Join(t.TempDir(), "absent.yaml")
	if _, err := loadConfig(settingsFileEnv(absent)); err == nil {
		t.Fatal("a settings file that is not there must be an error, not defaults")
	}
}
