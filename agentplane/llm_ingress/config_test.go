package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/llm_ingress/configpb"
)

func loadTestConfig(t *testing.T, environ []string) (*configpb.Config, error) {
	t.Helper()
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		if strings.HasPrefix(name, settingPrefix) {
			t.Setenv(name, "")
		}
	}
	for _, entry := range environ {
		name, value, _ := strings.Cut(entry, "=")
		t.Setenv(name, value)
	}
	return loadConfig()
}

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
log_llm_requests: true
port: 6060
codex_model_catalog_file: /yaml/codex_models.json
models:
  - model: test/model
    total_context_budget_tokens: 8192
`)
	c, err := loadTestConfig(t, []string{
		configFileEnv + "=" + path,
		"AGENTPLANE_LLM_INGRESS_TOKEN_AUDIENCE=env-audience",
		"AGENTPLANE_LLM_INGRESS_LITELLM_KEY=env-secret",
		"AGENTPLANE_LLM_INGRESS_LOG_LLM_REQUESTS=false",
		"AGENTPLANE_LLM_INGRESS_PORT=7070",
	})
	if err != nil {
		t.Fatal(err)
	}
	if c.TokenAudience != "env-audience" || c.GetPort() != 7070 || c.LitellmKey != "env-secret" || c.GetLogLlmRequests() ||
		len(c.AllowedServiceAccountNamespaces) != 1 || c.AllowedServiceAccountNamespaces[0] != "yaml-namespace" ||
		c.LitellmUrl != "http://127.0.0.1:8100/prefix" || len(c.Models) != 1 || c.Models[0].TotalContextBudgetTokens != 8192 ||
		c.CodexModelCatalogFile != "/yaml/codex_models.json" {
		t.Fatalf("unexpected merged config: %+v", c)
	}
}

func TestAttributionConfig(t *testing.T) {
	base := `allowed_service_account_namespaces: [test-namespace]
token_audience: test-audience
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
			path := writeSettings(t, base+test.yaml)
			c, err := loadTestConfig(t, []string{configFileEnv + "=" + path})
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
				if len(c.Attribution.JsonHeaders) != 1 || c.Attribution.JsonHeaders[0].Name != "x-litellm-spend-logs-metadata" || len(c.Attribution.Baggage) != 0 {
					t.Fatalf("default attribution = %+v", c.Attribution)
				}
			case "baggage only":
				if len(c.Attribution.JsonHeaders) != 0 || len(c.Attribution.Baggage) != 1 || c.Attribution.Baggage[0].Key != "Custom.PodUid" {
					t.Fatalf("baggage-only attribution = %+v", c.Attribution)
				}
			case "mixed-case custom key":
				if len(c.Attribution.JsonHeaders) != 1 || c.Attribution.JsonHeaders[0].Name != "X-Custom-Verified" ||
					c.Attribution.JsonHeaders[0].Fields[0].Key != "Agentplane.PodName" {
					t.Fatalf("custom attribution lost spelling: %+v", c.Attribution)
				}
			}
		})
	}
}

func TestLoadConfigRejectsInvalidModelBudgets(t *testing.T) {
	for _, value := range []string{`"not-a-budget"`, "null", "0", "-1"} {
		t.Run(value, func(t *testing.T) {
			path := writeSettings(t, "allowed_service_account_namespaces: [test-namespace]\n"+
				"token_audience: test-audience\n"+
				"litellm_url: http://127.0.0.1:8100\n"+
				"litellm_key: never-include-this-secret\n"+
				"models:\n  - model: test/model\n    total_context_budget_tokens: "+value+"\n")
			_, err := loadTestConfig(t, []string{configFileEnv + "=" + path})
			if err == nil || strings.Contains(err.Error(), "never-include-this-secret") {
				t.Fatalf("got unsafe or missing config error: %v", err)
			}
		})
	}
}

func TestLoadConfigRejectsUnknownFileSettingsWithoutEchoingValues(t *testing.T) {
	base := `allowed_service_account_namespaces: [test-namespace]
token_audience: test-audience
litellm_url: http://127.0.0.1:8100
litellm_key: never-include-this-secret
models: []
`
	path := writeSettings(t, base+`unknown_setting: never-include-this-secret
`)
	_, err := loadTestConfig(t, []string{configFileEnv + "=" + path})
	if err == nil || strings.Contains(err.Error(), "never-include-this-secret") {
		t.Fatalf("got unsafe or missing config error: %v", err)
	}
}

func TestLoadConfigPreservesDefaultsAndScalarEnvironmentContract(t *testing.T) {
	const base = "allowed_service_account_namespaces: [test-namespace]\ntoken_audience: test-audience\nlitellm_url: http://backend.example.test:4000\nlitellm_key: never-include-this-secret\n"
	for _, test := range []struct {
		name, yaml string
		env        []string
		port       int32
		fails      bool
	}{
		{name: "omitted defaults", port: 8080},
		{name: "empty environment ignored", env: []string{settingPrefix + "PORT="}, port: 8080},
		{name: "ephemeral port", yaml: "port: 0\n", port: 0},
		{name: "lists remain file only", env: []string{settingPrefix + "ALLOWED_SERVICE_ACCOUNT_NAMESPACES=ignored"}, port: 8080},
		{name: "invalid port override", env: []string{settingPrefix + "PORT=never-include-this-secret"}, fails: true},
		{name: "invalid boolean override", env: []string{settingPrefix + "LOG_LLM_REQUESTS=never-include-this-secret"}, fails: true},
		{name: "duplicate YAML key", yaml: "host: other.example.test\nhost: never-include-this-secret\n", fails: true},
		{name: "empty host", yaml: "host: ''\n", fails: true},
		{name: "port outside range", yaml: "port: 65536\n", fails: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			path := writeSettings(t, base+test.yaml)
			c, err := loadTestConfig(t, append(test.env, configFileEnv+"="+path))
			if test.fails {
				if err == nil || strings.Contains(err.Error(), "never-include-this-secret") {
					t.Fatalf("unsafe or missing config error: %v", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if c.GetHost() != "0.0.0.0" || c.GetPort() != test.port || c.GetLogLlmRequests() || c.AllowedServiceAccountNamespaces[0] != "test-namespace" {
				t.Fatal("settings defaults or environment precedence changed")
			}
		})
	}
}
