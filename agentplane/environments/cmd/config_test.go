package main

import (
	"fmt"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/testing/configtest"
)

const validSettings = `namespace: agentplane
metrics_bind_address: ":9090"
health_probe_bind_address: ":9091"
runner_gateway_target: "environments.{namespace}.svc:8082"
runner_gateway_token_file: /var/run/secrets/agentplane-runner-gateway/token
runner_security:
  mode: mtls
  tls:
    cert_file: /run/agentplane/sandbox-controller-client-tls/tls.crt
    key_file: /run/agentplane/sandbox-controller-client-tls/tls.key
    ca_file: /run/agentplane/workload-trust/ca.crt
`

func TestLoadConfigReadsYAMLAndIgnoresPerSettingEnvironment(t *testing.T) {
	t.Setenv(configFileEnv, configtest.WriteSettings(t, validSettings))
	for _, name := range []string{
		"NAMESPACE",
		"METRICS_BIND_ADDRESS",
		"HEALTH_PROBE_BIND_ADDRESS",
		"RUNNER_GATEWAY_TARGET",
		"RUNNER_GATEWAY_TOKEN_FILE",
		"RUNNER_SECURITY",
	} {
		t.Setenv("AGENTPLANE_SANDBOX_CONTROLLER_"+name, "from-environment")
	}

	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	want := config{
		Namespace: "agentplane", MetricsBindAddress: ":9090", HealthProbeBindAddress: ":9091",
		RunnerGatewayTarget:    "environments.{namespace}.svc:8082",
		RunnerGatewayTokenFile: "/var/run/secrets/agentplane-runner-gateway/token",
		RunnerSecurity: peertls.Security{Mode: peertls.MutualTLS, TLS: &peertls.Config{
			CertFile: "/run/agentplane/sandbox-controller-client-tls/tls.crt",
			KeyFile:  "/run/agentplane/sandbox-controller-client-tls/tls.key",
			CAFile:   "/run/agentplane/workload-trust/ca.crt",
		}},
	}
	if !reflect.DeepEqual(c, want) {
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
	t.Setenv(configFileEnv, configtest.WriteSettings(t, settings))

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
		"runner_gateway_target":     "runner_gateway_target: \"environments.{namespace}.svc:8082\"\n",
		"runner_gateway_token_file": "runner_gateway_token_file: /var/run/secrets/agentplane-runner-gateway/token\n",
		"runner_security":           "runner_security:\n  mode: mtls\n  tls:\n    cert_file: /run/agentplane/sandbox-controller-client-tls/tls.crt\n    key_file: /run/agentplane/sandbox-controller-client-tls/tls.key\n    ca_file: /run/agentplane/workload-trust/ca.crt\n",
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
		"missing runner security mode": strings.Replace(
			validSettings, "  mode: mtls\n", "", 1,
		),
		"incomplete runner security TLS": strings.Replace(
			validSettings, "    key_file: /run/agentplane/sandbox-controller-client-tls/tls.key\n", "", 1,
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
			t.Setenv(configFileEnv, configtest.WriteSettings(t, content))
			if _, err := loadConfig(); err == nil {
				t.Fatal("started with invalid settings")
			}
		})
	}
}

func TestRunnerGatewayTLSComesFromYAML(t *testing.T) {
	t.Setenv(configFileEnv, configtest.WriteSettings(t, validSettings+`runner_gateway_tls:
  ca_file: /run/trust/ca.pem
  server_uri: `+peertls.SessionDispatcherURI+"\n"))
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	want := peertls.ClientTLS{
		CAFile: "/run/trust/ca.pem", ServerURI: peertls.SessionDispatcherURI,
	}
	if c.RunnerGatewayTLS == nil || *c.RunnerGatewayTLS != want {
		t.Fatalf("runner Gateway TLS = %+v, want %+v", c.RunnerGatewayTLS, want)
	}
}

func TestRunnerGatewayTLSRejectsIncompleteOrUnknownSettings(t *testing.T) {
	for name, fields := range map[string]string{
		"missing roots":     "  server_uri: " + peertls.SessionDispatcherURI + "\n",
		"missing server":    "  ca_file: /run/trust/ca.pem\n",
		"unknown TLS field": "  insecure_skip_verify: true\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Setenv(configFileEnv, configtest.WriteSettings(t, validSettings+"runner_gateway_tls:\n"+fields))
			if _, err := loadConfig(); err == nil {
				t.Fatal("accepted invalid runner Gateway TLS settings")
			}
		})
	}
}

func TestControllerGatewayCallerUsesOnlyItsOwnSVID(t *testing.T) {
	const transport = `runner_gateway_tls:
  ca_file: /run/agentplane/workload-trust/ca.crt
  server_uri: spiffe://agentplane/session-dispatcher
  cert_file: /run/agentplane/sandbox-controller-client-tls/tls.crt
  key_file: /run/agentplane/sandbox-controller-client-tls/tls.key
  client_uri: spiffe://agentplane/sandbox-controller
`
	source := strings.Replace(validSettings, "runner_gateway_token_file: /var/run/secrets/agentplane-runner-gateway/token\n", "", 1) + transport
	t.Setenv(configFileEnv, configtest.WriteSettings(t, source))
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.RunnerGatewayTokenFile != "" || !c.RunnerGatewayTLS.UsesSVID() {
		t.Fatal("lost service SVID caller settings")
	}
	for name, invalid := range map[string]string{
		"mixed":      validSettings + transport,
		"other role": strings.Replace(source, "client_uri: spiffe://agentplane/sandbox-controller", "client_uri: spiffe://agentplane/app", 1),
		"partial":    strings.Replace(source, "  key_file: /run/agentplane/sandbox-controller-client-tls/tls.key\n", "", 1),
	} {
		t.Run(name, func(t *testing.T) {
			t.Setenv(configFileEnv, configtest.WriteSettings(t, invalid))
			if _, err := loadConfig(); err == nil {
				t.Fatal("accepted invalid service SVID caller")
			}
		})
	}
}

const environmentServiceSettings = `environment_service:
  policy_namespace: test-policy
  rpc:
    listen_address: ":8082"
    tls:
      cert_file: /run/test/tls.crt
      key_file: /run/test/tls.key
    auth:
      - mode: spiffe
        ca_file: /run/test/ca.crt
  service_readers: [spiffe://agentplane/app]
  service_managers: [spiffe://agentplane/app]
  service_operators: [spiffe://agentplane/app]
  service_delegation:
    actors:
      - spiffe_id: spiffe://agentplane/actions
    methods: [/agentplane.environments.v1.EnvironmentService/ResumeEnvironment]
`

const environmentRuntimeProxySettings = `  runtime_proxy:
    runner_security:
      mode: insecure
`

func TestEnvironmentServiceIsOptInAndStrict(t *testing.T) {
	t.Setenv(configFileEnv, configtest.WriteSettings(t, validSettings))
	c, err := loadConfig()
	if err != nil || c.EnvironmentService != nil {
		t.Fatalf("existing settings enabled EnvironmentService: %+v, %v", c.EnvironmentService, err)
	}
	t.Setenv(configFileEnv, configtest.WriteSettings(t, validSettings+environmentServiceSettings))
	c, err = loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.EnvironmentService == nil || c.EnvironmentService.RPC.ListenAddress != ":8082" || c.EnvironmentService.RuntimeProxy != nil ||
		len(c.EnvironmentService.ServiceReaders) != 1 || c.EnvironmentService.ServiceReaders[0] != peertls.AppURI {
		t.Fatalf("lost EnvironmentService peer roles: %+v", c.EnvironmentService)
	}
	for name, invalid := range map[string]string{
		"incomplete":            "environment_service: {}\n",
		"unknown nested key":    environmentServiceSettings + "  unknown: true\n",
		"unknown listener key":  strings.Replace(environmentServiceSettings, "    listen_address:", "    misspelled_address:", 1),
		"wrong service role":    strings.Replace(environmentServiceSettings, "[spiffe://agentplane/app]", "[spiffe://agentplane/test-unknown]", 1),
		"foreign method":        strings.Replace(environmentServiceSettings, "/agentplane.environments.v1.EnvironmentService/ResumeEnvironment", "/other.Service/Method", 1),
		"connection provenance": strings.Replace(environmentServiceSettings, "      - spiffe_id: spiffe://agentplane/actions", "      - spiffe_id: spiffe://agentplane/actions\n        connection_provenance: true", 1),
	} {
		t.Run(name, func(t *testing.T) {
			t.Setenv(configFileEnv, configtest.WriteSettings(t, validSettings+invalid))
			if _, err := loadConfig(); err == nil {
				t.Fatal("accepted invalid EnvironmentService settings")
			}
		})
	}
}

func TestEnvironmentRuntimeProxyIsOptionalAndStrict(t *testing.T) {
	t.Setenv(configFileEnv, configtest.WriteSettings(t, validSettings+environmentServiceSettings))
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.EnvironmentService.RuntimeProxy != nil {
		t.Fatalf("omitted runtime_proxy was populated: %+v", c.EnvironmentService.RuntimeProxy)
	}

	t.Setenv(configFileEnv, configtest.WriteSettings(t, validSettings+environmentServiceSettings+environmentRuntimeProxySettings))
	c, err = loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	runtimeProxy := c.EnvironmentService.RuntimeProxy
	if runtimeProxy == nil ||
		runtimeProxy.RunnerSecurity.Mode != peertls.Insecure || len(runtimeProxy.Readers) != 0 ||
		len(runtimeProxy.SessionCallers) != 0 || len(runtimeProxy.ControllerCallers) != 0 || len(runtimeProxy.ExecutorCallers) != 0 {
		t.Fatalf("runtime_proxy defaults or settings were lost: %+v", runtimeProxy)
	}

	invalid := map[string]string{
		"missing runner security": "  runtime_proxy:\n    readers: [spiffe://agentplane/app]\n",
		"invalid security mode":   strings.Replace(environmentRuntimeProxySettings, "      mode: insecure", "      mode: unknown", 1),
		"unknown nested key":      environmentRuntimeProxySettings + "    unknown: true\n",
		"obsolete audience":       environmentRuntimeProxySettings + "    audience: obsolete\n",
	}
	for _, field := range []string{"readers", "session_callers", "controller_callers", "executor_callers"} {
		invalid[field+" service ID"] = environmentRuntimeProxySettings + "    " + field + ": [spiffe://agentplane/unknown]\n"
	}
	for name, runtimeProxySettings := range invalid {
		t.Run(name, func(t *testing.T) {
			t.Setenv(configFileEnv, configtest.WriteSettings(t, validSettings+environmentServiceSettings+runtimeProxySettings))
			if _, err := loadConfig(); err == nil {
				t.Fatal("accepted invalid runtime_proxy settings")
			}
		})
	}
}

func TestPolicyServiceMustBeACompleteMutualTLSCallerAsTheController(t *testing.T) {
	block := func(target, clientURI, serverURI string) string {
		return fmt.Sprintf(`policy_service:
  target: %q
  tls:
    ca_file: /run/test/ca.crt
    server_uri: %s
    cert_file: /run/test/tls.crt
    key_file: /run/test/tls.key
    client_uri: %s
`, target, serverURI, clientURI)
	}
	controller, policy := peertls.SandboxControllerURI, peertls.PolicyControllerURI
	for _, tc := range []struct {
		name     string
		settings string
		valid    bool
	}{
		{"complete", environmentServiceSettings + block("policy-controller:8443", controller, policy), true},
		{"no EnvironmentService to read it", block("policy-controller:8443", controller, policy), false},
		{"no target", environmentServiceSettings + block("", controller, policy), false},
		{"another caller identity", environmentServiceSettings + block("policy-controller:8443", peertls.AppURI, policy), false},
		{"another server", environmentServiceSettings + block("policy-controller:8443", controller, peertls.ActionsURI), false},
		{"no client SVID", environmentServiceSettings + "policy_service:\n  target: policy-controller:8443\n  tls:\n    ca_file: /run/test/ca.crt\n    server_uri: " + policy + "\n", false},
		{"unknown key", environmentServiceSettings + block("policy-controller:8443", controller, policy) + "  unknown: true\n", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(configFileEnv, configtest.WriteSettings(t, validSettings+tc.settings))
			c, err := loadConfig()
			if tc.valid && (err != nil || c.PolicyService == nil || c.PolicyService.Target != "policy-controller:8443") {
				t.Fatalf("loadConfig = %+v, %v", c.PolicyService, err)
			}
			if !tc.valid && err == nil {
				t.Fatal("loadConfig accepted an incomplete policy_service")
			}
		})
	}
}
