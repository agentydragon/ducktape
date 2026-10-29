package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/llm_ingress/configpb"
)

const testRuntimeSecret = "test-runtime-secret-never-include-in-errors"

func loadTestConfig(t *testing.T, path string) (*configpb.Config, error) {
	t.Helper()
	t.Setenv(configFileEnv, path)
	t.Setenv(litellmKeyEnv, testRuntimeSecret)
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

func TestLoadConfigReadsFileAndRuntimeSecret(t *testing.T) {
	path := writeSettings(t, `allowed_service_account_namespaces: [yaml-namespace]
token_audience: yaml-audience
litellm_url: http://127.0.0.1:8100/prefix
log_llm_requests: true
port: 6060
codex_model_catalog_file: /yaml/codex_models.json
models:
  - model: test/model
    total_context_budget_tokens: 8192
`)
	c, err := loadTestConfig(t, path)
	if err != nil {
		t.Fatal(err)
	}
	if c.TokenAudience != "yaml-audience" || c.GetPort() != 6060 || c.LitellmKey != testRuntimeSecret || !c.GetLogLlmRequests() ||
		len(c.AllowedServiceAccountNamespaces) != 1 || c.AllowedServiceAccountNamespaces[0] != "yaml-namespace" ||
		c.LitellmUrl != "http://127.0.0.1:8100/prefix" || len(c.Models) != 1 || c.Models[0].TotalContextBudgetTokens != 8192 ||
		c.CodexModelCatalogFile != "/yaml/codex_models.json" {
		t.Fatal("settings file and runtime Secret were not loaded correctly")
	}
}

func TestAttributionConfig(t *testing.T) {
	base := `allowed_service_account_namespaces: [test-namespace]
token_audience: test-audience
litellm_url: http://127.0.0.1:8100
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
			c, err := loadTestConfig(t, path)
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
				"models:\n  - model: test/model\n    total_context_budget_tokens: "+value+"\n")
			_, err := loadTestConfig(t, path)
			if err == nil || strings.Contains(err.Error(), testRuntimeSecret) {
				t.Fatalf("got unsafe or missing config error: %v", err)
			}
		})
	}
}

func TestLoadConfigRejectsUnknownFileSettingsWithoutEchoingValues(t *testing.T) {
	base := `allowed_service_account_namespaces: [test-namespace]
token_audience: test-audience
litellm_url: http://127.0.0.1:8100
models: []
`
	path := writeSettings(t, base+`unknown_setting: never-include-this-secret
`)
	_, err := loadTestConfig(t, path)
	if err == nil || strings.Contains(err.Error(), "never-include-this-secret") || strings.Contains(err.Error(), testRuntimeSecret) {
		t.Fatalf("got unsafe or missing config error: %v", err)
	}
}

func TestLoadConfigDefaultsAndInvalidFileSettings(t *testing.T) {
	const base = "allowed_service_account_namespaces: [test-namespace]\ntoken_audience: test-audience\nlitellm_url: http://backend.example.test:4000\n"
	for _, test := range []struct {
		name, yaml string
		port       int32
		fails      bool
	}{
		{name: "omitted defaults", port: 8080},
		{name: "ephemeral port", yaml: "port: 0\n", port: 0},
		{name: "duplicate YAML key", yaml: "host: other.example.test\nhost: never-include-this-secret\n", fails: true},
		{name: "empty host", yaml: "host: ''\n", fails: true},
		{name: "port outside range", yaml: "port: 65536\n", fails: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			path := writeSettings(t, base+test.yaml)
			c, err := loadTestConfig(t, path)
			if test.fails {
				if err == nil || strings.Contains(err.Error(), "never-include-this-secret") || strings.Contains(err.Error(), testRuntimeSecret) {
					t.Fatalf("unsafe or missing config error: %v", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if c.GetHost() != "0.0.0.0" || c.GetPort() != test.port || c.GetLogLlmRequests() || c.AllowedServiceAccountNamespaces[0] != "test-namespace" {
				t.Fatal("omitted listener settings must use defaults and explicit zero must remain zero")
			}
		})
	}
}

func TestLoadConfigRequiresRuntimeSecret(t *testing.T) {
	path := writeSettings(t, "allowed_service_account_namespaces: [test-namespace]\n"+
		"token_audience: test-audience\nlitellm_url: http://backend.example.test:4000\n")
	t.Setenv(configFileEnv, path)
	t.Setenv(litellmKeyEnv, "")
	if _, err := loadConfig(); err == nil {
		t.Fatal("startup accepted a missing runtime Secret")
	}
}
