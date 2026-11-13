package main

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

func requiredSettings() map[string]any {
	return map[string]any{
		"policy_namespace": "policies", "subject_namespaces": []string{"agents", "tools"},
		"accounts_namespace": "accounts",
	}
}

func settingsFile(t *testing.T, content string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(configFileEnv, path)
}

func writeSettings(t *testing.T, settings map[string]any) {
	t.Helper()
	content, err := yaml.Marshal(settings)
	if err != nil {
		t.Fatal(err)
	}
	settingsFile(t, string(content))
}

func TestSettingsComeFromTheFile(t *testing.T) {
	settings := requiredSettings()
	settings["metrics_bind_address"] = ":9090"
	settings["health_probe_bind_address"] = ":9091"
	settings["grpc_bind_address"] = ":9092"
	settings["grpc_audience"] = "custom-policy"
	settings["reader_accounts"] = []string{"policies/reader"}
	settings["writer_accounts"] = []string{"policies/app"}
	writeSettings(t, settings)
	// The old scalar and comma-separated allowlist overrides cannot change file values.
	t.Setenv("AGENTPLANE_POLICY_CONTROLLER_POLICY_NAMESPACE", "other")
	t.Setenv("AGENTPLANE_POLICY_CONTROLLER_SUBJECT_NAMESPACES", "other")
	t.Setenv("AGENTPLANE_POLICY_CONTROLLER_WRITER_ACCOUNTS", "other/admin")
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.PolicyNamespace != "policies" || !slices.Equal(c.SubjectNamespaces, []string{"agents", "tools"}) || c.AccountsNamespace != "accounts" ||
		c.MetricsBindAddress != ":9090" || c.HealthProbeBindAddress != ":9091" || c.GRPCBindAddress != ":9092" || c.GRPCAudience != "custom-policy" ||
		!slices.Equal(c.ReaderAccounts, []string{"policies/reader"}) || !slices.Equal(c.WriterAccounts, []string{"policies/app"}) {
		t.Fatalf("settings were not read: %+v", c)
	}
}

func TestBindAddressesDefaultWhenOmitted(t *testing.T) {
	writeSettings(t, requiredSettings())
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.MetricsBindAddress != ":8080" || c.HealthProbeBindAddress != ":8081" || c.GRPCBindAddress != ":8082" || c.GRPCAudience != "agentplane-policy" {
		t.Fatalf("defaults were lost: %+v", c)
	}
}

func TestEachRequiredSettingIsRequired(t *testing.T) {
	for omitted := range requiredSettings() {
		t.Run(omitted, func(t *testing.T) {
			settings := requiredSettings()
			delete(settings, omitted)
			writeSettings(t, settings)
			if _, err := loadConfig(); err == nil {
				t.Fatalf("started without %s", omitted)
			}
		})
	}
}

func TestInvalidNamespaceNamesAreRefused(t *testing.T) {
	for _, subjects := range [][]string{{"agents", ""}, {"agents", " tools"}, {"Agents"}} {
		t.Run(strings.Join(subjects, ","), func(t *testing.T) {
			settings := requiredSettings()
			settings["subject_namespaces"] = subjects
			writeSettings(t, settings)
			if _, err := loadConfig(); err == nil {
				t.Fatal("started with a namespace name that cannot exist")
			}
		})
	}
}

func TestWorkloadIssuanceIsConfiguredWhole(t *testing.T) {
	for name, extra := range map[string]map[string]any{
		"an issuer without namespaces": {"workload_issuer": "test-workload-issuer"},
		"namespaces without an issuer": {"workload_namespaces": []string{"agents"}},
		"an invalid namespace":         {"workload_issuer": "test-workload-issuer", "workload_namespaces": []string{"Agents"}},
	} {
		t.Run(name, func(t *testing.T) {
			settings := requiredSettings()
			for key, value := range extra {
				settings[key] = value
			}
			writeSettings(t, settings)
			if _, err := loadConfig(); err == nil {
				t.Fatal("started with invalid workload issuance settings")
			}
		})
	}
	settings := requiredSettings()
	settings["workload_issuer"] = "test-workload-issuer"
	settings["workload_namespaces"] = []string{"agents", "tools"}
	writeSettings(t, settings)
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.WorkloadIssuer != "test-workload-issuer" || !slices.Equal(c.WorkloadNamespaces, []string{"agents", "tools"}) {
		t.Fatalf("workload issuance settings were not read: %+v", c)
	}
}

func TestMissingOrUnreadableFileIsRefused(t *testing.T) {
	for _, path := range []string{"", filepath.Join(t.TempDir(), "missing.yaml")} {
		t.Run(path, func(t *testing.T) {
			t.Setenv(configFileEnv, path)
			if _, err := loadConfig(); err == nil {
				t.Fatal("started without a readable settings file")
			}
		})
	}
}

func TestInvalidSettingsFilesAreRefused(t *testing.T) {
	for name, content := range map[string]string{
		"empty":              "",
		"unknown key":        "policy_namespace: policies\nsubject_namespaces: [agents]\naccounts_namespace: accounts\nunknown: value\n",
		"multiple documents": "policy_namespace: policies\n---\naccounts_namespace: accounts\n",
		"scalar list":        "policy_namespace: policies\nsubject_namespaces: agents,tools\naccounts_namespace: accounts\n",
	} {
		t.Run(name, func(t *testing.T) {
			settingsFile(t, content)
			if _, err := loadConfig(); err == nil {
				t.Fatal("started with invalid settings file")
			}
		})
	}
}
