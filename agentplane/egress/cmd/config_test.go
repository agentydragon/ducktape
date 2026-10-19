package main

import (
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const databaseURLEnv = settingPrefix + "DATABASE_URL=postgresql://user:never-print-this@db/egress"

// required is the environment of a deployment that sets every setting without a default.
func required(extra ...string) []string {
	return append([]string{
		settingPrefix + "RULES_NAMESPACE=rules", settingPrefix + "CREDENTIALS_NAMESPACE=secrets",
		settingPrefix + "CA_CERT=/ca.crt", settingPrefix + "CA_KEY=/ca.key", settingPrefix + "TOKEN_AUDIENCE=hop",
		settingPrefix + "ALLOWED_SERVICE_ACCOUNT_NAMESPACES=[\"ns\"]", databaseURLEnv,
	}, extra...)
}

func writeSettings(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "settings.yaml")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestListsAndNetworksLoadFromTheFileAndTheEnvironment(t *testing.T) {
	path := writeSettings(t, `allowed_service_account_namespaces: [file-namespace]
projected_token_audiences: [file-audience]
exempt_networks: [127.0.0.0/8]
admin_port: 1111
`)
	c, err := loadConfig([]string{
		settingPrefix + "CONFIG_FILE=" + path, settingPrefix + "RULES_NAMESPACE=rules",
		settingPrefix + "CREDENTIALS_NAMESPACE=secrets", settingPrefix + "CA_CERT=/ca.crt",
		settingPrefix + "CA_KEY=/ca.key", settingPrefix + "TOKEN_AUDIENCE=hop", databaseURLEnv,
		settingPrefix + "PROJECTED_TOKEN_AUDIENCES=[\"env-audience\"]", settingPrefix + "LISTEN_PORT=5555",
	})
	if err != nil {
		t.Fatal(err)
	}
	if c.ListenPort != 5555 || c.AdminPort != 1111 || c.AgentAPIPort != 8082 ||
		!slices.Equal(c.AllowedServiceAccountNamespaces, []string{"file-namespace"}) ||
		!slices.Equal(c.ProjectedTokenAudiences, []string{"env-audience"}) ||
		!slices.Equal(c.ExemptNetworks, []netip.Prefix{netip.MustParsePrefix("127.0.0.0/8")}) {
		t.Fatalf("unexpected merged config: %+v", c)
	}
}

func TestErrorsNeverCarryTheDatabaseURL(t *testing.T) {
	for _, bad := range [][]string{
		{settingPrefix + "DECISION_BATCH_SIZE=0"},
		{settingPrefix + "LISTEN_PORT=not-a-number"},
		{settingPrefix + "LOG_LEVELS={\"agentplane.other\":\"INFO\"}"},
		{settingPrefix + "EXEMPT_NETWORKS=[\"not-a-prefix\"]"},
		{settingPrefix + "UNKNOWN_SETTING=1"},
	} {
		_, err := loadConfig(required(bad...))
		if err == nil {
			t.Fatalf("%v accepted", bad)
		}
		if strings.Contains(err.Error(), "never-print-this") {
			t.Fatalf("error carries the database URL: %v", err)
		}
	}
}

func TestSettingsWithoutADefaultAreRequired(t *testing.T) {
	if _, err := loadConfig(required()); err != nil {
		t.Fatal(err)
	}
	for _, missing := range []string{
		"RULES_NAMESPACE", "CREDENTIALS_NAMESPACE", "CA_CERT", "CA_KEY", "TOKEN_AUDIENCE", "DATABASE_URL",
	} {
		rest := slices.DeleteFunc(required(), func(item string) bool {
			return strings.HasPrefix(item, settingPrefix+missing+"=")
		})
		if _, err := loadConfig(rest); err == nil {
			t.Fatalf("ran without %s", missing)
		}
	}
}

func TestADeploymentNamingNoWorkloadNamespaceIsRefused(t *testing.T) {
	for name, namespaces := range map[string]string{
		"absent": "", "empty list": `[]`, "empty entry": `["ns", ""]`,
	} {
		t.Run(name, func(t *testing.T) {
			environ := slices.DeleteFunc(required(), func(item string) bool {
				return strings.HasPrefix(item, settingPrefix+"ALLOWED_SERVICE_ACCOUNT_NAMESPACES=")
			})
			if namespaces != "" {
				environ = append(environ, settingPrefix+"ALLOWED_SERVICE_ACCOUNT_NAMESPACES="+namespaces)
			}
			if _, err := loadConfig(environ); err == nil {
				t.Fatal("ran with no usable workload namespace")
			}
		})
	}
}
