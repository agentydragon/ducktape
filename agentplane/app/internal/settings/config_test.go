package settings

import (
	"os"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
)

const validSettingsYAML = `
models:
  models:
    - model: model-a
      display_name: Model A
      reasoning_efforts: [low]
  harnesses:
    HARNESS_CLAUDE: [model-a]
    HARNESS_CODEX: []
thread_presets:
  DefaultThread:
    title: Default
    harness: HARNESS_CLAUDE
    model: model-a
sandbox_presets:
  Work Space:
    title: Work
    template: linux
    thread_preset: DefaultThread
  Alpha:
    title: Alpha
    template: linux
    thread_preset: DefaultThread
egress_admin_url: http://egress:8080
connection_service_url: http://connection:8080
action_federation:
  mode: direct
  service_url: https://actions.example.test
  login_jwks_uri: https://login.example.test/jwks
  target:
    issuer: https://actions.example.test
    audience: agentplane
    jwks_uri: https://actions.example.test/jwks
  scope: actions:operator
sandbox_namespace: sandboxes
runner_gateway_target: runner-gateway:9090
session_dispatcher_target: session-dispatcher:9090
policy_service_target: policy-controller:8083
policy_service_tls:
  ca_file: /run/agentplane/workload-trust/ca.pem
  server_uri: spiffe://agentplane/policy-controller
  cert_file: /run/agentplane/app/tls.crt
  key_file: /run/agentplane/app/tls.key
  client_uri: spiffe://agentplane/app
history_service_target: history:9090
history_service_token_file: /run/history/token
notifications_url: http://notifications:9090
notifications_token_file: /run/notifications/token
runner_gateway_token_file: /run/runner/token
egress_admin_token_file: /run/egress/token
` + dispatcherTokenSetting + `listen_address: 0.0.0.0:8080
log_levels: {}
`

// dispatcherTokenSetting is the explicit bearer setting that SVID tests remove.
const dispatcherTokenSetting = "session_dispatcher_token_file: /run/dispatcher/token\n"

func writeConfig(t *testing.T, contents string) string {
	t.Helper()
	path := t.TempDir() + "/app.yaml"
	if err := os.WriteFile(path, []byte(contents), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AGENTPLANE_CONFIG_FILE", path)
	t.Setenv("AGENTPLANE_DATABASE_URL", "postgresql://app@postgres/agentplane")
	return path
}

func validTestConfig() Config {
	return Config{
		Models: runtimeconfig.ModelCatalog{
			Models:    []runtimeconfig.ModelOption{{Model: "model-a", DisplayName: "Model A", ReasoningEfforts: []string{"low", "high"}}},
			Harnesses: map[string][]string{"HARNESS_CLAUDE": {"model-a"}, "HARNESS_CODEX": {"model-a"}},
		},
		ThreadPresets: map[string]ThreadPreset{
			"default": {Title: "Default", Harness: "HARNESS_CODEX", Model: "model-a", ReasoningEffort: stringPtr("low")},
		},
		SandboxPresets: map[string]SandboxPreset{
			"work": {Title: "Work", Template: "linux", ThreadPreset: "default", InboxTags: []string{"team"}},
		},
		SandboxNamespace:                    "sandboxes",
		RunnerGatewayTarget:                 "runner-gateway:9090",
		SessionDispatcherTarget:             "session-dispatcher:9090",
		HistoryServiceTarget:                "history:9090",
		PolicyServiceTarget:                 "policy-controller:8083",
		PolicyServiceTLS:                    &peertls.ClientTLS{CAFile: "/run/trust/ca.pem", ServerURI: peertls.PolicyControllerURI},
		PolicyServiceTokenFile:              "/var/run/policy/token",
		NotificationsURL:                    "http://notifications:9090",
		NotificationsTokenFile:              "/var/run/notifications/token",
		NotificationsRequestTimeoutSeconds:  5,
		RunnerGatewayTokenFile:              "/var/run/runner/token",
		SessionDispatcherTokenFile:          "/var/run/dispatcher/token",
		SessionDispatcherRequestTimeoutSecs: 20,
		HistoryServiceTokenFile:             "/var/run/history/token",
		HistoryServiceRequestTimeoutSeconds: 20,
		DatabaseURL:                         "postgresql://app@postgres/agentplane",
		EgressAdminURL:                      "http://egress:8080",
		ConnectionServiceURL:                "http://connection:8080",
		ActionFederation: ActionFederationConfig{
			Mode: "direct", ServiceURL: "https://actions.example.test", LoginJWKsURI: "https://login.example.test/jwks",
			LoginTokenProfile: "authentik", Target: OperatorOIDCConfig{
				Issuer: "https://actions.example.test", Audience: "agentplane", JWKsURI: "https://actions.example.test/jwks", TokenProfile: "authentik",
			}, Scope: "actions:operator",
		},
		ListenAddress: "127.0.0.1:8080", EgressAdminTokenFile: "/var/run/egress/token", EgressAdminTimeoutSeconds: 5,
	}
}

func stringPtr(value string) *string { return &value }

func TestConfigValidateAcceptsExistingSettingsContract(t *testing.T) {
	if err := validTestConfig().validate(); err != nil {
		t.Fatalf("valid config rejected: %v", err)
	}
}

func TestPolicyServiceTLSRequiresThePolicyControllerIdentity(t *testing.T) {
	config := validTestConfig()
	if err := config.validate(); err != nil {
		t.Fatalf("valid Policy Service TLS rejected: %v", err)
	}
	config.PolicyServiceTLS = nil
	if err := config.validate(); err == nil || !strings.Contains(err.Error(), "policy_service_tls is required") {
		t.Fatalf("missing Policy Service TLS error = %v", err)
	}
	config = validTestConfig()
	config.PolicyServiceTLS.ServerURI = peertls.CentralSamplerURI
	if err := config.validate(); err == nil {
		t.Fatal("accepted TLS for a different service identity")
	}
}

func appSVIDTLS(serverURI string) *peertls.ClientTLS {
	return &peertls.ClientTLS{
		CAFile: "/run/agentplane/workload-trust/ca.pem", ServerURI: serverURI,
		CertFile: "/run/agentplane/app/tls.crt", KeyFile: "/run/agentplane/app/tls.key", ClientURI: peertls.AppURI,
	}
}

func TestAppServiceCallersCanUseSVIDWithoutProjectedTokens(t *testing.T) {
	config := validTestConfig()
	config.SessionDispatcherTLS = appSVIDTLS(peertls.SessionDispatcherURI)
	config.SessionDispatcherTokenFile = ""
	config.RunnerGatewayTLS = appSVIDTLS(peertls.SessionDispatcherURI)
	config.RunnerGatewayTokenFile = ""
	config.HistoryServiceTLS = appSVIDTLS(peertls.HistoryIngressURI)
	config.HistoryServiceTokenFile = ""
	config.PolicyServiceTLS = appSVIDTLS(peertls.PolicyControllerURI)
	config.PolicyServiceTokenFile = ""
	config.EgressAdminTLS = appSVIDTLS(peertls.EgressProxyURI)
	config.EgressAdminTokenFile = ""
	config.NotificationsTLS = appSVIDTLS(peertls.NotificationsURI)
	config.NotificationsTokenFile = ""
	if err := config.validate(); err != nil {
		t.Fatalf("SVID caller config rejected: %v", err)
	}

	config.RunnerGatewayTLS = appSVIDTLS(peertls.SandboxControllerURI)
	if err := config.validate(); err != nil {
		t.Fatalf("Environments Runtime proxy rejected: %v", err)
	}
	config.RunnerGatewayTLS = appSVIDTLS(peertls.ActionsURI)
	if err := config.validate(); err == nil {
		t.Fatal("accepted unrelated Runtime proxy identity")
	}
	config.RunnerGatewayTLS = appSVIDTLS(peertls.SandboxControllerURI)
	config.SessionDispatcherTLS = appSVIDTLS(peertls.SandboxControllerURI)
	if err := config.validate(); err == nil {
		t.Fatal("accepted Environments as Session Dispatcher")
	}
	config.SessionDispatcherTLS = appSVIDTLS(peertls.SessionDispatcherURI)

	config.SessionDispatcherTokenFile = "/run/legacy/token"
	if err := config.validate(); err == nil || !strings.Contains(err.Error(), "session_dispatcher caller authentication") {
		t.Fatalf("SVID plus configured bearer error = %v, want caller-authentication error", err)
	}
}

func TestAppSVIDSettingsRequireExactCallerIdentityAndPeer(t *testing.T) {
	for _, test := range []struct {
		name   string
		change func(*Config)
		want   string
	}{
		{
			name: "wrong client identity",
			change: func(config *Config) {
				config.SessionDispatcherTLS.ClientURI = peertls.NotificationsURI
				config.SessionDispatcherTokenFile = ""
			},
			want: "session_dispatcher caller authentication",
		},
		{
			name: "wrong server identity",
			change: func(config *Config) {
				config.SessionDispatcherTLS.ServerURI = peertls.HistoryIngressURI
			},
			want: "session_dispatcher_tls.server_uri",
		},
		{
			name: "server-only TLS still requires token",
			change: func(config *Config) {
				config.SessionDispatcherTLS = &peertls.ClientTLS{
					CAFile: "/run/agentplane/workload-trust/ca.pem", ServerURI: peertls.SessionDispatcherURI,
				}
				config.SessionDispatcherTokenFile = ""
			},
			want: "session_dispatcher caller authentication",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			config := validTestConfig()
			config.SessionDispatcherTLS = appSVIDTLS(peertls.SessionDispatcherURI)
			config.SessionDispatcherTokenFile = ""
			test.change(&config)
			if err := config.validate(); err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("validate() error = %v, want substring %q", err, test.want)
			}
		})
	}
}

func TestEnvironmentServicePeerDefaultsToTheSessionDispatcher(t *testing.T) {
	config := validTestConfig()
	peer := config.EnvironmentServicePeer()
	if peer.Target != config.SessionDispatcherTarget || peer.TokenFile != config.SessionDispatcherTokenFile ||
		peer.RequestTimeoutSeconds != config.SessionDispatcherRequestTimeoutSecs || peer.TLS != config.SessionDispatcherTLS {
		t.Fatalf("omitted EnvironmentService peer = %+v, want the Session Dispatcher settings", peer)
	}
}

func TestLoadEnvironmentServicePeerUsesControllerTargetAndSVID(t *testing.T) {
	writeConfig(t, validSettingsYAML+`environment_service:
  target: sandbox-controller.agentplane.svc:8082
  request_timeout_s: 12
  tls:
    ca_file: /run/agentplane/workload-trust/ca.pem
    server_uri: spiffe://agentplane/sandbox-controller
    cert_file: /run/agentplane/app/tls.crt
    key_file: /run/agentplane/app/tls.key
    client_uri: spiffe://agentplane/app
`)
	config, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	peer := config.EnvironmentServicePeer()
	if peer.Target != "sandbox-controller.agentplane.svc:8082" || peer.RequestTimeoutSeconds != 12 ||
		peer.TLS == nil || peer.TLS.ServerURI != peertls.SandboxControllerURI || peer.TLS.ClientURI != peertls.AppURI || peer.TokenFile != "" {
		t.Fatalf("EnvironmentService peer = %+v", peer)
	}
	if config.SessionDispatcherTarget != "session-dispatcher:9090" || config.RunnerGatewayTarget != "runner-gateway:9090" {
		t.Fatalf("EnvironmentService peer changed other targets: dispatcher=%q runtime=%q", config.SessionDispatcherTarget, config.RunnerGatewayTarget)
	}
}

func TestLoadConfigRequiresPolicyServiceTargetAndTLS(t *testing.T) {
	t.Run("target", func(t *testing.T) {
		writeConfig(t, strings.Replace(validSettingsYAML, "policy_service_target: policy-controller:8083\n", "", 1))
		if _, err := Load(); err == nil || !strings.Contains(err.Error(), "policy_service_target is required") {
			t.Fatalf("missing policy target error = %v", err)
		}
	})
	t.Run("TLS", func(t *testing.T) {
		settings := strings.Replace(validSettingsYAML, "policy_service_tls:\n  ca_file: /run/agentplane/workload-trust/ca.pem\n  server_uri: spiffe://agentplane/policy-controller\n  cert_file: /run/agentplane/app/tls.crt\n  key_file: /run/agentplane/app/tls.key\n  client_uri: spiffe://agentplane/app\n", "", 1)
		writeConfig(t, settings)
		if _, err := Load(); err == nil || !strings.Contains(err.Error(), "policy_service_tls is required") {
			t.Fatalf("missing policy TLS error = %v", err)
		}
	})
}

func TestEnvironmentServicePeerRejectsPartialOrWrongAuthentication(t *testing.T) {
	for _, test := range []struct {
		name string
		peer *EnvironmentServicePeer
		want string
	}{
		{name: "missing SVID", peer: &EnvironmentServicePeer{Target: "controller:8082", RequestTimeoutSeconds: 10}, want: "environment_service caller authentication"},
		{name: "wrong server URI", peer: &EnvironmentServicePeer{Target: "controller:8082", RequestTimeoutSeconds: 10, TLS: appSVIDTLS(peertls.SessionDispatcherURI)}, want: "environment_service_tls.server_uri"},
		{name: "wrong caller SVID", peer: &EnvironmentServicePeer{Target: "controller:8082", RequestTimeoutSeconds: 10, TLS: &peertls.ClientTLS{CAFile: "/run/ca.pem", ServerURI: peertls.SandboxControllerURI, CertFile: "/run/app.crt", KeyFile: "/run/app.key", ClientURI: peertls.NotificationsURI}}, want: "environment_service caller authentication"},
		{name: "partial SVID", peer: &EnvironmentServicePeer{Target: "controller:8082", RequestTimeoutSeconds: 10, TLS: &peertls.ClientTLS{CAFile: "/run/ca.pem", ServerURI: peertls.SandboxControllerURI, CertFile: "/run/app.crt"}}, want: "environment_service caller authentication"},
	} {
		t.Run(test.name, func(t *testing.T) {
			config := validTestConfig()
			config.EnvironmentService = test.peer
			if err := config.validate(); err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("validate() error = %v, want substring %q", err, test.want)
			}
		})
	}
}

func TestOperatorFacadesKeepOperatorBearerWhenAppSVIDIsConfigured(t *testing.T) {
	config := validTestConfig()
	config.ActionsTLS = appSVIDTLS(peertls.ActionsURI)
	config.ConnectionServiceTLS = appSVIDTLS(peertls.MCPEdgeURI)
	if err := config.validate(); err != nil {
		t.Fatalf("operator facades rejected their transport SVID: %v", err)
	}
	config.ConnectionServiceTLS.ClientURI = peertls.NotificationsURI
	if err := config.validate(); err == nil || !strings.Contains(err.Error(), "connection_service caller authentication") {
		t.Fatalf("wrong Connection Service client identity error = %v", err)
	}
}

func TestLoadConfigUsesOnlyStatedCallerTokens(t *testing.T) {
	writeConfig(t, validSettingsYAML)
	config, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if config.SessionDispatcherTokenFile != "/run/dispatcher/token" ||
		config.HistoryServiceTokenFile != "/run/history/token" ||
		config.NotificationsTokenFile != "/run/notifications/token" ||
		config.RunnerGatewayTokenFile != "/run/runner/token" ||
		config.EgressAdminTokenFile != "/run/egress/token" {
		t.Fatalf("stated caller token files changed: %+v", config)
	}
	if config.PolicyServiceTokenFile != "" {
		t.Fatalf("SVID caller gained a bearer token: %q", config.PolicyServiceTokenFile)
	}

	tlsSettings := `session_dispatcher_tls:
  ca_file: /run/agentplane/workload-trust/ca.pem
  server_uri: spiffe://agentplane/session-dispatcher
  cert_file: /run/agentplane/app/tls.crt
  key_file: /run/agentplane/app/tls.key
  client_uri: spiffe://agentplane/app
`
	svidSettings := strings.Replace(validSettingsYAML, dispatcherTokenSetting, "", 1) + tlsSettings
	writeConfig(t, svidSettings)
	config, err = Load()
	if err != nil {
		t.Fatal(err)
	}
	if config.SessionDispatcherTokenFile != "" {
		t.Fatalf("dispatcher token path = %q, want unset for SVID mode", config.SessionDispatcherTokenFile)
	}

	writeConfig(t, svidSettings+"session_dispatcher_token_file: /run/legacy/token\n")
	if _, err := Load(); err == nil || !strings.Contains(err.Error(), "session_dispatcher caller authentication") {
		t.Fatalf("explicit bearer token with SVID error = %v, want caller-authentication error", err)
	}

	writeConfig(t, strings.Replace(validSettingsYAML, dispatcherTokenSetting, "", 1))
	if _, err := Load(); err == nil || !strings.Contains(err.Error(), "session_dispatcher caller authentication") {
		t.Fatalf("omitted bearer token error = %v, want caller-authentication error", err)
	}
}

func TestLoadConfigRequiresOneStrictYAMLFile(t *testing.T) {
	t.Run("path required", func(t *testing.T) {
		t.Setenv("AGENTPLANE_CONFIG_FILE", "")
		if _, err := Load(); err == nil || !strings.Contains(err.Error(), "AGENTPLANE_CONFIG_FILE is required") {
			t.Fatalf("missing config path error = %v", err)
		}
	})
	t.Run("valid file and runtime secret", func(t *testing.T) {
		writeConfig(t, validSettingsYAML)
		t.Setenv("AGENTPLANE_LISTEN_ADDRESS", "127.0.0.1:9999")
		c, err := Load()
		if err != nil {
			t.Fatal(err)
		}
		if c.ListenAddress != "0.0.0.0:8080" {
			t.Fatalf("static environment override was applied: %q", c.ListenAddress)
		}
		if c.DatabaseURL != "postgresql://app@postgres/agentplane" {
			t.Fatalf("database Secret input = %q", c.DatabaseURL)
		}
		if c.NotificationsRequestTimeoutSeconds != 5 || c.SessionDispatcherRequestTimeoutSecs != 20 ||
			c.HistoryServiceRequestTimeoutSeconds != 20 || c.EgressAdminTimeoutSeconds != 5 || c.ShutdownTimeoutSeconds != 5 {
			t.Fatalf("omitted timeout defaults changed: notifications=%v dispatcher=%v history=%v egress=%v shutdown=%v",
				c.NotificationsRequestTimeoutSeconds, c.SessionDispatcherRequestTimeoutSecs,
				c.HistoryServiceRequestTimeoutSeconds, c.EgressAdminTimeoutSeconds, c.ShutdownTimeoutSeconds)
		}
		if c.RunnerGatewayRequestTimeoutSeconds != 0 || c.RunnerGatewayLifecycleTimeoutSecs != 0 {
			t.Fatalf("unused Runner Gateway timeouts took defaults: request=%v lifecycle=%v",
				c.RunnerGatewayRequestTimeoutSeconds, c.RunnerGatewayLifecycleTimeoutSecs)
		}
		if names := c.SandboxPresetNames(); len(names) != 2 || names[0] != "Work Space" || names[1] != "Alpha" || c.ThreadPresets["DefaultThread"].CWD != "/state/workspaces/{session_id}" {
			t.Fatalf("unexpected Sandbox preset order %v or Thread preset defaults %+v", names, c.ThreadPresets)
		}
		if c.ActionFederation.LoginTokenProfile != "authentik" || c.ActionFederation.Target.TokenProfile != "authentik" {
			t.Fatalf("unexpected federation profile defaults: %+v", c.ActionFederation)
		}
	})
	for _, test := range []struct {
		name string
		yaml string
		want string
	}{
		{name: "unknown key", yaml: validSettingsYAML + "unknown_setting: true\n", want: "unknown_setting"},
		{name: "retired subjects", yaml: validSettingsYAML + "token_subjects: []\n", want: "token_subjects"},
		{name: "retired audience", yaml: validSettingsYAML + "token_audience: agentplane\n", want: "token_audience"},
		{name: "database secret in file", yaml: validSettingsYAML + "database_url: postgresql://app@postgres/agentplane\n", want: "database_url"},
		{name: "second document", yaml: validSettingsYAML + "---\nmodels: {}\n", want: "exactly one YAML document"},
		{name: "malformed YAML", yaml: validSettingsYAML + "thread_presets: [\n", want: "could not decode app settings file"},
	} {
		t.Run(test.name, func(t *testing.T) {
			writeConfig(t, test.yaml)
			if _, err := Load(); err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("load error = %v, want %q", err, test.want)
			}
		})
	}
}

func TestLoadConfigReadsOIDCStaticSettingsFromFileAndSecretInputs(t *testing.T) {
	configPath := writeConfig(t, validSettingsYAML+`oidc:
  issuer: https://auth.example.test/application/o/agentplane/
  public_base_url: https://app.example.test
`)
	t.Setenv("AGENTPLANE_OIDC_CLIENT_ID", "client")
	t.Setenv("AGENTPLANE_OIDC_CLIENT_SECRET", "client-secret")
	t.Setenv("AGENTPLANE_OIDC_SESSION_SECRET", "session-secret")
	// Former static values are ignored; the file is authoritative.
	t.Setenv("AGENTPLANE_OIDC_ISSUER", "https://ignored.example.test")
	t.Setenv("AGENTPLANE_OIDC_TOKEN_RENEW_BEFORE_SECONDS", "0")
	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.OIDC == nil || c.OIDC.Issuer != "https://auth.example.test/application/o/agentplane/" ||
		c.OIDC.SessionMaxSeconds != 604800 || c.OIDC.SessionIdleSeconds != 86400 ||
		c.OIDC.SessionActivityStepSeconds != 300 || c.OIDC.TokenRenewBeforeSeconds != 30 {
		t.Fatalf("unexpected OIDC settings: %+v", c.OIDC)
	}
	if err := os.WriteFile(configPath, []byte(validSettingsYAML+`oidc:
  issuer: https://auth.example.test/application/o/agentplane/
  public_base_url: https://app.example.test
  token_renew_before_seconds: 0
`), 0600); err != nil {
		t.Fatal(err)
	}
	c, err = Load()
	if err != nil {
		t.Fatal(err)
	}
	if c.OIDC == nil || c.OIDC.TokenRenewBeforeSeconds != 0 {
		t.Fatalf("explicit zero token renew setting was not retained: %+v", c.OIDC)
	}
}

func TestLoadConfigRetainsCaseSensitivePresetNames(t *testing.T) {
	writeConfig(t, validSettingsYAML)
	c, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := c.SandboxPresets["Work Space"]; !ok {
		t.Fatalf("sandbox preset name lost case or spacing: %#v", c.SandboxPresets)
	}
	if _, ok := c.SandboxPresets["work space"]; ok {
		t.Fatalf("sandbox preset name was case-folded: %#v", c.SandboxPresets)
	}
}

func TestValidateCatalogRequiresBothHarnessesAndAValidPreset(t *testing.T) {
	tests := []struct {
		name   string
		change func(*Config)
		want   string
	}{
		{
			name:   "missing harness key",
			change: func(c *Config) { delete(c.Models.Harnesses, "HARNESS_CLAUDE") },
			want:   "both native harnesses",
		},
		{
			name: "no offered model",
			change: func(c *Config) {
				c.Models.Harnesses["HARNESS_CLAUDE"] = nil
				c.Models.Harnesses["HARNESS_CODEX"] = nil
			},
			want: "offer at least one model",
		},
		{
			name: "unknown harness",
			change: func(c *Config) {
				c.Models.Harnesses["HARNESS_UNKNOWN"] = []string{"model-a"}
			},
			want: "unknown harness",
		},
		{
			name:   "unsupported reasoning effort",
			change: func(c *Config) { *c.ThreadPresets["default"].ReasoningEffort = "extra-high" },
			want:   "not supported",
		},
		{
			name: "missing sandbox thread preset",
			change: func(c *Config) {
				preset := c.SandboxPresets["work"]
				preset.ThreadPreset = "missing"
				c.SandboxPresets["work"] = preset
			},
			want: "unknown ThreadPreset",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			c := validTestConfig()
			test.change(&c)
			err := c.validate()
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("validate() error = %v, want substring %q", err, test.want)
			}
		})
	}
}

func TestValidateRejectsUnsafeEndpointForms(t *testing.T) {
	for _, endpoint := range []string{
		"https://user:pass@example.test",
		"https://example.test/path?token=x",
		"https://example.test/path?",
		"https://example.test/path#fragment",
		"https://example.test/path#",
		"https://example.test:invalid/path",
		"https://example.test:65536/path",
	} {
		t.Run(endpoint, func(t *testing.T) {
			if err := validateHTTPURL("endpoint", endpoint); err == nil {
				t.Fatalf("validateHTTPURL(%q) unexpectedly passed", endpoint)
			}
		})
	}
	if err := validateHTTPSOrLoopbackURL("token_endpoint", "http://actions.example.test/token"); err == nil {
		t.Fatal("non-loopback HTTP token endpoint unexpectedly passed")
	}
	if err := validateHTTPSOrLoopbackURL("token_endpoint", "http://127.0.0.1:8080/token"); err != nil {
		t.Fatalf("loopback HTTP token endpoint rejected: %v", err)
	}
}

func TestFrontendLinksRejectUnsafeEndpoints(t *testing.T) {
	for _, endpoint := range []string{
		"http://grafana.example.test/", "https://user:pass@grafana.example.test/",
		"https://grafana.example.test/?token=secret", "https://grafana.example.test/?",
		"https://grafana.example.test/#fragment", "https://grafana.example.test/#",
		"https://grafana.example.test:65536/", "relative",
	} {
		t.Run(endpoint, func(t *testing.T) {
			c := validTestConfig()
			c.FrontendConfig.TraceSearch = &TraceSearchSettings{
				GrafanaBaseURL: endpoint, TempoDatasourceUID: "tempo", DeploymentEnvironment: "fixture",
			}
			if err := c.validate(); err == nil {
				t.Fatalf("accepted unsafe frontend endpoint: %s", endpoint)
			}
		})
	}
}
