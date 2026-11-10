package main

import (
	"os"
	"reflect"
	"strings"
	"testing"

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
runner_gateway_control_target: '{sandbox}-control.{namespace}.svc:8080'
session_dispatcher_target: session-dispatcher:9090
history_service_target: history:9090
notifications_url: http://notifications:9090
listen_address: 0.0.0.0:8080
token_subjects: [system:serviceaccount:sandboxes:agentplane-agent]
log_levels: {}
`

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
		RunnerGatewayControlTarget:          "{sandbox}-control.{namespace}.svc:8080",
		HistoryServiceTarget:                "history:9090",
		PolicyServiceTarget:                 "policy-controller:8082",
		PolicyServiceTokenFile:              "/var/run/policy/token",
		NotificationsURL:                    "http://notifications:9090",
		NotificationsTokenFile:              "/var/run/notifications/token",
		NotificationsRequestTimeoutSeconds:  5,
		RunnerGatewayTokenFile:              "/var/run/runner/token",
		SessionDispatcherTokenFile:          "/var/run/dispatcher/token",
		SessionDispatcherRequestTimeoutSecs: 20,
		HistoryServiceTokenFile:             "/var/run/history/token",
		HistoryServiceRequestTimeoutSeconds: 20,
		RunnerGatewayRequestTimeoutSeconds:  20,
		RunnerGatewayLifecycleTimeoutSecs:   310,
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

func TestLoadConfigRequiresOneStrictYAMLFile(t *testing.T) {
	t.Run("path required", func(t *testing.T) {
		t.Setenv("AGENTPLANE_CONFIG_FILE", "")
		if _, err := loadConfig(); err == nil || !strings.Contains(err.Error(), "AGENTPLANE_CONFIG_FILE is required") {
			t.Fatalf("missing config path error = %v", err)
		}
	})
	t.Run("valid file and runtime secret", func(t *testing.T) {
		writeConfig(t, validSettingsYAML)
		t.Setenv("AGENTPLANE_LISTEN_ADDRESS", "127.0.0.1:9999")
		t.Setenv("AGENTPLANE_TOKEN_SUBJECTS", `[]`)
		c, err := loadConfig()
		if err != nil {
			t.Fatal(err)
		}
		if c.ListenAddress != "0.0.0.0:8080" {
			t.Fatalf("static environment override was applied: %q", c.ListenAddress)
		}
		if !reflect.DeepEqual(c.TokenSubjects, []string{"system:serviceaccount:sandboxes:agentplane-agent"}) {
			t.Fatalf("token subjects were not loaded from YAML: %#v", c.TokenSubjects)
		}
		if c.DatabaseURL != "postgresql://app@postgres/agentplane" {
			t.Fatalf("database Secret input = %q", c.DatabaseURL)
		}
		if c.NotificationsRequestTimeoutSeconds != 5 || c.RunnerGatewayLifecycleTimeoutSecs != 310 {
			t.Fatalf("omitted defaults changed: notifications=%v lifecycle=%v", c.NotificationsRequestTimeoutSeconds, c.RunnerGatewayLifecycleTimeoutSecs)
		}
		views := c.PresetViews()
		if len(views) != 2 || views[0].Name != "Work Space" || views[1].Name != "Alpha" || views[0].SessionDefaults.CWD != "/state/workspaces/{session_id}" {
			t.Fatalf("unexpected browser preset views: %+v", views)
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
		{name: "database secret in file", yaml: validSettingsYAML + "database_url: postgresql://app@postgres/agentplane\n", want: "database_url"},
		{name: "second document", yaml: validSettingsYAML + "---\nmodels: {}\n", want: "exactly one YAML document"},
		{name: "malformed YAML", yaml: validSettingsYAML + "thread_presets: [\n", want: "could not decode app settings file"},
	} {
		t.Run(test.name, func(t *testing.T) {
			writeConfig(t, test.yaml)
			if _, err := loadConfig(); err == nil || !strings.Contains(err.Error(), test.want) {
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
	c, err := loadConfig()
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
	c, err = loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.OIDC == nil || c.OIDC.TokenRenewBeforeSeconds != 0 {
		t.Fatalf("explicit zero token renew setting was not retained: %+v", c.OIDC)
	}
}

func TestLoadConfigRetainsCaseSensitivePresetNames(t *testing.T) {
	writeConfig(t, validSettingsYAML)
	c, err := loadConfig()
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
