package main

import (
	"net/netip"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/testing/configtest"
)

const databaseURL = "postgresql://user:never-print-this@db/egress"

const forwardSettings = `forward:
  listen_address: 127.0.0.1:5555
  tls:
    cert_file: /svid/tls.crt
    key_file: /svid/tls.key
  auth:
    - mode: spiffe
      ca_file: /svid/ca.crt
`

const validSettings = `rules_namespace: file-rules
credentials_namespace: file-credentials
allowed_service_account_namespaces: [file-namespace]
` + forwardSettings + `admin_listen_address: 127.0.0.1:1111
ca_cert: /ca.crt
ca_key: /ca.key
log_levels:
  root: INFO
exempt_networks: [127.0.0.0/8]
`

func environment(t *testing.T, contents string, extra ...string) []string {
	t.Helper()
	return append([]string{
		configFileEnv + "=" + configtest.WriteSettings(t, contents),
		databaseURLEnv + "=" + databaseURL,
	}, extra...)
}

func required(t *testing.T, extra ...string) []string {
	t.Helper()
	return environment(t, validSettings, extra...)
}

func withForward(contents, forward string) string {
	return strings.Replace(contents, forwardSettings, forward, 1)
}

func TestStaticSettingsComeOnlyFromFileAndDatabaseURLIsExplicit(t *testing.T) {
	environ := required(
		t,
		"AGENTPLANE_EGRESS_RULES_NAMESPACE=environment-rules",
		"AGENTPLANE_EGRESS_ADMIN_LISTEN_ADDRESS=127.0.0.1:9999",
		"AGENTPLANE_EGRESS_DECISION_BATCH_SIZE=0",
	)
	c, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, environ)
	if err != nil {
		t.Fatal(err)
	}
	if c.RulesNamespace != "file-rules" || c.AdminListenAddress != "127.0.0.1:1111" ||
		c.DecisionBatchSize != 100 || c.DatabaseURL != databaseURL ||
		!slices.Equal(c.AllowedServiceAccountNamespaces, []string{"file-namespace"}) ||
		!slices.Equal(c.ExemptNetworks, []netip.Prefix{netip.MustParsePrefix("127.0.0.0/8")}) {
		t.Fatalf("unexpected config: %+v", c)
	}
}

func TestConfigFileIsRequiredReadableAndSingleDocument(t *testing.T) {
	for name, environ := range map[string][]string{
		"missing": {databaseURLEnv + "=" + databaseURL},
		"blank":   {configFileEnv + "= ", databaseURLEnv + "=" + databaseURL},
		"unreadable": {
			configFileEnv + "=/missing/egress-settings.yaml",
			databaseURLEnv + "=" + databaseURL,
		},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, environ); err == nil {
				t.Fatal("loaded without a readable settings file")
			}
		})
	}
	if _, err := configtest.Load(
		t,
		"AGENTPLANE_EGRESS_",
		loadConfig,
		environment(t, validSettings+"---\nrules_namespace: second\n"),
	); err == nil {
		t.Fatal("accepted multiple YAML documents")
	}
}

func TestUnknownFileKeysAreRefusedAtEverySettingsLevel(t *testing.T) {
	unknown := []string{
		validSettings + "ignored: value\n",
		withForward(validSettings, strings.Replace(forwardSettings, "forward:\n", "forward:\n  ignored: value\n", 1)),
		withForward(validSettings, strings.Replace(forwardSettings, "    key_file: /svid/tls.key\n", "    key_file: /svid/tls.key\n    ignored: value\n", 1)),
		withForward(validSettings, strings.Replace(forwardSettings, "      ca_file: /svid/ca.crt\n", "      ca_file: /svid/ca.crt\n      ignored: value\n", 1)),
	}
	for i, contents := range unknown {
		t.Run(string(rune('a'+i)), func(t *testing.T) {
			if _, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, environment(t, contents)); err == nil {
				t.Fatal("accepted unknown setting")
			}
		})
	}
}

func TestDatabaseURLComesOnlyFromTheExplicitSecretEnvironment(t *testing.T) {
	c, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, required(t))
	if err != nil {
		t.Fatal(err)
	}
	if c.DatabaseURL != databaseURL {
		t.Fatalf("database URL = %q, want the explicit runtime Secret input", c.DatabaseURL)
	}
	contents := strings.Replace(validSettings, "log_levels:\n", "database_url: "+databaseURL+"\nlog_levels:\n", 1)
	if _, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, environment(t, contents)); err == nil {
		t.Fatal("accepted database_url from the settings file")
	}
	if _, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, environment(t, validSettings, databaseURLEnv+"=")); err == nil {
		t.Fatal("started without the explicit database Secret input")
	}
}

func TestErrorsNeverCarryTheDatabaseURL(t *testing.T) {
	invalid := []string{
		strings.Replace(validSettings, "log_levels:\n", "decision_batch_size: 0\nlog_levels:\n", 1),
		validSettings + "listen_port: 8888\n",
		strings.Replace(validSettings, "root: INFO", "agentplane.other: INFO", 1),
		strings.Replace(validSettings, "127.0.0.0/8", "not-a-prefix", 1),
	}
	for i, contents := range invalid {
		_, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, environment(t, contents))
		if err == nil {
			t.Fatalf("invalid configuration %d was accepted", i)
		}
		if strings.Contains(err.Error(), "never-print-this") {
			t.Fatalf("error carries the database URL: %v", err)
		}
	}
}

func TestRequiredStaticSettingsMustBePresent(t *testing.T) {
	missing := map[string]string{
		"rules_namespace":                    strings.Replace(validSettings, "rules_namespace: file-rules\n", "", 1),
		"credentials_namespace":              strings.Replace(validSettings, "credentials_namespace: file-credentials\n", "", 1),
		"allowed_service_account_namespaces": strings.Replace(validSettings, "allowed_service_account_namespaces: [file-namespace]\n", "", 1),
		"forward":                            withForward(validSettings, ""),
		"ca_cert":                            strings.Replace(validSettings, "ca_cert: /ca.crt\n", "", 1),
		"ca_key":                             strings.Replace(validSettings, "ca_key: /ca.key\n", "", 1),
	}
	for name, contents := range missing {
		t.Run(name, func(t *testing.T) {
			if _, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, environment(t, contents)); err == nil {
				t.Fatal("started without a required static setting")
			}
		})
	}
}

func TestAllowedServiceAccountNamespacesMustBeUsable(t *testing.T) {
	for name, namespaces := range map[string]string{
		"empty list": `[]`, "empty entry": `[file-namespace, ""]`,
	} {
		t.Run(name, func(t *testing.T) {
			contents := strings.Replace(validSettings, "[file-namespace]", namespaces, 1)
			if _, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, environment(t, contents)); err == nil {
				t.Fatal("ran with no usable workload namespace")
			}
		})
	}
}

func TestAdminGRPCIsOffUnlessItsSettingsAreSet(t *testing.T) {
	c, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, required(t))
	if err != nil {
		t.Fatal(err)
	}
	if c.adminGRPCEnabled() {
		t.Fatal("admin gRPC enabled without being configured")
	}
}

func TestAdminGRPCSettingsComeFromTheFileAndAreAllOrNothing(t *testing.T) {
	contents := validSettings + "admin_grpc_audience: file-audience\nadmin_grpc_callers: [test-ns/test-app, test-ns/test-other]\n"
	c, err := configtest.Load(
		t,
		"AGENTPLANE_EGRESS_",
		loadConfig,
		environment(t, contents, "AGENTPLANE_EGRESS_ADMIN_GRPC_AUDIENCE=environment-audience"),
	)
	if err != nil {
		t.Fatal(err)
	}
	callers, err := c.adminGRPCCallers()
	if err != nil {
		t.Fatal(err)
	}
	if !c.adminGRPCEnabled() || c.AdminGRPCAudience != "file-audience" ||
		!slices.Equal(callers, []auth.Account{{Namespace: "test-ns", Name: "test-app"}, {Namespace: "test-ns", Name: "test-other"}}) {
		t.Fatalf("unexpected file settings: %+v", c)
	}
	const audience = "admin_grpc_audience: test-audience\n"
	const callersList = "admin_grpc_callers: [test-ns/test-app]\n"
	for name, extra := range map[string]string{
		"audience without callers": audience,
		"callers without audience": callersList,
		"caller without a slash":   audience + "admin_grpc_callers: [test-ns]\n",
		"caller without namespace": audience + "admin_grpc_callers: [/test-app]\n",
		"caller without a name":    audience + "admin_grpc_callers: [test-ns/]\n",
		"caller with a deep path":  audience + "admin_grpc_callers: [test-ns/test-app/extra]\n",
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, environment(t, validSettings+extra)); err == nil {
				t.Fatal("accepted partial or malformed admin caller configuration")
			}
		})
	}
}

func TestForwardListenerUsesOneExplicitSPIFFEAllowlist(t *testing.T) {
	c, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, required(t))
	if err != nil {
		t.Fatal(err)
	}
	if c.Forward.ListenAddress != "127.0.0.1:5555" || c.Forward.TLS == nil || c.Forward.Has(listener.KubernetesBearer) ||
		!c.Forward.Has(listener.SPIFFE) {
		t.Fatalf("unexpected SPIFFE-only forward listener: %+v", c.Forward)
	}
	invalid := map[string]string{
		"missing listener": "",
		"empty auth allowlist": withForward(validSettings, `forward:
  listen_address: 127.0.0.1:5555
  tls: {cert_file: /svid/tls.crt, key_file: /svid/tls.key}
  auth: []
`),
		"spiffe without tls": withForward(validSettings, `forward:
  listen_address: 127.0.0.1:5555
  auth:
    - mode: spiffe
      ca_file: /svid/ca.crt
`),
		"bearer beside spiffe": withForward(validSettings, `forward:
  listen_address: 127.0.0.1:5555
  tls: {cert_file: /svid/tls.crt, key_file: /svid/tls.key}
  auth:
    - mode: spiffe
      ca_file: /svid/ca.crt
    - mode: kubernetes_bearer
      audiences: [test-non-sandbox]
`),
		"missing auth mode":  withForward(validSettings, strings.Replace(forwardSettings, "- mode: spiffe\n", "-\n", 1)),
		"unknown auth mode":  withForward(validSettings, strings.Replace(forwardSettings, "mode: spiffe", "mode: unknown", 1)),
		"unknown auth field": withForward(validSettings, strings.Replace(forwardSettings, "ca_file: /svid/ca.crt", "ca_file: /svid/ca.crt\n      ignored: value", 1)),
	}
	for name, contents := range invalid {
		t.Run(name, func(t *testing.T) {
			if _, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, environment(t, contents)); err == nil {
				t.Fatal("accepted incomplete or invalid authentication configuration")
			}
		})
	}
}

func TestLocalAPIServerLoadsWholeOrNotAtAll(t *testing.T) {
	server := "local_api_server: {host: kubernetes.default.svc.test, port: 443, token_file: /token}\n"
	c, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, environment(t, validSettings+server))
	if err != nil {
		t.Fatal(err)
	}
	if c.LocalAPIServer == nil || *c.LocalAPIServer != (apiServerConfig{Host: "kubernetes.default.svc.test", Port: 443, TokenFile: "/token"}) {
		t.Fatalf("local_api_server %+v", c.LocalAPIServer)
	}
	for _, partial := range []string{
		"local_api_server: {host: kubernetes.default.svc.test, port: 443}\n",
		"local_api_server: {port: 443, token_file: /token}\n",
		"local_api_server: {host: kubernetes.default.svc.test, token_file: /token}\n",
	} {
		if _, err := configtest.Load(t, "AGENTPLANE_EGRESS_", loadConfig, environment(t, validSettings+partial)); err == nil {
			t.Errorf("%q accepted", partial)
		}
	}
}
