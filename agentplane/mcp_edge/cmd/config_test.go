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
rpc_tls:
  listen: 0.0.0.0:8443
  cert_file: /etc/agentplane/tls/tls.crt
  key_file: /etc/agentplane/tls/tls.key
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

// testOAuthSettings is a complete provider-owned OAuth settings file. The identity values keep their case.
const testOAuthSettings = `upstream_client_id: client
upstream_client_secret_file: /etc/agentplane-mcp/client-secret
base_url: https://edge.example
integration_app_url: https://app.example
upstream_issuer: https://operator.example/issuer
upstream_subject: SubjectCaseSensitive
approving_operator:
  issuer: https://operator.example
  subject: OperatorCaseSensitive
`

// oauthFileSetting writes contents as the OAuth settings file and returns the settings line that names it.
func oauthFileSetting(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "oauth.yaml")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return fmt.Sprintf("oauth_file: %q\n", path)
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

func TestOperatorServicesRequireAnAdditionalMutualTLSEndpoint(t *testing.T) {
	baseSettings := `actions_target: actions:8080
policy_namespace: test-policy
actions_token_file: /run/agentplane/action-token/token
operator_oidc:
  issuer: https://operator.example
  audience: edge
  jwks_uri: https://operator.example/jwks
`
	validTLS := `rpc_tls:
  listen: 0.0.0.0:8082
  cert_file: /etc/agentplane/tls/tls.crt
  key_file: /etc/agentplane/tls/tls.key
  client_ca_file: /etc/agentplane/tls/client-ca.crt
operator_services:
  - spiffe://agentplane/app
`
	t.Run("allowlisted service", func(t *testing.T) {
		settingsFile(t, baseSettings+validTLS)
		c, err := loadConfig()
		if err != nil {
			t.Fatal(err)
		}
		if c.RPCTLS == nil || c.RPCTLS.ClientCAFile == "" || len(c.OperatorServices) != 1 || c.OperatorServices[0] != "spiffe://agentplane/app" {
			t.Fatalf("operator TLS settings = %+v / %v", c.RPCTLS, c.OperatorServices)
		}
	})
	for name, settings := range map[string]string{
		"no TLS listener": baseSettings + "operator_services: [spiffe://agentplane/app]\n",
		"no client CA": baseSettings + `rpc_tls:
  listen: 0.0.0.0:8082
  cert_file: /etc/agentplane/tls/tls.crt
  key_file: /etc/agentplane/tls/tls.key
operator_services: [spiffe://agentplane/app]
`,
		"unsupported service": baseSettings + `rpc_tls:
  listen: 0.0.0.0:8082
  cert_file: /etc/agentplane/tls/tls.crt
  key_file: /etc/agentplane/tls/tls.key
  client_ca_file: /etc/agentplane/tls/client-ca.crt
operator_services: [spiffe://agentplane/not-a-service]
`,
		"duplicate service": baseSettings + validTLS + "  - spiffe://agentplane/app\n",
		"client CA without allowlist": baseSettings + `rpc_tls:
  listen: 0.0.0.0:8082
  cert_file: /etc/agentplane/tls/tls.crt
  key_file: /etc/agentplane/tls/tls.key
  client_ca_file: /etc/agentplane/tls/client-ca.crt
`,
	} {
		t.Run(name, func(t *testing.T) {
			settingsFile(t, settings)
			if _, err := loadConfig(); err == nil {
				t.Fatal("accepted invalid operator TLS settings")
			}
		})
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
	t.Run("removed plaintext gRPC port", func(t *testing.T) {
		settingsFile(t, testSettings+"port: 8080\n")
		if _, err := loadConfig(); err == nil {
			t.Fatal("accepted the removed plaintext gRPC port setting")
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
	if c.Host != "0.0.0.0" || c.HTTPPort != 8081 || c.ActionsTarget != "actions:8080" || c.OAuth != nil {
		t.Fatalf("static settings were changed by environment variables: %+v", c)
	}
}

func TestOAuthSettingsComeFromFileAndValidateWithProjectedToken(t *testing.T) {
	settingsFile(t, testSettings+oauthFileSetting(t, testOAuthSettings))
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
		"unknown key":     testOAuthSettings + "unknown: true\n",
		"second document": testOAuthSettings + "---\nupstream_subject: another\n",
	} {
		t.Run(name, func(t *testing.T) {
			settingsFile(t, testSettings+oauthFileSetting(t, oauth))
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
	// Both forms are complete, so only the exclusion can reject them.
	inline := "oauth:\n  " + strings.ReplaceAll(strings.TrimSuffix(testOAuthSettings, "\n"), "\n", "\n  ") + "\n"
	settingsFile(t, testSettings+oauthFileSetting(t, testOAuthSettings)+inline)
	if _, err := loadConfig(); err == nil {
		t.Fatal("accepted inline and provider-owned OAuth settings together")
	}
}

func TestOAuthRequiresExactlyOneActionCallerCredential(t *testing.T) {
	base := strings.Replace(testSettings, "actions_token_file: /run/agentplane/action-token/token\n", "", 1)
	oauthFile := oauthFileSetting(t, testOAuthSettings)
	serverTLS := `actions_tls:
  ca_file: /etc/agentplane/ca/ca.crt
  server_uri: spiffe://agentplane/actions
`
	svid := `actions_tls:
  ca_file: /etc/agentplane/ca/ca.crt
  server_uri: spiffe://agentplane/actions
  cert_file: /var/run/secrets/agentplane-mcp/tls.crt
  key_file: /var/run/secrets/agentplane-mcp/tls.key
  client_uri: spiffe://agentplane/mcp-edge
`

	t.Run("service SVID with typed delegation", func(t *testing.T) {
		settingsFile(t, base+svid+oauthFile)
		if _, err := loadConfig(); err != nil {
			t.Fatalf("accepted exact MCP edge SVID configuration: %v", err)
		}
	})
	t.Run("server TLS with projected bearer", func(t *testing.T) {
		settingsFile(t, testSettings+serverTLS+oauthFile)
		if _, err := loadConfig(); err != nil {
			t.Fatalf("accepted verified server TLS with the projected token: %v", err)
		}
	})
	for name, settings := range map[string]string{
		"missing caller credential":               base + oauthFile,
		"server TLS without caller credential":    base + serverTLS + oauthFile,
		"mixed token and service SVID":            testSettings + svid + oauthFile,
		"wrong service identity cannot fall back": base + strings.Replace(svid, "spiffe://agentplane/mcp-edge", "spiffe://agentplane/app", 1) + oauthFile,
	} {
		t.Run(name, func(t *testing.T) {
			settingsFile(t, settings)
			if _, err := loadConfig(); err == nil {
				t.Fatal("accepted an invalid Action caller authentication configuration")
			}
		})
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
