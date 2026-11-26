package server

import (
	"fmt"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
	"github.com/agentydragon/ducktape/agentplane/testing/configtest"
)

const testLiteLLMKey = "test-provider-secret"

const settingsWithoutPolicyService = `allowed_service_account_namespaces: [yaml-namespace]
litellm_url: http://127.0.0.1:8100/prefix
log_llm_requests: true
rpc: {listen_address: "127.0.0.1:6060", tls: {cert_file: /tls/cert, key_file: /tls/key}, auth: [{mode: spiffe, ca_file: /tls/ca}]}
codex_model_catalog_file: /yaml/codex_models.json
kubeconfig: /yaml/kubeconfig
log_levels: {root: INFO}
models:
  - model: test/model
    total_context_budget_tokens: 8192
`

func policyServiceSettings(target, clientURI, serverURI string) string {
	return fmt.Sprintf(`policy_service:
  target: %q
  tls:
    ca_file: /tls/ca
    server_uri: %s
    cert_file: /tls/cert
    key_file: /tls/key
    client_uri: %s
`, target, serverURI, clientURI)
}

var validSettings = settingsWithoutPolicyService + policyServiceSettings("policy-controller:8443", peertls.LLMIngressURI, peertls.PolicyControllerURI)

func loadSettings(t *testing.T, contents string, extra ...string) (config, error) {
	t.Helper()
	path := configtest.WriteSettings(t, contents)
	environ := append([]string{configFileEnv + "=" + path, liteLLMKeyEnv + "=" + testLiteLLMKey}, extra...)
	return configtest.Load(t, "AGENTPLANE_LLM_INGRESS_", loadConfig, environ)
}

func TestStaticSettingsComeOnlyFromFileAndProviderKeyIsExplicit(t *testing.T) {
	c, err := loadSettings(
		t, validSettings,
		"AGENTPLANE_LLM_INGRESS_LITELLM_URL=http://env.invalid",
		"AGENTPLANE_LLM_INGRESS_LOG_LLM_REQUESTS=false",
		"AGENTPLANE_LLM_INGRESS_KUBECONFIG=/env/kubeconfig",
	)
	if err != nil {
		t.Fatal(err)
	}
	if c.RPC.ListenAddress != "127.0.0.1:6060" || c.LiteLLMKey != testLiteLLMKey || !c.LogLLMRequests ||
		len(c.AllowedServiceAccountNamespaces) != 1 || c.AllowedServiceAccountNamespaces[0] != "yaml-namespace" ||
		c.LiteLLMURL != "http://127.0.0.1:8100/prefix" || len(c.Models) != 1 || c.Models[0].TotalContextBudgetTokens != 8192 ||
		c.CodexModelCatalogFile != "/yaml/codex_models.json" || c.Kubeconfig != "/yaml/kubeconfig" ||
		c.LogLevels["root"] != "INFO" {
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
		"nested listener": strings.Replace(validSettings, "auth: [{mode: spiffe, ca_file: /tls/ca}]", "auth: [{mode: spiffe, ca_file: /tls/ca, ignored: secret-value}]", 1),
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
		PolicyService:                   identitycache.Peer{Target: "policy-controller:8443", TLS: peertls.ClientTLS{CAFile: "/tls/ca", ServerURI: peertls.PolicyControllerURI, CertFile: "/tls/cert", KeyFile: "/tls/key", ClientURI: peertls.LLMIngressURI}},
		LiteLLMURL:                      "http://127.0.0.1:8100",
		LiteLLMKey:                      testLiteLLMKey,
		Attribution:                     defaultAttribution(),
		RPC:                             listener.Config{ListenAddress: ":8080", Auth: []*listener.SPIFFEAuth{{CAFile: "/tls/ca"}}},
	}
	if err := validateConfig(&c); err == nil {
		t.Fatal("shared HTTP/2 listener accepted plaintext")
	}
	c.RPC.TLS = &listener.TLS{CertFile: "/tls/cert", KeyFile: "/tls/key"}
	if err := validateConfig(&c); err != nil {
		t.Fatal(err)
	}
}

func TestSamplerIdentityDelegationRequiresServiceActors(t *testing.T) {
	if _, err := loadSettings(t, validSettings+"central_sampler_identity_callers: true\n"); err == nil {
		t.Fatal("sampler Identity delegation accepted without a service actor")
	}
}

func TestPolicyServiceMustBeACompleteMutualTLSCallerAsTheIngress(t *testing.T) {
	block := policyServiceSettings
	ingress, policy := peertls.LLMIngressURI, peertls.PolicyControllerURI
	for _, tc := range []struct {
		name     string
		settings string
		valid    bool
	}{
		{"complete", block("policy-controller:8443", ingress, policy), true},
		{"no target", block("", ingress, policy), false},
		{"another caller identity", block("policy-controller:8443", peertls.MCPEdgeURI, policy), false},
		{"another server", block("policy-controller:8443", ingress, peertls.ActionsURI), false},
		{"missing", "", false},
		{"no client SVID", "policy_service:\n  target: policy-controller:8443\n  tls:\n    ca_file: /tls/ca\n    server_uri: " + policy + "\n", false},
		{"unknown key", block("policy-controller:8443", ingress, policy) + "  surprise: true\n", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c, err := loadSettings(t, settingsWithoutPolicyService+tc.settings)
			if tc.valid && (err != nil || c.PolicyService.Target != "policy-controller:8443") {
				t.Fatalf("loadConfig = %+v, %v", c.PolicyService, err)
			}
			if !tc.valid && err == nil {
				t.Fatal("loadConfig accepted an incomplete policy_service")
			}
		})
	}
}
