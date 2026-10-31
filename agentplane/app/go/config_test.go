package main

import (
	"os"
	"reflect"
	"strings"
	"testing"
)

func validTestConfig() Config {
	return Config{
		Models: ModelCatalog{
			Models: []ModelOption{{Model: "model-a", DisplayName: "Model A", ReasoningEfforts: []string{"low", "high"}}},
			Harnesses: map[string][]string{
				"HARNESS_CLAUDE": {"model-a"},
				"HARNESS_CODEX":  {"model-a"},
			},
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

func TestListenAddressConfig(t *testing.T) {
	for _, address := range []string{"127.0.0.1:8080", "[::1]:8080", "fd:7"} {
		config := validTestConfig()
		if _, _, err := applyCommandLineOverrides(&config, []string{"--listen-address=" + address}); err != nil {
			t.Fatal(err)
		}
		if config.ListenAddress != address {
			t.Fatalf("listen address = %q, want %q", config.ListenAddress, address)
		}
		if err := config.validate(); err != nil {
			t.Fatal(err)
		}
	}
	config := validTestConfig()
	config.ListenAddress = ""
	if err := config.validate(); err == nil || !strings.Contains(err.Error(), "listen_address") {
		t.Fatalf("empty listener validation = %v, want rejection", err)
	}
}

func TestApplyCommandLineOverridesRejectsRetiredSessionDispatcherHTTPURLFlag(t *testing.T) {
	config := validTestConfig()
	if _, _, err := applyCommandLineOverrides(&config, []string{"--session-dispatcher-http-url=http://dispatcher:8080"}); err == nil ||
		!strings.Contains(err.Error(), "session-dispatcher-http-url") {
		t.Fatalf("retired Session Dispatcher HTTP URL flag error = %v", err)
	}
}

func TestConfigValidateRejectsTooShortNewSandboxIdleSuspendDefault(t *testing.T) {
	seconds := int64(59)
	config := validTestConfig()
	config.NewSandboxDefaults.IdleSuspendAfterSeconds = &seconds
	if err := config.validate(); err == nil || !strings.Contains(err.Error(), "new_sandbox_defaults.idle_suspend_after_seconds") {
		t.Fatalf("invalid new Sandbox idle suspend default error = %v", err)
	}
}

func TestLoadConfigAppliesPresetDefaultsAndRequiredSettings(t *testing.T) {
	path := t.TempDir() + "/app.yaml"
	contents := `
models:
  models:
    - model: model-a
      display_name: Model A
      reasoning_efforts: []
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
default_policy_sets: []
new_sandbox_defaults:
  wake_on_send: true
  idle_suspend_after_seconds: 900
frontend_config:
  trace_search:
    grafana_base_url: https://grafana.example.test/
    tempo_datasource_uid: fixture-tempo
    deployment_environment: fixture
  langfuse_search:
    base_url: https://langfuse.example.test/
    project_id: fixture-project
    sandbox_namespace_metadata_key: sandbox_namespace
    sandbox_service_account_metadata_key: sandbox_service_account
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
runner_gateway_control_target: '{sandbox}-control.{namespace}.svc:8080'
history_service_target: history:9090
notifications_url: http://notifications:9090
database_url: postgresql://app@postgres/agentplane
`
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AGENTPLANE_CONFIG_FILE", path)
	t.Setenv("AGENTPLANE_TOKEN_SUBJECTS", `["system:serviceaccount:sandboxes:agentplane-agent"]`)
	c, err := loadConfigWithArgs(nil)
	if err != nil {
		t.Fatal(err)
	}
	thread := c.ThreadPresets["DefaultThread"]
	if thread.CWD != "/state/workspaces/{session_id}" || thread.Instructions != "" || thread.SetupScript != "" {
		t.Fatalf("unexpected ThreadPreset defaults: %+v", thread)
	}
	sandbox := c.SandboxPresets["Work Space"]
	if sandbox.PolicySets == nil || sandbox.InboxTags == nil {
		t.Fatalf("omitted SandboxPreset lists must be empty arrays: %+v", sandbox)
	}
	views := c.PresetViews()
	if len(views) != 2 || views[0].Name != "Work Space" || views[1].Name != "Alpha" || views[0].SessionDefaults.CWD != "/state/workspaces/{session_id}" || views[0].Bootstrap != "" {
		t.Fatalf("unexpected browser preset views: %+v", views)
	}
	if c.Models.Harnesses["HARNESS_CODEX"] == nil || c.Models.Models[0].ReasoningEfforts == nil {
		t.Fatalf("empty model catalog lists must be empty arrays: %+v", c.Models)
	}
	if c.ActionFederation.LoginTokenProfile != "authentik" || c.ActionFederation.Target.TokenProfile != "authentik" {
		t.Fatalf("unexpected federation profile defaults: %+v", c.ActionFederation)
	}
	if !c.NewSandboxDefaults.WakeOnSend || c.NewSandboxDefaults.IdleSuspendAfterSeconds == nil || *c.NewSandboxDefaults.IdleSuspendAfterSeconds != 900 {
		t.Fatalf("unexpected new Sandbox defaults: %+v", c.NewSandboxDefaults)
	}
	if c.RunnerGatewayControlTarget != "{sandbox}-control.{namespace}.svc:8080" {
		t.Fatalf("runner gateway control target was not loaded from YAML: %q", c.RunnerGatewayControlTarget)
	}
	if c.LogLevels == nil || len(c.LogLevels) != 0 {
		t.Fatalf("omitted log_levels must default to an empty map, got %#v", c.LogLevels)
	}
	if c.FrontendConfig.TraceSearch == nil || c.FrontendConfig.TraceSearch.TempoDatasourceUID != "fixture-tempo" ||
		c.FrontendConfig.LangfuseSearch == nil || c.FrontendConfig.LangfuseSearch.ProjectID != "fixture-project" {
		t.Fatalf("frontend config was not loaded from YAML: %+v", c.FrontendConfig)
	}
	if key := c.FrontendConfig.LangfuseSearch.SessionMetadataKey; key != "attributes.litellm.metadata.spend_logs_metadata.agentplane.session_id" {
		t.Fatalf("default Langfuse Session filter key = %q", key)
	}
	t.Setenv("AGENTPLANE_FRONTEND_CONFIG", `{"trace_search":{"grafana_base_url":"https://grafana.example.test/","tempo_datasource_uid":"env-tempo","deployment_environment":"env"}}`)
	c, err = loadConfigWithArgs(nil)
	if err != nil {
		t.Fatal(err)
	}
	if c.FrontendConfig.TraceSearch == nil || c.FrontendConfig.TraceSearch.TempoDatasourceUID != "env-tempo" || c.FrontendConfig.LangfuseSearch != nil {
		t.Fatalf("frontend config environment override = %+v", c.FrontendConfig)
	}
	c, err = loadConfigWithArgs([]string{`--frontend-config={"langfuse_search":{"base_url":"https://langfuse.example.test/","project_id":"cli-project","session_metadata_key":"session","sandbox_namespace_metadata_key":"namespace","sandbox_service_account_metadata_key":"account"}}`})
	if err != nil {
		t.Fatal(err)
	}
	if c.FrontendConfig.TraceSearch != nil || c.FrontendConfig.LangfuseSearch == nil || c.FrontendConfig.LangfuseSearch.ProjectID != "cli-project" || c.FrontendConfig.LangfuseSearch.SessionMetadataKey != "session" {
		t.Fatalf("frontend config CLI override = %+v", c.FrontendConfig)
	}
	for _, invalid := range []string{
		`{"trace_search":{}}`,
		`{"langfuse_search":{"base_url":"https://langfuse.example.test/","project_id":"partial"}}`,
		`{"langfuse_search":{"base_url":"https://langfuse.example.test/","project_id":"project","session_metadata_key":"","sandbox_namespace_metadata_key":"namespace","sandbox_service_account_metadata_key":"account"}}`,
		`{"unknown":"refused"}`,
	} {
		if _, err := loadConfigWithArgs([]string{"--frontend-config=" + invalid}); err == nil {
			t.Errorf("accepted invalid frontend config: %s", invalid)
		}
	}
	if !reflect.DeepEqual(c.TokenSubjects, []string{"system:serviceaccount:sandboxes:agentplane-agent"}) {
		t.Fatalf("token subjects environment override = %#v", c.TokenSubjects)
	}
	c, err = loadConfigWithArgs([]string{`--models={"models":[{"model":"model-a","display_name":"Model A","reasoning_efforts":[]}],"harnesses":{"HARNESS_CLAUDE":["model-a"],"HARNESS_CODEX":[],"HARNESS_CENTRAL_SAMPLER":["model-a"]}}`})
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Models.Harnesses["HARNESS_CENTRAL_SAMPLER"]) != 1 {
		t.Fatalf("central sampler catalog = %+v", c.Models)
	}
}

func TestRuntimeCLIOverridesEnvironmentAndYAML(t *testing.T) {
	path := t.TempDir() + "/app.yaml"
	contents := `
models:
  models:
    - model: model-a
      display_name: Model A
      reasoning_efforts: []
  harnesses:
    HARNESS_CLAUDE: [model-a]
    HARNESS_CODEX: []
thread_presets: {}
sandbox_presets: {}
default_policy_sets: []
egress_admin_url: http://egress:8080
connection_service_url: http://connection:8080/
action_federation:
  mode: direct
  service_url: https://actions.example.test
  login_jwks_uri: https://login.example.test/jwks
  target:
    issuer: https://actions.example.test
    audience: agentplane
    jwks_uri: https://actions.example.test/jwks
  scope: actions:operator
sandbox_namespace: yaml-sandboxes
runner_gateway_target: yaml-runner:9090
runner_gateway_control_target: 'yaml-{sandbox}-control.{namespace}.svc:8080'
runner_gateway_token_file: /yaml/runner-token
history_service_target: yaml-history:9090
session_dispatcher_target: yaml-dispatcher:9090
notifications_url: http://yaml-notifications:9090
notifications_token_file: /yaml/notifications-token
listen_address: 127.0.0.1:8080
database_url: postgresql://app@postgres/agentplane
log_levels: {}
`
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("AGENTPLANE_CONFIG_FILE", path)
	t.Setenv("AGENTPLANE_RUNNER_GATEWAY_TOKEN_FILE", "/env/runner-token")
	t.Setenv("AGENTPLANE_SESSION_DISPATCHER_TARGET", "env-dispatcher:9090")
	t.Setenv("AGENTPLANE_NOTIFICATIONS_TOKEN_FILE", "/env/notifications-token")

	c, err := loadConfigWithArgs([]string{
		"--sandbox-namespace=cli-sandboxes",
		`--models={"models":[{"model":"cli-model","display_name":"CLI Model","reasoning_efforts":["high"]}],"harnesses":{"HARNESS_CLAUDE":["cli-model"],"HARNESS_CODEX":["cli-model"]}}`,
		`--thread-presets={"cli-thread":{"title":"CLI Thread","harness":"HARNESS_CLAUDE","model":"cli-model"}}`,
		`--sandbox-presets={"CLI Sandbox":{"title":"CLI Sandbox","template":"linux","thread_preset":"cli-thread"}}`,
		`--default-policy-sets=["cli-a","cli-b"]`,
		`--token-subjects=["system:serviceaccount:app:worker"]`,
		"--runner-gateway-target", "cli-runner:9090",
		"--runner-gateway-token-file=/cli/runner-token",
		"--history-service-target=cli-history:9090",
		"--session-dispatcher-target=cli-dispatcher:9090",
		"--notifications-url=http://cli-notifications:9090",
		"--notifications-token-file=/cli/notifications-token",
		"--listen-address=0.0.0.0:9091",
		"--shutdown-timeout=7",
	})
	if err != nil {
		t.Fatalf("load config with runtime CLI overrides: %v", err)
	}
	if c.SandboxNamespace != "cli-sandboxes" || c.RunnerGatewayTarget != "cli-runner:9090" || c.HistoryServiceTarget != "cli-history:9090" {
		t.Fatalf("CLI runtime values were not applied: %+v", c)
	}
	if c.RunnerGatewayControlTarget != "yaml-{sandbox}-control.{namespace}.svc:8080" {
		t.Fatalf("Runner Gateway control target was not retained from YAML: %q", c.RunnerGatewayControlTarget)
	}
	if len(c.Models.Models) != 1 || c.Models.Models[0].Model != "cli-model" ||
		!reflect.DeepEqual(c.DefaultPolicySets, []string{"cli-a", "cli-b"}) ||
		!reflect.DeepEqual(c.TokenSubjects, []string{"system:serviceaccount:app:worker"}) {
		t.Fatalf("structured CLI settings were not applied: %+v", c)
	}
	if thread := c.ThreadPresets["cli-thread"]; thread.CWD != "/state/workspaces/{session_id}" || thread.Instructions != "" || thread.SetupScript != "" {
		t.Fatalf("CLI ThreadPreset defaults were not applied: %+v", thread)
	}
	if views := c.PresetViews(); len(views) != 1 || views[0].Name != "CLI Sandbox" || views[0].SessionDefaults.Harness != "HARNESS_CLAUDE" {
		t.Fatalf("CLI SandboxPreset order/defaults were not applied: %+v", views)
	}
	if c.SessionDispatcherTarget != "cli-dispatcher:9090" {
		t.Fatalf("CLI Session Dispatcher values were not applied: %+v", c)
	}
	if c.NotificationsURL != "http://cli-notifications:9090" || c.NotificationsTokenFile != "/cli/notifications-token" {
		t.Fatalf("CLI Notification values were not applied: %+v", c)
	}
	if c.RunnerGatewayTokenFile != "/cli/runner-token" {
		t.Fatalf("CLI should override environment and YAML: %q", c.RunnerGatewayTokenFile)
	}
	if c.ListenAddress != "0.0.0.0:9091" {
		t.Fatalf("CLI listen address was not applied: %q", c.ListenAddress)
	}
	if c.ShutdownTimeoutSeconds != 7 {
		t.Fatalf("CLI shutdown timeout was not applied: %d", c.ShutdownTimeoutSeconds)
	}

	envWins, err := loadConfigWithArgs([]string{"--listen-address=0.0.0.0:8080"})
	if err != nil {
		t.Fatalf("load config with environment override: %v", err)
	}
	if envWins.RunnerGatewayTokenFile != "/env/runner-token" || envWins.NotificationsTokenFile != "/env/notifications-token" {
		t.Fatalf("environment should override YAML when CLI omits a value: runner=%q notifications=%q", envWins.RunnerGatewayTokenFile, envWins.NotificationsTokenFile)
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
