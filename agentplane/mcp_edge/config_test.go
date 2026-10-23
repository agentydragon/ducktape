package main

import (
	"os"
	"path/filepath"
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

func TestOAuthJSONNestedEnvironmentOverridesAndSingleDocument(t *testing.T) {
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		if strings.HasPrefix(name, settingPrefix) {
			t.Setenv(name, "")
		}
	}
	path := filepath.Join(t.TempDir(), "edge.yaml")
	if err := os.WriteFile(path, []byte(testSettings), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(settingPrefix+"CONFIG_FILE", path)
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
