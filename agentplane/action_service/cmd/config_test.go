package main

import (
	"crypto/ecdh"
	"crypto/rand"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/catalog"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/linkage"
	"github.com/agentydragon/ducktape/agentplane/action_service/internal/push"
	"github.com/agentydragon/ducktape/agentplane/identity/operator"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/testing/configtest"
)

const databaseURLEnv = settingPrefix + "DATABASE_URL=postgres://test-user:never-print-this@test-database/actions"

// requiredFile is a deployment's settings file with every setting that has no default.
const requiredRPC = `rpc:
  listen_address: "0.0.0.0:8080"
  tls:
    cert_file: /test/actions.crt
    key_file: /test/actions.key
  auth:
    - mode: spiffe
      ca_file: /test/workload-ca.crt
`

const requiredFile = requiredRPC + `policy_namespace: test-agentplane
caller_service_account_namespaces: [test-agentplane]
`

func loadTestConfig(t *testing.T, file string, environ ...string) (config, error) {
	t.Helper()
	return configtest.Load(t, settingPrefix, loadConfig, append([]string{
		settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, file),
		databaseURLEnv,
	}, environ...))
}

func TestWhatADeploymentLeavesUnsetTakesItsDefault(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile)
	if err != nil {
		t.Fatal(err)
	}
	if c.DatabaseURL == "" || c.RPC.ListenAddress != "0.0.0.0:8080" || c.operatorsEnabled() {
		t.Errorf("settings: %+v", c)
	}
}

func TestSessionDelegationServiceIsOptionalAndNamesOnlyTheCentralSampler(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile)
	if err != nil {
		t.Fatal(err)
	}
	if c.SessionDelegationService != "" {
		t.Fatalf("unset session_delegation_service = %q", c.SessionDelegationService)
	}

	withSPIFFE := requiredFile
	c, err = loadTestConfig(t, withSPIFFE+"session_delegation_service: "+peertls.CentralSamplerURI+"\n")
	if err != nil {
		t.Fatal(err)
	}
	if c.SessionDelegationService != peertls.CentralSamplerURI {
		t.Fatalf("session_delegation_service = %q", c.SessionDelegationService)
	}

	for name, file := range map[string]string{
		"requires SPIFFE listener": strings.Replace(requiredFile, "    - mode: spiffe\n      ca_file: /test/workload-ca.crt\n", "", 1) + "session_delegation_service: " + peertls.CentralSamplerURI + "\n",
		"another valid service":    withSPIFFE + "session_delegation_service: " + peertls.MCPEdgeURI + "\n",
		"malformed service":        withSPIFFE + "session_delegation_service: not-a-service\n",
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := loadTestConfig(t, file); err == nil {
				t.Fatal("accepted")
			}
		})
	}
}

func TestGenericEnvironmentVariablesDoNotOverrideTheFile(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile, settingPrefix+"RPC__LISTEN_ADDRESS=127.0.0.1:9001")
	if err != nil {
		t.Fatal(err)
	}
	if c.RPC.ListenAddress != "0.0.0.0:8080" {
		t.Errorf("environment overrode static file settings: %+v", c)
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

func TestOperatorIssuerIsRead(t *testing.T) {
	c, err := loadTestConfig(t, requiredFile+`operator_oidc:
  issuer: https://issuer.test.example/
  jwks_uri: https://issuer.test.example/jwks/
  audience: test-actions-audience
`)
	if err != nil {
		t.Fatal(err)
	}
	wantOperators := operator.Settings{
		Issuer: "https://issuer.test.example/", Audience: "test-actions-audience", JWKSURI: "https://issuer.test.example/jwks/",
		TokenProfile: operator.ProfileAuthentik,
	}
	if c.OperatorOIDC != wantOperators || !c.operatorsEnabled() {
		t.Errorf("settings: %+v", c)
	}
}

func TestThePolicySettingsAreRead(t *testing.T) {
	c, err := loadTestConfig(t, requiredRPC+`policy_namespace: test-policies
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
    client_id: test-client
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
`, settingPrefix+"MCP_SERVERS__TEST_REGISTERED__CLIENT_ID=ignored-environment-client")
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
		"unknown key":                    requiredFile + "test_unknown_setting: never-print-this\n",
		"unknown listener key":           strings.Replace(requiredFile, "  listen_address: \"0.0.0.0:8080\"", "  unknown: never-print-this", 1),
		"missing listener":               strings.Replace(requiredFile, requiredRPC, "", 1),
		"no caller namespaces":           strings.Replace(requiredFile, "[test-agentplane]", "[]", 1),
		"no policy namespace":            strings.Replace(requiredFile, "policy_namespace: test-agentplane\n", "", 1),
		"database Secret in static file": requiredFile + "database_url: never-print-this\n",
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
		"action group field of another type": requiredFile + strings.Replace(testActionGroups, "available: yes", "available: never-print-this", 1),
		"unknown action group field":         requiredFile + strings.Replace(testActionGroups, "available: yes", "health: never-print-this", 1),
		"executor kind":                      requiredFile + strings.Replace(testActionGroups, "kind: mcp", "kind: never-print-this", 1),
		"executor field of another kind":     requiredFile + strings.Replace(testActionGroups, "kind: mcp", "kind: mcp\n      namespace: never-print-this", 1),
		"web push without its key":           requiredFile + testWebPush,
		"web push key in static file":        requiredFile + testWebPush + "  private_key_pem: never-print-this\n",
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
		if _, err := loadTestConfig(t, requiredFile+testActionGroups, settingPrefix+variable); err != nil {
			t.Errorf("unused environment override affected the file: %v", err)
		}
	}
}

func TestInvalidDirectWaitsAreRefused(t *testing.T) {
	for _, file := range []string{
		"direct_wait_seconds: -1\n",
		"direct_wait_seconds: .inf\n",
		"direct_wait_seconds: .nan\n",
	} {
		if _, err := loadTestConfig(t, requiredFile+file); err == nil {
			t.Fatalf("accepted %s", file)
		}
	}
}

func TestTheOrchestrationPeersAreSetExactlyWhenAnOrchestrationGroupCallsThem(t *testing.T) {
	const orchestration = `action_groups:
  test-orchestration:
    title: Test orchestration
    description: test group description
    executor: {kind: orchestration_v1, description: test executor, namespace: test-agentplane}
`
	const dispatcher = `session_dispatcher:
  target: test-dispatcher:8080
  token_file: /test/dispatcher-token
  request_timeout_s: 15
`
	const history = `history_service:
  target: test-history:8080
  token_file: /test/history-token
  request_timeout_s: 10
`
	c, err := loadTestConfig(t, requiredFile+orchestration+dispatcher+history)
	if err != nil {
		t.Fatal(err)
	}
	if want := (peer{Target: "test-dispatcher:8080", TokenFile: "/test/dispatcher-token", RequestTimeoutSeconds: 15}); c.SessionDispatcher == nil || *c.SessionDispatcher != want {
		t.Errorf("session_dispatcher = %+v, want %+v", c.SessionDispatcher, want)
	}
	if want := (peer{Target: "test-history:8080", TokenFile: "/test/history-token", RequestTimeoutSeconds: 10}); c.HistoryService == nil || *c.HistoryService != want {
		t.Errorf("history_service = %+v, want %+v", c.HistoryService, want)
	}
	for name, file := range map[string]string{
		"an orchestration group without the Dispatcher": orchestration + history,
		"an orchestration group without History":        orchestration + dispatcher,
		"the peers without an orchestration group":      dispatcher + history,
		"a peer without a timeout":                      orchestration + dispatcher + strings.Replace(history, "  request_timeout_s: 10\n", "", 1),
		"a group launching in another namespace":        strings.Replace(orchestration, "namespace: test-agentplane", "namespace: test-other", 1) + dispatcher + history,
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := loadTestConfig(t, requiredFile+file); err == nil {
				t.Error("accepted")
			}
		})
	}
}

func TestSessionDelegationPeersAreSetExactlyWhenADelegationGroupCallsThem(t *testing.T) {
	const group = `action_groups:
  environments:
    title: Environments
    description: same-principal environments
    executor: {kind: session_delegation, description: forwards the caller}
`
	const dispatcher = `session_delegation_dispatcher:
  target: test-dispatcher:8080
  token_file: /test/delegated-dispatcher-token
  request_timeout_s: 40
`
	const history = `session_delegation_history:
  target: test-history:8080
  token_file: /test/delegated-history-token
  request_timeout_s: 40
`
	c, err := loadTestConfig(t, requiredFile+group+dispatcher+history)
	if err != nil {
		t.Fatal(err)
	}
	if c.SessionDelegationDispatcher == nil || c.SessionDelegationDispatcher.Target != "test-dispatcher:8080" ||
		c.SessionDelegationHistory == nil || c.SessionDelegationHistory.Target != "test-history:8080" {
		t.Fatalf("delegation peers: dispatcher=%+v history=%+v", c.SessionDelegationDispatcher, c.SessionDelegationHistory)
	}
	for name, file := range map[string]string{
		"delegation group missing the shared dispatcher peer": group + history,
		"delegation group missing History":                    group + dispatcher,
		"delegation peers without a delegation group":         dispatcher + history,
		"delegate peer without a token file":                  group + strings.Replace(dispatcher, "  token_file: /test/delegated-dispatcher-token\n", "", 1) + history,
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := loadTestConfig(t, requiredFile+file); err == nil {
				t.Fatal("accepted")
			}
		})
	}
}

func TestEnvironmentServicePeerIsOptionalAndPinsControllerSVID(t *testing.T) {
	if c, err := loadTestConfig(t, requiredFile); err != nil || c.EnvironmentService != nil {
		t.Fatalf("omitted environment_service = %+v, error = %v", c.EnvironmentService, err)
	}

	const environment = `environment_service:
  target: sandbox-controller.agentplane.svc:8082
  request_timeout_s: 12
  tls:
    ca_file: /trust/ca.pem
    server_uri: spiffe://agentplane/sandbox-controller
    cert_file: /svid/actions.crt
    key_file: /svid/actions.key
    client_uri: spiffe://agentplane/actions
`
	c, err := loadTestConfig(t, requiredFile+environment)
	if err != nil {
		t.Fatal(err)
	}
	if c.EnvironmentService == nil || c.EnvironmentService.Target != "sandbox-controller.agentplane.svc:8082" ||
		c.EnvironmentService.RequestTimeoutSeconds != 12 || c.EnvironmentService.TLS.ServerURI != peertls.SandboxControllerURI ||
		c.EnvironmentService.TLS.ClientURI != peertls.ActionsURI {
		t.Fatalf("environment_service = %+v", c.EnvironmentService)
	}

	for name, invalid := range map[string]string{
		"missing target":    strings.Replace(environment, "  target: sandbox-controller.agentplane.svc:8082\n", "", 1),
		"missing timeout":   strings.Replace(environment, "  request_timeout_s: 12\n", "", 1),
		"missing SVID":      strings.Replace(environment, "  tls:\n    ca_file: /trust/ca.pem\n    server_uri: spiffe://agentplane/sandbox-controller\n    cert_file: /svid/actions.crt\n    key_file: /svid/actions.key\n    client_uri: spiffe://agentplane/actions\n", "", 1),
		"wrong server URI":  strings.Replace(environment, peertls.SandboxControllerURI, peertls.SessionDispatcherURI, 1),
		"wrong caller SVID": strings.Replace(environment, peertls.ActionsURI, peertls.AppURI, 1),
		"partial SVID":      strings.Replace(environment, "    key_file: /svid/actions.key\n", "", 1),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := loadTestConfig(t, requiredFile+invalid); err == nil {
				t.Fatal("accepted invalid environment_service configuration")
			}
		})
	}
}

func TestListenerRequiresExplicitAddressAndAuthentication(t *testing.T) {
	for _, fragment := range []string{
		"rpc: {auth: [{mode: kubernetes_bearer, audiences: [test-actions]}]}\n",
		"rpc: {listen_address: ':8080'}\n",
		"rpc: {listen_address: ':8080', auth: [{mode: spiffe, ca_file: /trust/ca.crt}]}\n",
	} {
		if _, err := loadTestConfig(t, strings.Replace(requiredFile, requiredRPC, fragment, 1)); err == nil {
			t.Fatalf("accepted invalid RPC listener: %s", fragment)
		}
	}
}

func TestSettingsFileIsRequiredAndContainsOneDocument(t *testing.T) {
	for _, path := range []string{"", "/not-a-settings-file"} {
		_, err := configtest.Load(t, settingPrefix, loadConfig, []string{databaseURLEnv, settingPrefix + "CONFIG_FILE=" + path})
		if err == nil {
			t.Fatalf("accepted config file %q", path)
		}
	}
	for _, trailingDocument := range []string{"policy_namespace: replacement\n", "]\n"} {
		_, err := loadTestConfig(t, requiredFile+"---\n"+trailingDocument)
		if err == nil || !strings.Contains(err.Error(), "must contain exactly one YAML document") {
			t.Errorf("expected single-document error for %q, got %v", trailingDocument, err)
		}
	}
}

func TestMCPClientIDCanComeFromItsProjectedSecretFile(t *testing.T) {
	clientID := configtest.WriteSettings(t, "registered-client\n")
	file := requiredFile + "mcp_servers:\n  github:\n    server_id: github\n    server_url: https://mcp.example/mcp\n    redirect_uri: https://app.example/callback\n    client_id_file: " + clientID + "\n"
	c, err := loadTestConfig(t, file)
	if err != nil || c.MCPServers["github"].ClientID == nil || *c.MCPServers["github"].ClientID != "registered-client" {
		t.Fatalf("projected client ID was not resolved: %v", err)
	}
	for _, invalid := range []string{
		strings.Replace(file, clientID, "/missing-client-id", 1),
		file + "    client_id: conflicting-client\n",
		file + "    use_shared_cimd: true\n",
		strings.Replace(file, clientID, configtest.WriteSettings(t, ""), 1),
	} {
		if _, err := loadTestConfig(t, invalid); err == nil {
			t.Fatal("accepted invalid projected client ID configuration")
		}
	}
}

func TestActionsPeersAcceptOwnSVIDInsteadOfToken(t *testing.T) {
	const group = `action_groups:
  environments:
    title: Environments
    description: same-principal environments
    executor: {kind: session_delegation, description: forwards the caller}
`
	const peer = `
  target: peer:8443
  request_timeout_s: 10
  tls:
    ca_file: /trust/ca.pem
    server_uri: spiffe://agentplane/session-dispatcher
    cert_file: /svid/tls.crt
    key_file: /svid/tls.key
    client_uri: spiffe://agentplane/actions
`
	source := requiredFile + group + "session_delegation_dispatcher:" + peer + "session_delegation_history:" + peer
	if _, err := loadTestConfig(t, source); err != nil {
		t.Fatal(err)
	}
	for name, invalid := range map[string]string{
		"mixed":      strings.Replace(source, "  target: peer:8443", "  token_file: /token\n  target: peer:8443", 1),
		"wrong role": strings.Replace(source, "client_uri: spiffe://agentplane/actions", "client_uri: spiffe://agentplane/app", 1),
		"partial":    strings.Replace(source, "    key_file: /svid/tls.key\n", "", 1),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := loadTestConfig(t, invalid); err == nil {
				t.Fatal("accepted invalid caller authentication")
			}
		})
	}
	// Orchestration peers share the same validation and service role.
	source = strings.ReplaceAll(source, "session_delegation_dispatcher:", "session_dispatcher:")
	source = strings.ReplaceAll(source, "session_delegation_history:", "history_service:")
	source = strings.Replace(source, "kind: session_delegation, description: forwards the caller", "kind: orchestration_v1, description: test executor, namespace: test-agentplane", 1)
	if _, err := loadTestConfig(t, source); err != nil {
		t.Fatal(err)
	}
}

func TestPolicyServiceMustBeACompleteMutualTLSCallerAsActions(t *testing.T) {
	block := func(target, clientURI, serverURI string) string {
		return "policy_service:\n  target: \"" + target + "\"\n  tls:\n    ca_file: /test/workload-ca.crt\n    server_uri: " + serverURI +
			"\n    cert_file: /test/actions.crt\n    key_file: /test/actions.key\n    client_uri: " + clientURI + "\n"
	}
	actions, policy := peertls.ActionsURI, peertls.PolicyControllerURI
	for _, tc := range []struct {
		name     string
		settings string
		valid    bool
	}{
		{"complete", block("test-policy-controller:8443", actions, policy), true},
		{"no target", block("", actions, policy), false},
		{"another caller identity", block("test-policy-controller:8443", peertls.MCPEdgeURI, policy), false},
		{"another server", block("test-policy-controller:8443", actions, peertls.ActionsURI), false},
		{"no client SVID", "policy_service:\n  target: test-policy-controller:8443\n  tls:\n    ca_file: /test/workload-ca.crt\n    server_uri: " + policy + "\n", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c, err := loadTestConfig(t, requiredFile+tc.settings)
			if tc.valid && (err != nil || c.PolicyService == nil || c.PolicyService.Target != "test-policy-controller:8443") {
				t.Fatalf("loadConfig = %+v, %v", c.PolicyService, err)
			}
			if !tc.valid && err == nil {
				t.Fatal("loadConfig accepted an incomplete policy_service")
			}
		})
	}
}
