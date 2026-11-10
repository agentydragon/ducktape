package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
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

func TestLoadConfigReadsNestedProxySettings(t *testing.T) {
	path := writeSettingsFile(t, `
proxy:
  address: egress.example:9001
  transport: {mode: tls, ca_file: /ca, auth: {mode: spiffe, cert_file: /cert, key_file: /key}}
audience_token_files:
  https://kubernetes.test/Audience: /var/run/api/upper-token
  https://kubernetes.test/audience: /var/run/api/lower-token
platform_routes:
  listen_address: 127.0.0.1:3130
  routes:
    Control.Route:
      service_prefixes: [/agentplane.test.Service/]
      upstream: gateway:80
      transport: {mode: plaintext, token_file: /var/run/control/token}
listen_address: 127.0.0.1:0
`)
	config, err := loadConfig(settingsFileEnv(path))
	if err != nil {
		t.Fatal(err)
	}
	if config.Proxy.Address != "egress.example:9001" {
		t.Fatalf("proxy address = %q", config.Proxy.Address)
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
	route := config.PlatformRoutes.Routes["Control.Route"]
	routeTransport, routeOkay := route.Transport.(plaintextRouteTransportConfig)
	if config.PlatformRoutes.ListenAddress != "127.0.0.1:3130" || route.Upstream != "gateway:80" ||
		!routeOkay || routeTransport.TokenFile != "/var/run/control/token" ||
		len(route.ServicePrefixes) != 1 || route.ServicePrefixes[0] != "/agentplane.test.Service/" {
		t.Fatalf("platform route = %+v", route)
	}
}

func TestLoadConfigAcceptsAnEmptySharedRouteTable(t *testing.T) {
	content := `
proxy: {address: p.example:8888, transport: {mode: tls, ca_file: /ca, auth: {mode: spiffe, cert_file: /cert, key_file: /key}}}
platform_routes: {routes: {}}
`
	config, err := loadConfig(settingsFileEnv(writeSettingsFile(t, content)))
	if err != nil {
		t.Fatal(err)
	}
	if len(config.PlatformRoutes.Routes) != 0 || config.PlatformRoutes.ListenAddress != "" {
		t.Fatalf("empty platform route configuration = %+v", config.PlatformRoutes)
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
  listen_address: 127.0.0.1:3130
  routes:
    egress-rules:
      service_prefixes: [/agentplane.egress.v1.RulesService/]
      upstream: egress-admin.agentplane.svc.cluster.local:8081
      transport: {mode: tls_connect, server_uri: spiffe://agentplane/egress-proxy}
workload_routes:
  'notifications.agentplane.svc.cluster.local:8080':
    upstream: notifications.agentplane.svc.cluster.local:8443
    server_uri: spiffe://agentplane/notifications
`)
	config, err := loadConfig(settingsFileEnv(path))
	if err != nil {
		t.Fatal(err)
	}
	transport := config.Proxy.Transport
	if transport.CAFile != "/run/runner/ca.crt" || transport.Auth.CertFile != "/run/runner/tls.crt" || transport.Auth.KeyFile != "/run/runner/tls.key" {
		t.Fatalf("proxy transport = %#v, want TLS with the CA and SPIFFE files", transport)
	}
	if config.AudienceTokenFiles["agentplane-workload"] != "/var/run/agentplane-egress/token" {
		t.Fatalf("destination audience token file = %#v", config.AudienceTokenFiles)
	}
	route := config.PlatformRoutes.Routes["egress-rules"]
	if config.PlatformRoutes.ListenAddress != "127.0.0.1:3130" ||
		route.Upstream != "egress-admin.agentplane.svc.cluster.local:8081" ||
		len(route.ServicePrefixes) != 1 || route.ServicePrefixes[0] != "/agentplane.egress.v1.RulesService/" {
		t.Fatalf("Rules route = %+v", route)
	}
	routeTransport, okay := route.Transport.(tlsConnectRouteTransportConfig)
	if !okay || routeTransport.ServerURI != environment_runtime_transport.EgressProxyURI {
		t.Fatalf("Rules route transport = %#v, want TLS CONNECT pinned to %q", route.Transport, environment_runtime_transport.EgressProxyURI)
	}
	workloadRoute := config.WorkloadRoutes["notifications.agentplane.svc.cluster.local:8080"]
	if workloadRoute.Upstream != "notifications.agentplane.svc.cluster.local:8443" || workloadRoute.ServerURI != "spiffe://agentplane/notifications" {
		t.Fatalf("notification workload route = %+v", workloadRoute)
	}
}

func TestSidecarRefusesMalformedOrContradictorySettings(t *testing.T) {
	required := `proxy: {address: p.example:8888, transport: {mode: tls, ca_file: /ca, auth: {mode: spiffe, cert_file: /cert, key_file: /key}}}` + "\n"
	validRoute := "platform_routes: {listen_address: 127.0.0.1:3130, routes: {rules: {service_prefixes: [/rules.Service/], upstream: 'rules.test:8081', transport: {mode: plaintext}}}}\n"
	tlsRoute := "platform_routes: {listen_address: 127.0.0.1:3130, routes: {rules: {service_prefixes: [/rules.Service/], upstream: 'rules.test:8081', transport: {mode: tls_connect, server_uri: " + environment_runtime_transport.EgressProxyURI + "}}}}\n"
	for name, content := range map[string]string{
		"unknown key":                              required + "proxy_hots: typo\n",
		"legacy proxy field":                       "proxy_host: p.example\ntoken_file: /token\n",
		"proxy unknown key":                        strings.Replace(required, "transport:", "extra: true, transport:", 1),
		"transport unknown key":                    strings.Replace(required, "auth:", "extra: true, auth:", 1),
		"auth unknown key":                         strings.Replace(required, "key_file: /key", "key_file: /key, extra: true", 1),
		"unknown transport mode":                   strings.Replace(required, "mode: tls", "mode: http", 1),
		"unknown auth mode":                        strings.Replace(required, "mode: spiffe", "mode: token", 1),
		"missing transport mode":                   strings.Replace(required, "mode: tls, ", "", 1),
		"missing auth mode":                        strings.Replace(required, "mode: spiffe, ", "", 1),
		"missing proxy":                            "platform_routes: {routes: {}}\n",
		"missing address":                          strings.Replace(required, "address: p.example:8888, ", "", 1),
		"malformed address":                        strings.Replace(required, "p.example:8888", "p.example/path:8888", 1),
		"invalid port":                             strings.Replace(required, "8888", "70000", 1),
		"missing transport":                        "proxy: {address: p.example:8888}\n",
		"missing auth":                             strings.Replace(required, ", auth: {mode: spiffe, cert_file: /cert, key_file: /key}", "", 1),
		"TLS missing CA":                           strings.Replace(required, "ca_file: /ca, ", "", 1),
		"TLS CA relative":                          strings.Replace(required, "/ca", "ca", 1),
		"SPIFFE missing key":                       strings.Replace(required, ", key_file: /key", "", 1),
		"SPIFFE cert relative":                     strings.Replace(required, "/cert", "cert", 1),
		"top-level auth is unknown":                required + "auth: {mode: spiffe}\n",
		"not an object":                            "- proxy\n",
		"empty":                                    "",
		"null platform routes":                     required + "platform_routes: null\n",
		"routes not an object":                     required + "platform_routes: {routes: [rules]}\n",
		"audience value object":                    required + "audience_token_files: {a: {nested: 1}}\n",
		"removed Rules HTTP setting":               required + "rules_api: rules.test:80\n",
		"legacy per-route listener":                required + "platform_routes: {rules: {listen_address: 127.0.0.1:3130}}\n",
		"route unknown field":                      required + strings.Replace(validRoute, "upstream: 'rules.test:8081'", "upstream: 'rules.test:8081', extra: true", 1),
		"route missing transport":                  required + strings.Replace(validRoute, ", transport: {mode: plaintext}", "", 1),
		"unknown route mode":                       required + strings.Replace(validRoute, "mode: plaintext", "mode: http", 1),
		"unknown route transport key":              required + strings.Replace(validRoute, "mode: plaintext", "mode: plaintext, extra: true", 1),
		"plaintext route token relative":           required + strings.Replace(validRoute, "mode: plaintext", "mode: plaintext, token_file: token", 1),
		"plaintext route rejects server URI":       required + strings.Replace(validRoute, "mode: plaintext", "mode: plaintext, server_uri: spiffe://agentplane/egress-proxy", 1),
		"TLS route rejects token":                  required + strings.Replace(tlsRoute, "server_uri: spiffe://agentplane/egress-proxy", "server_uri: spiffe://agentplane/egress-proxy, token_file: /token", 1),
		"TLS route requires server URI":            required + strings.Replace(tlsRoute, ", server_uri: spiffe://agentplane/egress-proxy", "", 1),
		"TLS route server URI is SPIFFE":           required + strings.Replace(tlsRoute, "spiffe://agentplane/egress-proxy", "https://rules.test", 1),
		"route missing upstream":                   required + strings.Replace(validRoute, "upstream: 'rules.test:8081', ", "", 1),
		"route missing service prefix":             required + strings.Replace(validRoute, "service_prefixes: [/rules.Service/], ", "", 1),
		"route invalid service prefix":             required + strings.Replace(validRoute, "/rules.Service/", "rules.Service", 1),
		"duplicate service prefix":                 required + "platform_routes: {listen_address: 127.0.0.1:3130, routes: {a: {service_prefixes: [/same.Service/], upstream: 'a:1', transport: {mode: plaintext}}, b: {service_prefixes: [/same.Service/], upstream: 'b:2', transport: {mode: plaintext}}}}\n",
		"routes missing listener":                  required + strings.Replace(validRoute, "listen_address: 127.0.0.1:3130, ", "", 1),
		"workload routes not an object":            required + "workload_routes: [rules]\n",
		"workload route requires server URI":       required + "workload_routes: {'rules.test:443': {upstream: 'rules.test:8443'}}\n",
		"workload route server URI is SPIFFE":      required + "workload_routes: {'rules.test:443': {upstream: 'rules.test:8443', server_uri: 'https://rules.test'}}\n",
		"workload route requires valid SPIFFE URI": required + "workload_routes: {'rules.test:443': {upstream: 'rules.test:8443', server_uri: 'spiffe:///rules'}}\n",
	} {
		t.Run(name, func(t *testing.T) {
			config, err := loadConfig(settingsFileEnv(writeSettingsFile(t, content)))
			if err == nil {
				err = config.validate()
			}
			if err == nil {
				t.Fatal("sidecar accepted malformed or contradictory settings")
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
