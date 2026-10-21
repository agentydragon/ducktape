package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
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

func TestSettingsAreRefusedWithoutEchoingTheirValues(t *testing.T) {
	for name, file := range map[string]string{
		// A key of the Python service's settings that this service does not read yet is refused like any other.
		"unknown key":       "token_audience: never-print-this\n",
		"port of a word":    "port: never-print-this\n",
		"port out of range": "port: 70000\n",
		"empty host":        "host: \"\"\n",
	} {
		t.Run(name, func(t *testing.T) {
			_, err := loadTestConfig(t, file)
			if err == nil {
				t.Fatal("accepted")
			}
			if strings.Contains(err.Error(), "never-print-this") {
				t.Errorf("the error carries a setting's value: %v", err)
			}
		})
	}
	if _, err := loadTestConfig(t, "", settingPrefix+"DATABASE_URL="); err == nil {
		t.Error("accepted without a database URL")
	}
}
