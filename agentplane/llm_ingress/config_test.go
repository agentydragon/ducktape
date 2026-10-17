package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeSettings(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "settings.yaml")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadConfigPrecedence(t *testing.T) {
	path := writeSettings(t, `allowed_service_account_namespaces: [yaml-namespace]
token_audience: yaml-audience
litellm_url: http://127.0.0.1:8100/prefix
litellm_key: yaml-secret
models:
  - model: test/model
    total_context_budget_tokens: 8192
`)
	c, err := loadConfig(
		[]string{
			"--token-audience=flag-audience", "--port=9090", "--log-llm-requests", "false",
			`--allowed-service-account-namespaces=["flag-namespace"]`,
		},
		[]string{
			configFileEnv + "=" + path,
			"AGENTPLANE_LLM_INGRESS_TOKEN_AUDIENCE=env-audience",
			"AGENTPLANE_LLM_INGRESS_LITELLM_KEY=env-secret",
			"AGENTPLANE_LLM_INGRESS_LOG_LLM_REQUESTS=true",
			"AGENTPLANE_LLM_INGRESS_PORT=7070",
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if c.TokenAudience != "flag-audience" || c.Port != 9090 || c.LiteLLMKey != "env-secret" || c.LogLLMRequests ||
		len(c.AllowedServiceAccountNamespaces) != 1 || c.AllowedServiceAccountNamespaces[0] != "flag-namespace" ||
		c.LiteLLMURL != "http://127.0.0.1:8100/prefix" || len(c.Models) != 1 || c.Models[0].TotalContextBudgetTokens != 8192 {
		t.Fatalf("unexpected merged config: %+v", c)
	}
}

func TestLoadConfigRejectsInvalidModelBudgets(t *testing.T) {
	for _, value := range []string{"8.0", `"8"`, "true", "null"} {
		t.Run(value, func(t *testing.T) {
			path := writeSettings(t, "allowed_service_account_namespaces: [test-namespace]\n"+
				"token_audience: test-audience\n"+
				"litellm_url: http://127.0.0.1:8100\n"+
				"litellm_key: never-include-this-secret\n"+
				"models:\n  - model: test/model\n    total_context_budget_tokens: "+value+"\n")
			_, err := loadConfig(nil, []string{configFileEnv + "=" + path})
			if err == nil || strings.Contains(err.Error(), "never-include-this-secret") {
				t.Fatalf("got unsafe or missing config error: %v", err)
			}
		})
	}
}

func TestLoadConfigRejectsUnknownSettingsWithoutEchoingValues(t *testing.T) {
	base := `allowed_service_account_namespaces: [test-namespace]
token_audience: test-audience
litellm_url: http://127.0.0.1:8100
litellm_key: never-include-this-secret
models: []
`
	path := writeSettings(t, base+`unknown_setting: never-include-this-secret
`)
	_, err := loadConfig(nil, []string{configFileEnv + "=" + path})
	if err == nil || strings.Contains(err.Error(), "never-include-this-secret") {
		t.Fatalf("got unsafe or missing config error: %v", err)
	}
	path = writeSettings(t, base)
	_, err = loadConfig(nil, []string{
		configFileEnv + "=" + path,
		"AGENTPLANE_LLM_INGRESS_UNKNOWN=never-include-this-secret",
	})
	if err == nil || strings.Contains(err.Error(), "never-include-this-secret") {
		t.Fatalf("got unsafe or missing environment error: %v", err)
	}
}
