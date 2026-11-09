package main

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const testSettings = `database_url: postgres://edge
actions_target: actions:8080
caller_service_account_namespaces: [test-callers]
mintable_accounts: [test-callers/test-caller]
operator_oidc:
  issuer: https://operator.example
  audience: edge
  jwks_uri: https://operator.example/jwks
`

// settingsFile clears inherited edge settings and points the loader at a file holding contents.
func settingsFile(t *testing.T, contents string) string {
	t.Helper()
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		if strings.HasPrefix(name, settingPrefix) {
			t.Setenv(name, "")
		}
	}
	path := filepath.Join(t.TempDir(), "edge.yaml")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(settingPrefix+"CONFIG_FILE", path)
	return path
}

func TestOAuthJSONNestedEnvironmentOverridesAndSingleDocument(t *testing.T) {
	path := settingsFile(t, testSettings)
	t.Setenv(settingPrefix+"OAUTH", `{"upstream_client_id":"client","upstream_client_secret_file":"/secret","upstream_subject":"old","upstream_issuer":"https://operator.example","base_url":"https://edge.example","integration_app_url":"https://app.example","approving_operator":{"issuer":"https://operator.example","subject":"old"}}`)
	t.Setenv(settingPrefix+"OAUTH__UPSTREAM_SUBJECT", "current")
	t.Setenv(settingPrefix+"OAUTH__APPROVING_OPERATOR__SUBJECT", "current-operator")
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.OAuth.UpstreamSubject != "current" || c.OAuth.ApprovingOperator.Subject != "current-operator" {
		t.Fatalf("nested OAuth overrides lost: %+v", c.OAuth)
	}
	if err := os.WriteFile(path, []byte(testSettings+"---\nport: 9000\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := loadConfig(); err == nil {
		t.Fatal("accepted a second settings document")
	}
}

func TestConnectionIdentities(t *testing.T) {
	settingsFile(t, testSettings)
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.ConnectionIdentities != nil {
		t.Fatalf("absent connection_identities parsed as %+v", c.ConnectionIdentities)
	}

	settingsFile(t, testSettings+"connection_identities:\n  policy_namespace: test-policy\n  names: [test-identity, test-other]\n")
	c, err = loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if got := c.ConnectionIdentities; got == nil || got.PolicyNamespace != "test-policy" ||
		!slices.Equal(got.Names, []string{"test-identity", "test-other"}) {
		t.Fatalf("connection_identities = %+v", got)
	}
}

func TestConnectionIdentitiesRejectsIncompleteOrInvalid(t *testing.T) {
	for name, block := range map[string]string{
		"empty":             "connection_identities: {}\n",
		"no namespace":      "connection_identities:\n  names: [test-identity]\n",
		"no names":          "connection_identities:\n  policy_namespace: test-policy\n",
		"empty names":       "connection_identities:\n  policy_namespace: test-policy\n  names: []\n",
		"invalid namespace": "connection_identities:\n  policy_namespace: Test_Policy\n  names: [test-identity]\n",
		"invalid name":      "connection_identities:\n  policy_namespace: test-policy\n  names: [\"\"]\n",
		"unknown key":       "connection_identities:\n  policy_namespace: test-policy\n  names: [test-identity]\n  accounts: [x]\n",
	} {
		t.Run(name, func(t *testing.T) {
			settingsFile(t, testSettings+block)
			if c, err := loadConfig(); err == nil {
				t.Fatalf("accepted %+v", c.ConnectionIdentities)
			}
		})
	}
}
