package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	sandboxcontroller "github.com/agentydragon/ducktape/agentplane/sandbox_controller"
)

const validSettings = `namespace: agentplane
metrics_bind_address: ":9090"
health_probe_bind_address: ":9091"
runner_gateway_target: "{sandbox}-control.{namespace}.svc:8080"
runner_gateway_token_file: /var/run/secrets/agentplane-runner-gateway/token
runner_certificate_issuer:
  name: test-workload-issuer
  kind: ClusterIssuer
`

func writeSettings(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "settings.yaml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadConfigReadsYAMLAndIgnoresPerSettingEnvironment(t *testing.T) {
	t.Setenv(configFileEnv, writeSettings(t, validSettings))
	for _, name := range []string{
		"NAMESPACE",
		"METRICS_BIND_ADDRESS",
		"HEALTH_PROBE_BIND_ADDRESS",
		"RUNNER_GATEWAY_TARGET",
		"RUNNER_GATEWAY_TOKEN_FILE",
		"RUNNER_CERTIFICATE_ISSUER",
	} {
		t.Setenv("AGENTPLANE_SANDBOX_CONTROLLER_"+name, "from-environment")
	}

	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	want := config{
		Namespace: "agentplane", MetricsBindAddress: ":9090", HealthProbeBindAddress: ":9091",
		RunnerGatewayTarget:    "{sandbox}-control.{namespace}.svc:8080",
		RunnerGatewayTokenFile: "/var/run/secrets/agentplane-runner-gateway/token",
		RunnerCertificateIssuer: sandboxcontroller.CertificateIssuer{
			Name: "test-workload-issuer", Kind: sandboxcontroller.IssuerKindClusterIssuer,
		},
	}
	if c != want {
		t.Fatalf("unexpected settings: %+v", c)
	}
}

func TestLoadConfigRequiresConfigFilePath(t *testing.T) {
	t.Setenv(configFileEnv, " \t")
	if _, err := loadConfig(); err == nil {
		t.Fatal("started without a settings file path")
	}
}

func TestLoadConfigRejectsMissingFile(t *testing.T) {
	t.Setenv(configFileEnv, filepath.Join(t.TempDir(), "missing.yaml"))
	if _, err := loadConfig(); err == nil {
		t.Fatal("started without the required settings file")
	}
}

func TestBindAddressesDefaultWhenOmittedFromYAML(t *testing.T) {
	settings := strings.Replace(validSettings, "metrics_bind_address: \":9090\"\n", "", 1)
	settings = strings.Replace(settings, "health_probe_bind_address: \":9091\"\n", "", 1)
	t.Setenv(configFileEnv, writeSettings(t, settings))

	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.MetricsBindAddress != ":8080" || c.HealthProbeBindAddress != ":8081" {
		t.Fatalf("bind address defaults were lost: %+v", c)
	}
}

func TestLoadConfigRejectsInvalidSettingsFiles(t *testing.T) {
	missing := map[string]string{
		"namespace":                 "namespace: agentplane\n",
		"runner_gateway_target":     "runner_gateway_target: \"{sandbox}-control.{namespace}.svc:8080\"\n",
		"runner_gateway_token_file": "runner_gateway_token_file: /var/run/secrets/agentplane-runner-gateway/token\n",
		"runner_certificate_issuer": "runner_certificate_issuer:\n  name: test-workload-issuer\n  kind: ClusterIssuer\n",
	}
	cases := map[string]string{
		"unknown key":        validSettings + "unknown_setting: value\n",
		"malformed YAML":     "namespace: [unclosed\n",
		"multiple documents": validSettings + "---\n{}\n",
		"empty metrics bind address": strings.Replace(
			validSettings, "metrics_bind_address: \":9090\"\n", "metrics_bind_address: \"\"\n", 1,
		),
		"empty health probe bind address": strings.Replace(
			validSettings, "health_probe_bind_address: \":9091\"\n", "health_probe_bind_address: \"\"\n", 1,
		),
		"issuer without a name": strings.Replace(validSettings, "  name: test-workload-issuer\n", "", 1),
		"issuer without a kind": strings.Replace(validSettings, "  kind: ClusterIssuer\n", "", 1),
		"issuer of an unknown kind": strings.Replace(
			validSettings, "  kind: ClusterIssuer\n", "  kind: ExternalIssuer\n", 1,
		),
		"issuer as a bare name": strings.Replace(
			validSettings, "runner_certificate_issuer:\n  name: test-workload-issuer\n  kind: ClusterIssuer\n",
			"runner_certificate_issuer: test-workload-issuer\n", 1,
		),
	}
	for name, line := range missing {
		content := strings.Replace(validSettings, line, "", 1)
		if content == validSettings {
			t.Fatalf("test fixture no longer contains %s", name)
		}
		cases["missing "+name] = content
	}

	for name, content := range cases {
		t.Run(name, func(t *testing.T) {
			t.Setenv(configFileEnv, writeSettings(t, content))
			if _, err := loadConfig(); err == nil {
				t.Fatal("started with invalid settings")
			}
		})
	}
}

func TestRunnerGatewayTLSComesFromYAML(t *testing.T) {
	t.Setenv(configFileEnv, writeSettings(t, validSettings+`runner_gateway_tls:
  ca_file: /run/trust/ca.pem
  server_uri: `+environment_runtime_transport.SessionDispatcherURI+"\n"))
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	want := environment_runtime_transport.ClientTLS{
		CAFile: "/run/trust/ca.pem", ServerURI: environment_runtime_transport.SessionDispatcherURI,
	}
	if c.RunnerGatewayTLS == nil || *c.RunnerGatewayTLS != want {
		t.Fatalf("runner Gateway TLS = %+v, want %+v", c.RunnerGatewayTLS, want)
	}
}

func TestRunnerGatewayTLSRejectsIncompleteOrUnknownSettings(t *testing.T) {
	for name, fields := range map[string]string{
		"missing roots":     "  server_uri: " + environment_runtime_transport.SessionDispatcherURI + "\n",
		"missing server":    "  ca_file: /run/trust/ca.pem\n",
		"unknown TLS field": "  insecure_skip_verify: true\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Setenv(configFileEnv, writeSettings(t, validSettings+"runner_gateway_tls:\n"+fields))
			if _, err := loadConfig(); err == nil {
				t.Fatal("accepted invalid runner Gateway TLS settings")
			}
		})
	}
}
