package main

import (
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
)

const databaseURLEnv = settingPrefix + "DATABASE_URL=postgresql://user:never-print-this@db/egress"

// required is the environment of a deployment that sets every setting without a default.
func required(t *testing.T, extra ...string) []string {
	t.Helper()
	return append([]string{
		settingPrefix + "RULES_NAMESPACE=rules", settingPrefix + "CREDENTIALS_NAMESPACE=secrets",
		settingPrefix + "CA_CERT=/ca.crt", settingPrefix + "CA_KEY=/ca.key", settingPrefix + "TOKEN_AUDIENCE=hop",
		settingPrefix + "CONFIG_FILE=" + writeSettings(t, "allowed_service_account_namespaces: [ns]\n"), databaseURLEnv,
	}, extra...)
}

func loadTestConfig(t *testing.T, environ []string) (config, error) {
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

func TestCollectionsLoadFromTheFileAndScalarsFromTheEnvironment(t *testing.T) {
	path := writeSettings(t, `allowed_service_account_namespaces: [file-namespace]
projected_token_audiences: [file-audience]
exempt_networks: [127.0.0.0/8]
admin_listen_address: 127.0.0.1:1111
`)
	c, err := loadTestConfig(t, []string{
		settingPrefix + "CONFIG_FILE=" + path, settingPrefix + "RULES_NAMESPACE=rules",
		settingPrefix + "CREDENTIALS_NAMESPACE=secrets", settingPrefix + "CA_CERT=/ca.crt",
		settingPrefix + "CA_KEY=/ca.key", settingPrefix + "TOKEN_AUDIENCE=hop", databaseURLEnv,
		settingPrefix + "LISTEN_ADDRESS=127.0.0.1:5555",
	})
	if err != nil {
		t.Fatal(err)
	}
	if c.ListenAddress != "127.0.0.1:5555" || c.AdminListenAddress != "127.0.0.1:1111" || c.AgentAPIListenAddress != "0.0.0.0:8082" ||
		!slices.Equal(c.AllowedServiceAccountNamespaces, []string{"file-namespace"}) ||
		!slices.Equal(c.ProjectedTokenAudiences, []string{"file-audience"}) ||
		!slices.Equal(c.ExemptNetworks, []netip.Prefix{netip.MustParsePrefix("127.0.0.0/8")}) {
		t.Fatalf("unexpected merged config: %+v", c)
	}
}

func TestErrorsNeverCarryTheDatabaseURL(t *testing.T) {
	for _, bad := range [][]string{
		{settingPrefix + "DECISION_BATCH_SIZE=0"},
		{settingPrefix + "CONFIG_FILE=" + writeSettings(t, "allowed_service_account_namespaces: [ns]\nlisten_port: 8888\n")},
		{settingPrefix + "CONFIG_FILE=" + writeSettings(t, "allowed_service_account_namespaces: [ns]\nlog_levels: {agentplane.other: INFO}\n")},
		{settingPrefix + "CONFIG_FILE=" + writeSettings(t, "allowed_service_account_namespaces: [ns]\nexempt_networks: [not-a-prefix]\n")},
	} {
		_, err := loadTestConfig(t, required(t, bad...))
		if err == nil {
			t.Fatalf("%v accepted", bad)
		}
		if strings.Contains(err.Error(), "never-print-this") {
			t.Fatalf("error carries the database URL: %v", err)
		}
	}
}

func TestSettingsWithoutADefaultAreRequired(t *testing.T) {
	if _, err := loadTestConfig(t, required(t)); err != nil {
		t.Fatal(err)
	}
	for _, missing := range []string{
		"RULES_NAMESPACE", "CREDENTIALS_NAMESPACE", "CA_CERT", "CA_KEY", "TOKEN_AUDIENCE", "DATABASE_URL",
	} {
		rest := slices.DeleteFunc(required(t), func(item string) bool {
			return strings.HasPrefix(item, settingPrefix+missing+"=")
		})
		if _, err := loadTestConfig(t, rest); err == nil {
			t.Fatalf("ran without %s", missing)
		}
	}
}

func TestADeploymentNamingNoWorkloadNamespaceIsRefused(t *testing.T) {
	for name, namespaces := range map[string]string{
		"absent": "", "empty list": `[]`, "empty entry": `["ns", ""]`,
	} {
		t.Run(name, func(t *testing.T) {
			contents := ""
			if namespaces != "" {
				contents = "allowed_service_account_namespaces: " + namespaces + "\n"
			}
			environ := required(t, settingPrefix+"CONFIG_FILE="+writeSettings(t, contents))
			if _, err := loadTestConfig(t, environ); err == nil {
				t.Fatal("ran with no usable workload namespace")
			}
		})
	}
}

func TestAdminGRPCIsOffUnlessItsSettingsAreSet(t *testing.T) {
	c, err := loadTestConfig(t, required(t))
	if err != nil {
		t.Fatal(err)
	}
	if c.adminGRPCEnabled() {
		t.Fatal("admin gRPC enabled without being configured")
	}
}

func TestAdminGRPCLoadsFromTheFileAndTheEnvironment(t *testing.T) {
	path := writeSettings(t, `allowed_service_account_namespaces: [ns]
admin_grpc_audience: file-audience
admin_grpc_callers: [test-ns/test-app, test-ns/test-other]
`)
	c, err := loadTestConfig(t, required(
		t,
		settingPrefix+"CONFIG_FILE="+path, settingPrefix+"ADMIN_GRPC_AUDIENCE=env-audience",
	))
	if err != nil {
		t.Fatal(err)
	}
	callers, err := c.adminGRPCCallers()
	if err != nil {
		t.Fatal(err)
	}
	if !c.adminGRPCEnabled() || c.AdminGRPCAudience != "env-audience" ||
		!slices.Equal(callers, []auth.Account{{Namespace: "test-ns", Name: "test-app"}, {Namespace: "test-ns", Name: "test-other"}}) {
		t.Fatalf("unexpected merged config: %+v", c)
	}
}

func TestAdminGRPCIsAllOrNothing(t *testing.T) {
	const audience, callers = "admin_grpc_audience: test-audience\n", "admin_grpc_callers: [test-ns/test-app]\n"
	for name, contents := range map[string]string{
		"audience without callers": audience,
		"callers without audience": callers,
		"caller without a slash":   audience + "admin_grpc_callers: [test-ns]\n",
		"caller without namespace": audience + "admin_grpc_callers: [/test-app]\n",
		"caller without a name":    audience + "admin_grpc_callers: [test-ns/]\n",
		"caller with a deep path":  audience + "admin_grpc_callers: [test-ns/test-app/extra]\n",
	} {
		t.Run(name, func(t *testing.T) {
			path := writeSettings(t, "allowed_service_account_namespaces: [ns]\n"+contents)
			if _, err := loadTestConfig(t, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil {
				t.Fatal("accepted")
			}
		})
	}
}
