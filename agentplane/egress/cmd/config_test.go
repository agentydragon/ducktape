package main

import (
	"net/netip"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const requiredFlags = "--rules-namespace=rules --credentials-namespace=secrets --ca-cert=/ca.crt --ca-key=/ca.key " +
	"--token-audience=hop"

func required(extra ...string) []string {
	return append(strings.Fields(requiredFlags), extra...)
}

func writeSettings(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "settings.yaml")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

const databaseURLEnv = settingPrefix + "DATABASE_URL=postgresql://user:never-print-this@db/egress"

func TestEachSourceOverridesTheOneBeforeIt(t *testing.T) {
	path := writeSettings(t, `allowed_service_account_namespaces: [file-namespace]
projected_token_audiences: [file-audience]
exempt_networks: [127.0.0.0/8]
admin_port: 1111
listen_port: 2222
`)
	c, err := loadConfig(required(`--listen-port=3333`), []string{
		configFileEnv + "=" + path, databaseURLEnv,
		settingPrefix + "ADMIN_PORT=4444", settingPrefix + "LISTEN_PORT=5555",
		settingPrefix + "PROJECTED_TOKEN_AUDIENCES=[\"env-audience\"]",
	})
	if err != nil {
		t.Fatal(err)
	}
	if c.ListenPort != 3333 || c.AdminPort != 4444 || c.AgentAPIPort != 8082 ||
		c.AllowedServiceAccountNamespaces[0] != "file-namespace" ||
		len(c.ProjectedTokenAudiences) != 1 || c.ProjectedTokenAudiences[0] != "env-audience" ||
		len(c.ExemptNetworks) != 1 || c.ExemptNetworks[0] != netip.MustParsePrefix("127.0.0.0/8") {
		t.Fatalf("unexpected merged config: %+v", c)
	}
}

func TestSettingsFileRejectsWhatItDoesNotKnow(t *testing.T) {
	path := writeSettings(t, "allowed_service_account_namespaces: [ns]\nconfdir: /tmp\n")
	if _, err := loadConfig(required(), []string{configFileEnv + "=" + path, databaseURLEnv}); err == nil {
		t.Fatal("a key outside the settings was accepted")
	}
	if _, err := loadConfig(required("--confdir=/tmp"), []string{databaseURLEnv}); err == nil {
		t.Fatal("a flag outside the settings was accepted")
	}
}

func TestSettingsFileThatIsNamedMustExist(t *testing.T) {
	if _, err := loadConfig(required(), []string{configFileEnv + "=/nonexistent/settings.yaml", databaseURLEnv}); err == nil {
		t.Fatal("a missing settings file ran on defaults")
	}
}

func TestErrorsNeverCarryTheDatabaseURL(t *testing.T) {
	environ := []string{databaseURLEnv, settingPrefix + "ALLOWED_SERVICE_ACCOUNT_NAMESPACES=[\"ns\"]"}
	for _, bad := range [][]string{
		required("--decision-batch-size=0"),
		required("--listen-port=not-a-number"),
		required("--log-levels={\"agentplane.other\":\"INFO\"}"),
		required("--exempt-networks=[\"not-a-prefix\"]"),
	} {
		_, err := loadConfig(bad, environ)
		if err == nil {
			t.Fatalf("%v accepted", bad)
		}
		if strings.Contains(err.Error(), "never-print-this") {
			t.Fatalf("error carries the database URL: %v", err)
		}
	}
}

func TestSettingsWithoutADefaultAreRequired(t *testing.T) {
	complete := append(required(), "--allowed-service-account-namespaces=[\"ns\"]")
	if _, err := loadConfig(complete, []string{databaseURLEnv}); err != nil {
		t.Fatal(err)
	}
	for _, missing := range []string{"rules-namespace", "credentials-namespace", "ca-cert", "ca-key", "token-audience"} {
		var rest []string
		for _, arg := range complete {
			if !strings.HasPrefix(arg, "--"+missing+"=") {
				rest = append(rest, arg)
			}
		}
		if _, err := loadConfig(rest, []string{databaseURLEnv}); err == nil {
			t.Fatalf("ran without %s", missing)
		}
	}
	if _, err := loadConfig(complete, nil); err == nil {
		t.Fatal("ran without a database URL")
	}
}

func TestADeploymentNamingNoWorkloadNamespaceIsRefused(t *testing.T) {
	for name, namespaces := range map[string]string{
		"absent": "", "empty list": `[]`, "empty entry": `["ns", ""]`,
	} {
		t.Run(name, func(t *testing.T) {
			args := required()
			if namespaces != "" {
				args = append(args, "--allowed-service-account-namespaces="+namespaces)
			}
			if _, err := loadConfig(args, []string{databaseURLEnv}); err == nil {
				t.Fatal("ran with no usable workload namespace")
			}
		})
	}
}

// The manifests render each setting under its file key, as its upper-cased environment variable
// and as its kebab-case flag.
func TestEverySettingIsAFileKeyAnEnvironmentVariableAndAFlag(t *testing.T) {
	configType := reflect.TypeFor[config]()
	for i := range configType.NumField() {
		key := configType.Field(i).Tag.Get("yaml")
		flag := "--" + strings.ReplaceAll(key, "_", "-") + "=value"
		given, err := flagValues(environmentOptions(nil), []string{flag})
		if err != nil || given[settingPrefix+strings.ToUpper(key)] != "value" {
			t.Errorf("%s is not the flag %s: %v, %v", key, flag, given, err)
		}
	}
}
