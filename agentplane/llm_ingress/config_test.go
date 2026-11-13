package main

import (
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/testing/configtest"
)

const testLiteLLMKey = "test-provider-secret"

const validSettings = `allowed_service_account_namespaces: [yaml-namespace]
policy_namespace: test-policy
token_audience: yaml-audience
litellm_url: http://127.0.0.1:8100/prefix
log_llm_requests: true
rpc: {listen_address: "127.0.0.1:6060", tls: {cert_file: /tls/cert, key_file: /tls/key}, auth: [{mode: kubernetes_bearer, audiences: [yaml-audience]}]}
codex_model_catalog_file: /yaml/codex_models.json
kubeconfig: /yaml/kubeconfig
central_sampler_accounts: [yaml-namespace/sampler]
log_levels: {root: INFO}
models:
  - model: test/model
    total_context_budget_tokens: 8192
`

func loadSettings(t *testing.T, contents string, extra ...string) (config, error) {
	t.Helper()
	path := configtest.WriteSettings(t, contents)
	environ := append([]string{configFileEnv + "=" + path, liteLLMKeyEnv + "=" + testLiteLLMKey}, extra...)
	return configtest.Load(t, "AGENTPLANE_LLM_INGRESS_", loadConfig, environ)
}

func TestStaticSettingsComeOnlyFromFileAndProviderKeyIsExplicit(t *testing.T) {
	c, err := loadSettings(
		t, validSettings,
		"AGENTPLANE_LLM_INGRESS_TOKEN_AUDIENCE=env-audience",
		"AGENTPLANE_LLM_INGRESS_LITELLM_URL=http://env.invalid",
		"AGENTPLANE_LLM_INGRESS_LOG_LLM_REQUESTS=false",
		"AGENTPLANE_LLM_INGRESS_KUBECONFIG=/env/kubeconfig",
	)
	if err != nil {
		t.Fatal(err)
	}
	if c.TokenAudience != "yaml-audience" || c.RPC.ListenAddress != "127.0.0.1:6060" || c.LiteLLMKey != testLiteLLMKey || !c.LogLLMRequests ||
		len(c.AllowedServiceAccountNamespaces) != 1 || c.AllowedServiceAccountNamespaces[0] != "yaml-namespace" ||
		c.LiteLLMURL != "http://127.0.0.1:8100/prefix" || len(c.Models) != 1 || c.Models[0].TotalContextBudgetTokens != 8192 ||
		c.CodexModelCatalogFile != "/yaml/codex_models.json" || c.Kubeconfig != "/yaml/kubeconfig" ||
		len(c.CentralSamplerAccounts) != 1 || c.CentralSamplerAccounts[0] != "yaml-namespace/sampler" || c.LogLevels["root"] != "INFO" {
		t.Fatalf("unexpected file settings: %+v", c)
	}
}

func TestSettingsFileIsRequiredReadableAndSingleDocument(t *testing.T) {
	for name, environ := range map[string][]string{
		"missing":    {liteLLMKeyEnv + "=" + testLiteLLMKey},
		"blank":      {configFileEnv + "= ", liteLLMKeyEnv + "=" + testLiteLLMKey},
		"unreadable": {configFileEnv + "=/missing/llm-ingress.yaml", liteLLMKeyEnv + "=" + testLiteLLMKey},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := configtest.Load(t, "AGENTPLANE_LLM_INGRESS_", loadConfig, environ); err == nil {
				t.Fatal("loaded without a readable settings file")
			}
		})
	}
	if _, err := loadSettings(t, validSettings+"---\npolicy_namespace: second\n"); err == nil {
		t.Fatal("accepted multiple YAML documents")
	}
	path := configtest.WriteSettings(t, validSettings)
	if _, err := configtest.Load(t, "AGENTPLANE_LLM_INGRESS_", loadConfig, []string{configFileEnv + "=" + path}); err == nil {
		t.Fatal("started without the explicit provider Secret input")
	}
}

func TestProviderKeyIsNotDecodedFromTheSettingsFile(t *testing.T) {
	settings := strings.Replace(validSettings, "log_llm_requests: true\n", "litellm_key: file-secret\nlog_llm_requests: true\n", 1)
	if _, err := loadSettings(t, settings); err == nil {
		t.Fatal("accepted the provider credential from the settings file")
	}
}

func TestPolicyNamespaceIsRequired(t *testing.T) {
	for name, contents := range map[string]string{
		"missing": strings.Replace(validSettings, "policy_namespace: test-policy\n", "", 1),
		"empty":   strings.Replace(validSettings, "policy_namespace: test-policy", `policy_namespace: ""`, 1),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := loadSettings(t, contents); err == nil {
				t.Fatal("accepted a missing policy_namespace")
			}
		})
	}
}

func TestAttributionConfig(t *testing.T) {
	for _, test := range []struct {
		name  string
		yaml  string
		fails bool
	}{
		{name: "default"},
		{name: "baggage only", yaml: "attribution:\n  json_headers: []\n  baggage:\n    - key: Custom.PodUid\n      source: pod_uid\n    - key: Custom.Identity\n      source: identity\n"},
		{name: "mixed-case custom key", yaml: "attribution:\n  json_headers:\n    - name: X-Custom-Verified\n      fields:\n        - key: Agentplane.PodName\n          source: pod_name\n        - key: Agentplane.Identity\n          source: identity\n"},
		{name: "empty", yaml: "attribution: {json_headers: [], baggage: []}\n", fails: true},
		{name: "empty JSON fields", yaml: "attribution:\n  json_headers:\n    - name: x-custom-verified\n      fields: []\n", fails: true},
		{name: "duplicate key", yaml: "attribution:\n  json_headers: []\n  baggage:\n    - key: same\n      source: pod_uid\n    - key: same\n      source: namespace\n", fails: true},
		{name: "unknown source", yaml: "attribution:\n  json_headers: []\n  baggage:\n    - key: claim\n      source: body_metadata\n", fails: true},
		{name: "credential header", yaml: "attribution:\n  baggage: []\n  json_headers:\n    - name: X-Api-Key\n      fields:\n        - key: a\n          source: pod_name\n", fails: true},
		{name: "routing header", yaml: "attribution:\n  baggage: []\n  json_headers:\n    - name: X-Forwarded-For\n      fields:\n        - key: a\n          source: pod_name\n", fails: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			settings := strings.Replace(validSettings, "log_llm_requests: true\n", test.yaml+"log_llm_requests: true\n", 1)
			c, err := loadSettings(t, settings)
			if test.fails {
				if err == nil {
					t.Fatalf("invalid attribution accepted: %+v", c.Attribution)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			switch test.name {
			case "default":
				if len(c.Attribution.JSONHeaders) != 1 || c.Attribution.JSONHeaders[0].Name != "x-litellm-spend-logs-metadata" || len(c.Attribution.Baggage) != 0 {
					t.Fatalf("default attribution = %+v", c.Attribution)
				}
			case "baggage only":
				if len(c.Attribution.JSONHeaders) != 0 || len(c.Attribution.Baggage) != 2 || c.Attribution.Baggage[0].Key != "Custom.PodUid" {
					t.Fatalf("baggage-only attribution = %+v", c.Attribution)
				}
			case "mixed-case custom key":
				if len(c.Attribution.JSONHeaders) != 1 || c.Attribution.JSONHeaders[0].Name != "X-Custom-Verified" ||
					c.Attribution.JSONHeaders[0].Fields[0].Key != "Agentplane.PodName" {
					t.Fatalf("custom attribution lost spelling: %+v", c.Attribution)
				}
			}
		})
	}
}

func TestLoadConfigRejectsInvalidModelBudgetsAndUnknownKeys(t *testing.T) {
	for _, value := range []string{`"not-a-budget"`, "null", "0", "-1"} {
		t.Run(value, func(t *testing.T) {
			settings := strings.Replace(validSettings, "total_context_budget_tokens: 8192", "total_context_budget_tokens: "+value, 1)
			if _, err := loadSettings(t, settings); err == nil || strings.Contains(err.Error(), testLiteLLMKey) {
				t.Fatalf("got unsafe or missing config error: %v", err)
			}
		})
	}
	for name, settings := range map[string]string{
		"top level":       validSettings + "unknown_setting: secret-value\n",
		"nested listener": strings.Replace(validSettings, "auth: [{mode: kubernetes_bearer, audiences: [yaml-audience]}]", "auth: [{mode: kubernetes_bearer, audiences: [yaml-audience], ignored: secret-value}]", 1),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := loadSettings(t, settings); err == nil || strings.Contains(err.Error(), "secret-value") {
				t.Fatalf("unknown setting accepted or echoed its value: %v", err)
			}
		})
	}
}

func TestSharedListenerRequiresTLS(t *testing.T) {
	c := config{
		AllowedServiceAccountNamespaces: []string{"test-namespace"},
		PolicyNamespace:                 "test-policy",
		TokenAudience:                   testAudience,
		LiteLLMURL:                      "http://127.0.0.1:8100",
		LiteLLMKey:                      testLiteLLMKey,
		Attribution:                     defaultAttribution(),
		RPC:                             listener.Config{ListenAddress: ":8080", Auth: []listener.Authentication{&listener.KubernetesBearerAuth{Audiences: []string{testAudience}}}},
	}
	if err := validateConfig(&c); err == nil {
		t.Fatal("shared HTTP/2 listener accepted plaintext")
	}
	c.RPC.TLS = &listener.TLS{CertFile: "/tls/cert", KeyFile: "/tls/key"}
	if err := validateConfig(&c); err != nil {
		t.Fatal(err)
	}
}
