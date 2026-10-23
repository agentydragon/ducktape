package main

import (
	"crypto/ecdh"
	"crypto/rand"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/api"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/push"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
)

const databaseURLEnv = settingPrefix + "DATABASE_URL=postgres://test-user:never-print-this@test-database/actions"

// requiredFile is a deployment's settings file with every setting that has no default.
const requiredFile = `token_audience: test-actions
policy_namespace: test-agentplane
caller_service_account_namespaces: [test-agentplane]
`

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
	c, err := loadTestConfig(t, requiredFile)
	if err != nil {
		t.Fatal(err)
	}
	if c.DatabaseURL == "" || c.Host != "0.0.0.0" || c.Port != 8080 || len(c.ReaderAccounts) != 0 || c.operatorsEnabled() {
		t.Errorf("settings: %+v", c)
	}
}

func TestAnEnvironmentVariableOverridesTheFile(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile+"host: 127.0.0.1\nport: 9000\n", settingPrefix+"PORT=9001")
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
	c, err := loadTestConfig(t, requiredFile+testActionGroups)
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
	c, err := loadTestConfig(t, requiredFile)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.ActionGroups.Resolve("test-group", "test-action"); !errors.Is(err, catalog.ErrUnknownAction) {
		t.Errorf("Resolve = %v", err)
	}
}

func TestReaderAccountsAndTheOperatorIssuerAreRead(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile+`reader_accounts:
  - {namespace: test-agentplane, name: test-notifications}
  - test-agentplane/test-other-reader
operator_oidc:
  issuer: https://issuer.test.example/
  jwks_uri: https://issuer.test.example/jwks/
`, settingPrefix+"OPERATOR_OIDC__AUDIENCE=test-actions-audience")
	if err != nil {
		t.Fatal(err)
	}
	wantReaders := []auth.Account{{Namespace: "test-agentplane", Name: "test-notifications"}, {Namespace: "test-agentplane", Name: "test-other-reader"}}
	wantOperators := api.OperatorOIDCSettings{
		Issuer: "https://issuer.test.example/", Audience: "test-actions-audience", JWKSURI: "https://issuer.test.example/jwks/",
		TokenProfile: api.ProfileAuthentik,
	}
	if !slices.Equal(c.ReaderAccounts, wantReaders) || c.OperatorOIDC != wantOperators || !c.operatorsEnabled() {
		t.Errorf("settings: %+v", c)
	}
}

func TestThePolicySettingsAreRead(t *testing.T) {
	c, err := loadTestConfig(t, `token_audience: test-actions
policy_namespace: test-policies
caller_service_account_namespaces: [test-callers, test-more-callers]
`)
	if err != nil {
		t.Fatal(err)
	}
	if c.PolicyNamespace != "test-policies" || !slices.Equal(c.CallerServiceAccountNamespaces, []string{"test-callers", "test-more-callers"}) {
		t.Errorf("settings: %+v", c)
	}
}

// testWebPush is a deployment's Web Push settings but the private key, a Secret it sets in the environment.
const testWebPush = `web_push:
  subject: mailto:test-vapid@agentplane.test
  public_base_url: https://app.test/
  allowed_push_hosts: [push.test, other-push.test]
`

func TestWebPushIsOffUnlessConfiguredAndReadsItsKeyFromTheEnvironment(t *testing.T) {
	if c, err := loadTestConfig(t, requiredFile); err != nil || c.WebPush.Configured() {
		t.Errorf("Web Push without settings: %+v, %v", c.WebPush, err)
	}
	private, err := ecdh.P256().GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKCS8PrivateKey(private)
	if err != nil {
		t.Fatal(err)
	}
	key := string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}))
	c, err := loadTestConfig(t, requiredFile+testWebPush, settingPrefix+"WEB_PUSH__PRIVATE_KEY_PEM="+key)
	if err != nil {
		t.Fatal(err)
	}
	want := push.Settings{
		PrivateKeyPEM: key, Subject: "mailto:test-vapid@agentplane.test", PublicBaseURL: "https://app.test/",
		AllowedPushHosts: []string{"push.test", "other-push.test"},
	}
	if !reflect.DeepEqual(c.WebPush, want) {
		t.Errorf("Web Push settings: %+v", c.WebPush)
	}
}

func TestTheMCPServersAreRead(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile+`mcp_client_metadata:
  url: https://test-actions.example/oauth/client-metadata.json
  client_name: Test application
mcp_servers:
  test_registered:
    server_id: test_registered
    server_url: https://test-mcp.example/mcp
    client_id: test-placeholder
    client_secret_file: /test/client-secret
    redirect_uri: https://test-app.example/mcp-linkage/callback
    scopes: [test-read]
  test_shared:
    server_id: test_shared
    server_url: https://test-other-mcp.example/mcp
    token_endpoint: https://test-other-mcp.example/token
    use_shared_cimd: true
    redirect_uri: https://test-app.example/mcp-linkage/callback
    scopes: [test-write]
`, settingPrefix+"MCP_SERVERS__TEST_REGISTERED__CLIENT_ID=test-client")
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]linkage.Server{
		"test_registered": {
			ServerID: "test_registered", ServerURL: "https://test-mcp.example/mcp", ClientID: new("test-client"),
			ClientSecretFile: new("/test/client-secret"), RedirectURI: "https://test-app.example/mcp-linkage/callback",
			Scopes: []string{"test-read"},
		},
		"test_shared": {
			ServerID: "test_shared", ServerURL: "https://test-other-mcp.example/mcp",
			TokenEndpoint: new("https://test-other-mcp.example/token"), UseSharedCIMD: true,
			RedirectURI: "https://test-app.example/mcp-linkage/callback", Scopes: []string{"test-write"},
		},
	}
	if !reflect.DeepEqual(c.MCPServers, want) || c.MCPClientMetadata != (linkage.ClientMetadata{
		URL: "https://test-actions.example/oauth/client-metadata.json", ClientName: "Test application",
	}) {
		t.Errorf("MCP settings: %+v, %+v", c.MCPServers, c.MCPClientMetadata)
	}
}

func TestSettingsAreRefusedWithoutEchoingTheirValues(t *testing.T) {
	for name, file := range map[string]string{
		"unknown key":          requiredFile + "test_unknown_setting: never-print-this\n",
		"port of a word":       requiredFile + "port: never-print-this\n",
		"port out of range":    requiredFile + "port: 70000\n",
		"empty host":           requiredFile + "host: \"\"\n",
		"no audience":          strings.Replace(requiredFile, "token_audience: test-actions\n", "", 1),
		"no caller namespaces": strings.Replace(requiredFile, "[test-agentplane]", "[]", 1),
		"no policy namespace":  strings.Replace(requiredFile, "policy_namespace: test-agentplane\n", "", 1),
		"reader of a word":     requiredFile + "reader_accounts: [never-print-this]\n",
		"operator issuer without its audience": requiredFile + `operator_oidc:
  issuer: https://never-print-this.test.example/
  jwks_uri: https://issuer.test.example/jwks/
`,
		"operator keys over plain http": requiredFile + `operator_oidc:
  issuer: https://issuer.test.example/
  audience: test-actions-audience
  jwks_uri: http://never-print-this.test.example/jwks/
`,
		"unknown token profile": requiredFile + `operator_oidc:
  issuer: https://issuer.test.example/
  audience: test-actions-audience
  jwks_uri: https://issuer.test.example/jwks/
  token_profile: never-print-this
`,
		"action group":                       requiredFile + strings.Replace(testActionGroups, "server_id: test-group", "server_id: never-print-this", 1),
		"action groups key":                  requiredFile + strings.Replace(testActionGroups, "X-Test-Header: test", "1: never-print-this", 1),
		"action group field of another type": requiredFile + strings.Replace(testActionGroups, "available: yes", "available: never-print-this", 1),
		"unknown action group field":         requiredFile + strings.Replace(testActionGroups, "available: yes", "health: never-print-this", 1),
		"web push without its key":           requiredFile + testWebPush,
		"web push key not PEM":               requiredFile + testWebPush + "  private_key_pem: never-print-this\n",
		"unknown web push key":               requiredFile + testWebPush + "  vapid_claims: never-print-this\n",
		"MCP server without a client": requiredFile + `mcp_servers:
  test_server:
    server_id: test_server
    server_url: https://never-print-this.example/mcp
    redirect_uri: https://test-app.example/mcp-linkage/callback
`,
		"unknown MCP server field": requiredFile + `mcp_servers:
  test_server:
    server_id: test_server
    server_url: https://test-mcp.example/mcp
    client_id: test-client
    redirect_uri: https://test-app.example/mcp-linkage/callback
    client_secret: never-print-this
`,
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
	if _, err := loadTestConfig(t, requiredFile, settingPrefix+"DATABASE_URL="); err == nil {
		t.Error("accepted without a database URL")
	}
	for _, variable := range []string{"ACTION_GROUPS={}", "ACTION_GROUPS__TEST_GROUP__AVAILABLE=false"} {
		if _, err := loadTestConfig(t, requiredFile+testActionGroups, settingPrefix+variable); err == nil {
			t.Errorf("accepted %s, which only the file sets", variable)
		}
	}
}

const testOAuth = `{"config_url":"https://issuer.example/.well-known/openid-configuration","upstream_client_id":"test-client","upstream_client_secret_file":"/secret/client","base_url":"https://actions.example","integration_app_url":"https://app.example","jwt_signing_key_file":"/secret/jwt","encryption_key_file":"/secret/encryption","upstream_issuer":"https://issuer.example","upstream_subject":"test-upstream-subject","approving_operator":{"issuer":"https://operator.example","subject":"test-operator"}}`

func TestOAuthSecretJSONAndNestedEnvironmentOverrides(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile, settingPrefix+"OAUTH="+testOAuth, settingPrefix+"OAUTH__UPSTREAM_CLIENT_ID=test-override")
	if err != nil {
		t.Fatal(err)
	}
	if c.OAuth == nil || c.OAuth.UpstreamClientID != "test-override" || c.OAuth.ApprovingOperator.Subject != "test-operator" {
		t.Fatal("OAuth Secret or its override was not read")
	}
	for _, raw := range []string{"never-print-this", "null", strings.Replace(testOAuth, `"upstream_client_id":"test-client"`, `"unknown":"never-print-this"`, 1)} {
		_, err := loadTestConfig(t, requiredFile, settingPrefix+"OAUTH="+raw)
		if err == nil || strings.Contains(err.Error(), "never-print-this") {
			t.Fatalf("invalid OAuth Secret: %v", err)
		}
	}
}

func TestInvalidMCPWaitsAreRefused(t *testing.T) {
	for _, file := range []string{"max_wait_seconds: -1\n", "direct_wait_seconds: .inf\n", "max_wait_seconds: .nan\n"} {
		if _, err := loadTestConfig(t, requiredFile+file); err == nil {
			t.Fatalf("accepted %s", file)
		}
	}
}

func TestTheDeployedReaderAccountsJSONEnvironmentIsRead(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile, settingPrefix+`READER_ACCOUNTS=[{"namespace":"test-agentplane","name":"test-notifications"}]`)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(c.ReaderAccounts, []auth.Account{{Namespace: "test-agentplane", Name: "test-notifications"}}) {
		t.Fatalf("readers: %v", c.ReaderAccounts)
	}
	for _, raw := range []string{`[{"namespace":"test"}]`, `[{"namespace":"test","name":"reader","unexpected":"never-print-this"}]`, `[] []`, `null`} {
		_, err := loadTestConfig(t, requiredFile, settingPrefix+"READER_ACCOUNTS="+raw)
		if err == nil || strings.Contains(err.Error(), "never-print-this") {
			t.Fatalf("invalid readers: %v", err)
		}
	}
}
