package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const testSettings = `actions_target: actions:8080
policy_namespace: test-policy
actions_token_file: /run/agentplane/action-token/token
operator_oidc:
  issuer: https://operator.example
  audience: edge
  jwks_uri: https://operator.example/jwks
`

func clearSettingsEnv(t *testing.T) {
	t.Helper()
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		if strings.HasPrefix(name, settingPrefix) {
			t.Setenv(name, "")
		}
	}
}

func settingsFile(t *testing.T, contents string) string {
	t.Helper()
	clearSettingsEnv(t)
	path := filepath.Join(t.TempDir(), "edge.yaml")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(settingPrefix+"CONFIG_FILE", path)
	t.Setenv(settingPrefix+"DATABASE_URL", "postgres://edge")
	return path
}

func TestLoadConfigReadsDatabaseURLFromExplicitSecretInput(t *testing.T) {
	settingsFile(t, testSettings)
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.DatabaseURL != "postgres://edge" {
		t.Fatalf("database URL = %q", c.DatabaseURL)
	}
}

func TestLoadConfigRequiresSettingsFileAndDatabaseURL(t *testing.T) {
	t.Run("missing settings file", func(t *testing.T) {
		clearSettingsEnv(t)
		if _, err := loadConfig(); err == nil {
			t.Fatal("accepted a missing settings file path")
		}
	})

	t.Run("unreadable settings file", func(t *testing.T) {
		settingsFile(t, testSettings)
		t.Setenv(settingPrefix+"CONFIG_FILE", filepath.Join(t.TempDir(), "missing.yaml"))
		if _, err := loadConfig(); err == nil {
			t.Fatal("accepted an unreadable settings file")
		}
	})

	t.Run("missing database URL", func(t *testing.T) {
		settingsFile(t, testSettings)
		t.Setenv(settingPrefix+"DATABASE_URL", "")
		if _, err := loadConfig(); err == nil {
			t.Fatal("accepted a missing Secret-backed database URL")
		}
	})
}

func TestConfigFileRejectsUnknownKeysAndAdditionalDocuments(t *testing.T) {
	t.Run("unknown key", func(t *testing.T) {
		settingsFile(t, testSettings+"unexpected_setting: true\n")
		if _, err := loadConfig(); err == nil {
			t.Fatal("accepted an unknown settings key")
		}
	})

	t.Run("second document", func(t *testing.T) {
		path := settingsFile(t, testSettings)
		if err := os.WriteFile(path, []byte(testSettings+"---\nport: 9000\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := loadConfig(); err == nil {
			t.Fatal("accepted a second settings document")
		}
	})
}

func TestStaticEnvironmentDoesNotOverrideFileSettings(t *testing.T) {
	settingsFile(t, testSettings)
	t.Setenv(settingPrefix+"HOST", "invalid host")
	t.Setenv(settingPrefix+"PORT", "9000")
	t.Setenv(settingPrefix+"ACTIONS_TARGET", "overridden:9999")
	t.Setenv(settingPrefix+"OAUTH", "not-json")
	t.Setenv(settingPrefix+"OAUTH__UPSTREAM_SUBJECT", "overridden")

	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.Host != "0.0.0.0" || c.Port != 8080 || c.ActionsTarget != "actions:8080" || c.OAuth != nil {
		t.Fatalf("static settings were changed by environment variables: %+v", c)
	}
}

func TestOAuthSettingsComeFromFileAndValidateWithProjectedToken(t *testing.T) {
	oauthPath := filepath.Join(t.TempDir(), "oauth.yaml")
	oauth := `upstream_client_id: client
upstream_client_secret_file: /etc/agentplane-mcp/client-secret
base_url: https://edge.example
integration_app_url: https://app.example
upstream_issuer: https://operator.example/issuer
upstream_subject: SubjectCaseSensitive
approving_operator:
  issuer: https://operator.example
  subject: OperatorCaseSensitive
`
	if err := os.WriteFile(oauthPath, []byte(oauth), 0o600); err != nil {
		t.Fatal(err)
	}
	settings := testSettings + fmt.Sprintf("oauth_file: %q\n", oauthPath)
	settingsFile(t, settings)
	t.Setenv(settingPrefix+"OAUTH_FILE", filepath.Join(t.TempDir(), "ignored.yaml"))
	t.Setenv(settingPrefix+"OAUTH__UPSTREAM_SUBJECT", "overridden")
	c, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.OAuth == nil || c.OAuth.UpstreamSubject != "SubjectCaseSensitive" || c.OAuth.ApprovingOperator.Subject != "OperatorCaseSensitive" {
		t.Fatalf("OAuth identity values were not preserved exactly: %+v", c.OAuth)
	}
	if c.OAuth.UpstreamClientSecretFile != "/etc/agentplane-mcp/client-secret" {
		t.Fatalf("OAuth client secret file = %q", c.OAuth.UpstreamClientSecretFile)
	}
}

func TestOAuthSettingsFileRejectsUnknownKeysAndAdditionalDocuments(t *testing.T) {
	for name, oauth := range map[string]string{
		"unknown key": `upstream_client_id: client
upstream_client_secret_file: /etc/agentplane-mcp/client-secret
base_url: https://edge.example
integration_app_url: https://app.example
upstream_issuer: https://operator.example/issuer
upstream_subject: subject
approving_operator:
  issuer: https://operator.example
  subject: operator
unknown: true
`,
		"second document": `upstream_client_id: client
upstream_client_secret_file: /etc/agentplane-mcp/client-secret
base_url: https://edge.example
integration_app_url: https://app.example
upstream_issuer: https://operator.example/issuer
upstream_subject: subject
approving_operator:
  issuer: https://operator.example
  subject: operator
---
upstream_subject: another
`,
	} {
		t.Run(name, func(t *testing.T) {
			oauthPath := filepath.Join(t.TempDir(), "oauth.yaml")
			if err := os.WriteFile(oauthPath, []byte(oauth), 0o600); err != nil {
				t.Fatal(err)
			}
			settingsFile(t, testSettings+fmt.Sprintf("oauth_file: %q\n", oauthPath))
			if _, err := loadConfig(); err == nil {
				t.Fatal("accepted an invalid provider-owned OAuth settings file")
			}
		})
	}
}

func TestOAuthSettingsFileMustExist(t *testing.T) {
	settingsFile(t, testSettings+fmt.Sprintf("oauth_file: %q\n", filepath.Join(t.TempDir(), "missing.yaml")))
	if _, err := loadConfig(); err == nil {
		t.Fatal("accepted an unreadable provider-owned OAuth settings file")
	}
}

func TestOAuthFileAndInlineSettingsAreMutuallyExclusive(t *testing.T) {
	oauthPath := filepath.Join(t.TempDir(), "oauth.yaml")
	if err := os.WriteFile(oauthPath, []byte("upstream_client_id: client\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	settings := testSettings + fmt.Sprintf("oauth_file: %q\n", oauthPath) + `oauth:
  upstream_client_id: client
  upstream_client_secret_file: /etc/agentplane-mcp/client-secret
  base_url: https://edge.example
  integration_app_url: https://app.example
  upstream_issuer: https://operator.example/issuer
  upstream_subject: SubjectCaseSensitive
  approving_operator:
    issuer: https://operator.example
    subject: OperatorCaseSensitive
`
	settingsFile(t, settings)
	if _, err := loadConfig(); err == nil {
		t.Fatal("accepted inline and provider-owned OAuth settings together")
	}
}

func TestOAuthRequiresProjectedActionToken(t *testing.T) {
	oauthPath := filepath.Join(t.TempDir(), "oauth.yaml")
	oauth := `upstream_client_id: client
upstream_client_secret_file: /etc/agentplane-mcp/client-secret
base_url: https://edge.example
integration_app_url: https://app.example
upstream_issuer: https://operator.example/issuer
upstream_subject: subject
approving_operator:
  issuer: https://operator.example
  subject: operator
`
	if err := os.WriteFile(oauthPath, []byte(oauth), 0o600); err != nil {
		t.Fatal(err)
	}
	settings := strings.Replace(testSettings, "actions_token_file: /run/agentplane/action-token/token\n", "", 1) +
		fmt.Sprintf("oauth_file: %q\n", oauthPath)
	settingsFile(t, settings)
	if _, err := loadConfig(); err == nil {
		t.Fatal("accepted OAuth without the projected Action Service token path")
	}
}

func TestPolicyNamespaceIsRequiredAndValidated(t *testing.T) {
	for name, contents := range map[string]string{
		"missing":           strings.Replace(testSettings, "policy_namespace: test-policy\n", "", 1),
		"invalid namespace": strings.Replace(testSettings, "test-policy", "Test_Policy", 1),
	} {
		t.Run(name, func(t *testing.T) {
			settingsFile(t, contents)
			if c, err := loadConfig(); err == nil {
				t.Fatalf("accepted policy namespace in %+v", c)
			}
		})
	}
}
