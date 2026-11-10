package main

import (
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/testing/configtest"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
)

func TestLoadConfigPrecedence(t *testing.T) {
	path := configtest.WriteSettings(t, `allowed_service_account_namespaces: [yaml-namespace]
token_audience: yaml-audience
litellm_url: http://127.0.0.1:8100/prefix
litellm_key: yaml-secret
log_llm_requests: true
rpc: {listen_address: "127.0.0.1:6060", tls: {cert_file: /tls/cert, key_file: /tls/key}, auth: [{mode: kubernetes_bearer, audiences: [yaml-audience, env-audience]}]}
codex_model_catalog_file: /yaml/codex_models.json
models:
  - model: test/model
    total_context_budget_tokens: 8192
`)
	c, err := configtest.Load(t, settingPrefix, loadConfig, []string{
		configFileEnv + "=" + path,
		"AGENTPLANE_LLM_INGRESS_TOKEN_AUDIENCE=env-audience",
		"AGENTPLANE_LLM_INGRESS_LITELLM_KEY=env-secret",
		"AGENTPLANE_LLM_INGRESS_LOG_LLM_REQUESTS=false",
	})
	if err != nil {
		t.Fatal(err)
	}
	if c.TokenAudience != "env-audience" || c.RPC.ListenAddress != "127.0.0.1:6060" || c.LiteLLMKey != "env-secret" || c.LogLLMRequests ||
		len(c.AllowedServiceAccountNamespaces) != 1 || c.AllowedServiceAccountNamespaces[0] != "yaml-namespace" ||
		c.LiteLLMURL != "http://127.0.0.1:8100/prefix" || len(c.Models) != 1 || c.Models[0].TotalContextBudgetTokens != 8192 ||
		c.CodexModelCatalogFile != "/yaml/codex_models.json" {
		t.Fatalf("unexpected merged config: %+v", c)
	}
}

func TestAttributionConfig(t *testing.T) {
	base := `allowed_service_account_namespaces: [test-namespace]
token_audience: test-audience
rpc: {listen_address: ":8080", tls: {cert_file: /tls/cert, key_file: /tls/key}, auth: [{mode: kubernetes_bearer, audiences: [test-audience]}]}
litellm_url: http://127.0.0.1:8100
litellm_key: test-secret
`
	for _, test := range []struct {
		name  string
		yaml  string
		fails bool
	}{
		{name: "default"},
		{name: "baggage only", yaml: "attribution:\n  baggage:\n    - key: Custom.PodUid\n      source: pod_uid\n"},
		{name: "mixed-case custom key", yaml: "attribution:\n  json_headers:\n    - name: X-Custom-Verified\n      fields:\n        - key: Agentplane.PodName\n          source: pod_name\n"},
		{name: "empty", yaml: "attribution: {}\n", fails: true},
		{name: "empty JSON fields", yaml: "attribution:\n  json_headers:\n    - name: x-custom-verified\n      fields: []\n", fails: true},
		{name: "duplicate key", yaml: "attribution:\n  baggage:\n    - key: same\n      source: pod_uid\n    - key: same\n      source: namespace\n", fails: true},
		{name: "unknown source", yaml: "attribution:\n  baggage:\n    - key: claim\n      source: body_metadata\n", fails: true},
		{name: "credential header", yaml: "attribution:\n  json_headers:\n    - name: X-Api-Key\n      fields:\n        - key: a\n          source: pod_name\n", fails: true},
		{name: "routing header", yaml: "attribution:\n  json_headers:\n    - name: X-Forwarded-For\n      fields:\n        - key: a\n          source: pod_name\n", fails: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			path := configtest.WriteSettings(t, base+test.yaml)
			c, err := configtest.Load(t, settingPrefix, loadConfig, []string{configFileEnv + "=" + path})
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
				if len(c.Attribution.JSONHeaders) != 0 || len(c.Attribution.Baggage) != 1 || c.Attribution.Baggage[0].Key != "Custom.PodUid" {
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

func TestPolicyNamespaceRequiresEveryAttributionOutputToNameTheIdentity(t *testing.T) {
	base := `allowed_service_account_namespaces: [test-namespace]
policy_namespace: test-policy
token_audience: test-audience
rpc: {listen_address: ":8080", tls: {cert_file: /tls/cert, key_file: /tls/key}, auth: [{mode: kubernetes_bearer, audiences: [test-audience]}]}
litellm_url: http://127.0.0.1:8100
litellm_key: test-secret
`
	for _, test := range []struct {
		name  string
		yaml  string
		fails bool
	}{
		{name: "default"},
		{name: "every output names the identity", yaml: "attribution:\n  json_headers:\n    - name: x-custom\n      fields:\n        - key: a\n          source: identity\n  baggage:\n    - key: b\n      source: identity\n"},
		{name: "baggage without identity", yaml: "attribution:\n  json_headers:\n    - name: x-custom\n      fields:\n        - key: a\n          source: identity\n  baggage:\n    - key: b\n      source: namespace\n", fails: true},
		{name: "header with identity UID alone", yaml: "attribution:\n  json_headers:\n    - name: x-custom\n      fields:\n        - key: a\n          source: identity_uid\n", fails: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			path := configtest.WriteSettings(t, base+test.yaml)
			c, err := configtest.Load(t, settingPrefix, loadConfig, []string{configFileEnv + "=" + path})
			if test.fails != (err != nil) {
				t.Fatalf("load = %+v, %v; want failure %v", c.Attribution, err, test.fails)
			}
			if err == nil && c.PolicyNamespace != "test-policy" {
				t.Fatalf("policy namespace = %q", c.PolicyNamespace)
			}
		})
	}
}

func TestLoadConfigRejectsInvalidModelBudgets(t *testing.T) {
	for _, value := range []string{`"not-a-budget"`, "null", "0", "-1"} {
		t.Run(value, func(t *testing.T) {
			path := configtest.WriteSettings(t, "allowed_service_account_namespaces: [test-namespace]\n"+
				"token_audience: test-audience\n"+`rpc: {listen_address: ":8080", tls: {cert_file: /tls/cert, key_file: /tls/key}, auth: [{mode: kubernetes_bearer, audiences: [test-audience]}]}`+"\n"+
				"litellm_url: http://127.0.0.1:8100\n"+
				"litellm_key: never-include-this-secret\n"+
				"models:\n  - model: test/model\n    total_context_budget_tokens: "+value+"\n")
			_, err := configtest.Load(t, settingPrefix, loadConfig, []string{configFileEnv + "=" + path})
			if err == nil || strings.Contains(err.Error(), "never-include-this-secret") {
				t.Fatalf("got unsafe or missing config error: %v", err)
			}
		})
	}
}

func TestLoadConfigRejectsUnknownFileSettingsWithoutEchoingValues(t *testing.T) {
	base := `allowed_service_account_namespaces: [test-namespace]
token_audience: test-audience
rpc: {listen_address: ":8080", tls: {cert_file: /tls/cert, key_file: /tls/key}, auth: [{mode: kubernetes_bearer, audiences: [test-audience]}]}
litellm_url: http://127.0.0.1:8100
litellm_key: never-include-this-secret
models: []
`
	path := configtest.WriteSettings(t, base+`unknown_setting: never-include-this-secret
`)
	_, err := configtest.Load(t, settingPrefix, loadConfig, []string{configFileEnv + "=" + path})
	if err == nil || strings.Contains(err.Error(), "never-include-this-secret") {
		t.Fatalf("got unsafe or missing config error: %v", err)
	}
}

func TestSharedListenerRequiresTLS(t *testing.T) {
	c := config{
		AllowedServiceAccountNamespaces: []string{"test-namespace"},
		TokenAudience:                   testAudience,
		LiteLLMURL:                      "http://127.0.0.1:8100",
		LiteLLMKey:                      testBackendKey,
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
