package main

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/api"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
)

const databaseURLEnv = settingPrefix + "DATABASE_URL=postgres://test-user:never-print-this@test-database/actions"

// requiredFile is a deployment's settings file with every setting that has no default.
const requiredFile = `token_audience: test-actions
caller_service_account_namespaces: [test-agentplane]
`

func loadTestConfig(t *testing.T, file string, environ ...string) (config, error) {
	t.Helper()
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		if strings.HasPrefix(name, settingPrefix) {
			t.Setenv(name, "")
		}
	}
	path := filepath.Join(t.TempDir(), "settings.yaml")
	if err := os.WriteFile(path, []byte(file), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, entry := range append([]string{settingPrefix + "CONFIG_FILE=" + path, databaseURLEnv}, environ...) {
		name, value, _ := strings.Cut(entry, "=")
		t.Setenv(name, value)
	}
	return loadConfig()
}

func TestWhatADeploymentLeavesUnsetTakesItsDefault(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile)
	if err != nil {
		t.Fatal(err)
	}
	if c.DatabaseURL == "" || c.Host != "0.0.0.0" || c.Port != 8080 || len(c.ReaderAccounts) != 0 || c.operatorsEnabled() {
		t.Errorf("settings: %+v", c)
	}
}

func TestAnEnvironmentVariableOverridesTheFile(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile+"host: 127.0.0.1\nport: 9000\n", settingPrefix+"PORT=9001")
	if err != nil {
		t.Fatal(err)
	}
	if c.Host != "127.0.0.1" || c.Port != 9001 {
		t.Errorf("file and environment overrides: %+v", c)
	}
}

// A group as the deployment's settings file holds it, keys of every case included.
const testActionGroups = `action_groups:
  test-group:
    title: Test group
    description: A group of the settings file.
    available: yes
    executor:
      kind: mcp
      description: Test MCP executor.
      config:
        server_id: test-group
        headers: {X-Test-Header: test}
    actions:
      test-action:
        description: Test Action.
        input_schema:
          type: object
          properties: {testName: {type: string}}
          additionalProperties: false
`

func TestActionGroupsAreReadFromTheFileWithTheirKeysAsWritten(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile+testActionGroups)
	if err != nil {
		t.Fatal(err)
	}
	group, action, err := c.ActionGroups.Resolve("test-group", "test-action")
	if err != nil {
		t.Fatal(err)
	}
	if binding := group.Executor.(catalog.MCPBinding); !strings.Contains(string(binding.Config), `"X-Test-Header"`) {
		t.Errorf("the MCP configuration is read as %s", binding.Config)
	}
	for _, key := range []string{`"additionalProperties"`, `"testName"`} {
		if !strings.Contains(string(action.InputSchema), key) {
			t.Errorf("the input schema is read as %s", action.InputSchema)
		}
	}
}

func TestWithoutActionGroupsNoActionResolves(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.ActionGroups.Resolve("test-group", "test-action"); !errors.Is(err, catalog.ErrUnknownAction) {
		t.Errorf("Resolve = %v", err)
	}
}

func TestReaderAccountsAndTheOperatorIssuerAreRead(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile+`reader_accounts:
  - {namespace: test-agentplane, name: test-notifications}
  - test-agentplane/test-other-reader
operator_oidc:
  issuer: https://issuer.test.example/
  jwks_uri: https://issuer.test.example/jwks/
`, settingPrefix+"OPERATOR_OIDC__AUDIENCE=test-actions-audience")
	if err != nil {
		t.Fatal(err)
	}
	wantReaders := []auth.Account{{Namespace: "test-agentplane", Name: "test-notifications"}, {Namespace: "test-agentplane", Name: "test-other-reader"}}
	wantOperators := api.OperatorOIDCSettings{
		Issuer: "https://issuer.test.example/", Audience: "test-actions-audience", JWKSURI: "https://issuer.test.example/jwks/",
		TokenProfile: api.ProfileAuthentik,
	}
	if !slices.Equal(c.ReaderAccounts, wantReaders) || c.OperatorOIDC != wantOperators || !c.operatorsEnabled() {
		t.Errorf("settings: %+v", c)
	}
}

func TestSettingsAreRefusedWithoutEchoingTheirValues(t *testing.T) {
	for name, file := range map[string]string{
		"unknown key":          requiredFile + "test_unknown_setting: never-print-this\n",
		"port of a word":       requiredFile + "port: never-print-this\n",
		"port out of range":    requiredFile + "port: 70000\n",
		"empty host":           requiredFile + "host: \"\"\n",
		"no audience":          strings.Replace(requiredFile, "token_audience: test-actions\n", "", 1),
		"no caller namespaces": strings.Replace(requiredFile, "[test-agentplane]", "[]", 1),
		"reader of a word":     requiredFile + "reader_accounts: [never-print-this]\n",
		"operator issuer without its audience": requiredFile + `operator_oidc:
  issuer: https://never-print-this.test.example/
  jwks_uri: https://issuer.test.example/jwks/
`,
		"operator keys over plain http": requiredFile + `operator_oidc:
  issuer: https://issuer.test.example/
  audience: test-actions-audience
  jwks_uri: http://never-print-this.test.example/jwks/
`,
		"unknown token profile": requiredFile + `operator_oidc:
  issuer: https://issuer.test.example/
  audience: test-actions-audience
  jwks_uri: https://issuer.test.example/jwks/
  token_profile: never-print-this
`,
		"action group":                       requiredFile + strings.Replace(testActionGroups, "server_id: test-group", "server_id: never-print-this", 1),
		"action groups key":                  requiredFile + strings.Replace(testActionGroups, "X-Test-Header: test", "1: never-print-this", 1),
		"action group field of another type": requiredFile + strings.Replace(testActionGroups, "available: yes", "available: never-print-this", 1),
		"unknown action group field":         requiredFile + strings.Replace(testActionGroups, "available: yes", "health: never-print-this", 1),
	} {
		t.Run(name, func(t *testing.T) {
			_, err := loadTestConfig(t, file)
			if err == nil {
				t.Fatal("accepted")
			}
			// yaml.v3 quotes a value's first seven characters at most.
			if strings.Contains(err.Error(), "never-p") {
				t.Errorf("the error carries a setting's value: %v", err)
			}
		})
	}
	if _, err := loadTestConfig(t, requiredFile, settingPrefix+"DATABASE_URL="); err == nil {
		t.Error("accepted without a database URL")
	}
	for _, variable := range []string{"ACTION_GROUPS={}", "ACTION_GROUPS__TEST_GROUP__AVAILABLE=false"} {
		if _, err := loadTestConfig(t, requiredFile+testActionGroups, settingPrefix+variable); err == nil {
			t.Errorf("accepted %s, which only the file sets", variable)
		}
	}
}
