package main

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
)

const databaseURLEnv = settingPrefix + "DATABASE_URL=postgres://test-user:never-print-this@test-database/actions"

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
	c, err := loadTestConfig(t, "")
	if err != nil {
		t.Fatal(err)
	}
	if c.DatabaseURL == "" || c.Host != "0.0.0.0" || c.Port != 8080 {
		t.Errorf("settings: %+v", c)
	}
}

func TestAnEnvironmentVariableOverridesTheFile(t *testing.T) {
	c, err := loadTestConfig(t, "host: 127.0.0.1\nport: 9000\n", settingPrefix+"PORT=9001")
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
	c, err := loadTestConfig(t, testActionGroups)
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
	c, err := loadTestConfig(t, "")
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.ActionGroups.Resolve("test-group", "test-action"); !errors.Is(err, catalog.ErrUnknownAction) {
		t.Errorf("Resolve = %v", err)
	}
}

func TestSettingsAreRefusedWithoutEchoingTheirValues(t *testing.T) {
	for name, file := range map[string]string{
		// A key of the Python service's settings that this service does not read yet is refused like any other.
		"unknown key":                        "token_audience: never-print-this\n",
		"port of a word":                     "port: never-print-this\n",
		"port out of range":                  "port: 70000\n",
		"empty host":                         "host: \"\"\n",
		"action group":                       strings.Replace(testActionGroups, "server_id: test-group", "server_id: never-print-this", 1),
		"action groups key":                  strings.Replace(testActionGroups, "X-Test-Header: test", "1: never-print-this", 1),
		"action group field of another type": strings.Replace(testActionGroups, "available: yes", "available: never-print-this", 1),
		"unknown action group field":         strings.Replace(testActionGroups, "available: yes", "health: never-print-this", 1),
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
	if _, err := loadTestConfig(t, "", settingPrefix+"DATABASE_URL="); err == nil {
		t.Error("accepted without a database URL")
	}
	for _, variable := range []string{"ACTION_GROUPS={}", "ACTION_GROUPS__TEST_GROUP__AVAILABLE=false"} {
		if _, err := loadTestConfig(t, testActionGroups, settingPrefix+variable); err == nil {
			t.Errorf("accepted %s, which only the file sets", variable)
		}
	}
}
