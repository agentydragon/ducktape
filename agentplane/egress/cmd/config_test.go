package main

import (
	"net/netip"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/testing/configtest"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
)

const databaseURLEnv = settingPrefix + "DATABASE_URL=postgresql://user:never-print-this@db/egress"

// required is the environment of a deployment that sets every setting without a default.
func required(t *testing.T, extra ...string) []string {
	t.Helper()
	return append([]string{
		settingPrefix + "RULES_NAMESPACE=rules", settingPrefix + "CREDENTIALS_NAMESPACE=secrets",
		settingPrefix + "CA_CERT=/ca.crt", settingPrefix + "CA_KEY=/ca.key",
		settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, `allowed_service_account_namespaces: [ns]
forward:
  listen_address: 0.0.0.0:8888
  tls: {cert_file: /svid/tls.crt, key_file: /svid/tls.key}
  auth:
    - mode: spiffe
      ca_file: /svid/ca.crt
`), databaseURLEnv,
	}, extra...)
}

func TestCollectionsLoadFromTheFileAndScalarsFromTheEnvironment(t *testing.T) {
	path := configtest.WriteSettings(t, `allowed_service_account_namespaces: [file-namespace]
projected_token_audiences: [file-audience]
exempt_networks: [127.0.0.0/8]
forward:
  listen_address: 127.0.0.1:5555
  tls: {cert_file: /svid/tls.crt, key_file: /svid/tls.key}
  auth:
    - mode: spiffe
      ca_file: /svid/ca.crt
admin_listen_address: 127.0.0.1:1111
`)
	c, err := configtest.Load(t, settingPrefix, loadConfig, []string{
		settingPrefix + "CONFIG_FILE=" + path, settingPrefix + "RULES_NAMESPACE=rules",
		settingPrefix + "CREDENTIALS_NAMESPACE=secrets", settingPrefix + "CA_CERT=/ca.crt",
		settingPrefix + "CA_KEY=/ca.key", databaseURLEnv,
	})
	if err != nil {
		t.Fatal(err)
	}
	if c.Forward.ListenAddress != "127.0.0.1:5555" || !c.Forward.Has(listener.SPIFFE) ||
		c.AdminListenAddress != "127.0.0.1:1111" ||
		!slices.Equal(c.AllowedServiceAccountNamespaces, []string{"file-namespace"}) ||
		!slices.Equal(c.ProjectedTokenAudiences, []string{"file-audience"}) ||
		!slices.Equal(c.ExemptNetworks, []netip.Prefix{netip.MustParsePrefix("127.0.0.0/8")}) {
		t.Fatalf("unexpected merged config: %+v", c)
	}
}

func TestErrorsNeverCarryTheDatabaseURL(t *testing.T) {
	for _, bad := range [][]string{
		{settingPrefix + "DECISION_BATCH_SIZE=0"},
		{settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, "allowed_service_account_namespaces: [ns]\nlisten_port: 8888\n")},
		{settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, "allowed_service_account_namespaces: [ns]\ntoken_audience: old\n")},
		{settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, "allowed_service_account_namespaces: [ns]\nlog_levels: {agentplane.other: INFO}\n")},
		{settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, "allowed_service_account_namespaces: [ns]\nexempt_networks: [not-a-prefix]\n")},
	} {
		_, err := configtest.Load(t, settingPrefix, loadConfig, required(t, bad...))
		if err == nil {
			t.Fatalf("%v accepted", bad)
		}
		if strings.Contains(err.Error(), "never-print-this") {
			t.Fatalf("error carries the database URL: %v", err)
		}
	}
}

func TestSettingsWithoutADefaultAreRequired(t *testing.T) {
	if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t)); err != nil {
		t.Fatal(err)
	}
	for _, missing := range []string{
		"RULES_NAMESPACE", "CREDENTIALS_NAMESPACE", "CA_CERT", "CA_KEY", "DATABASE_URL",
	} {
		rest := slices.DeleteFunc(required(t), func(item string) bool {
			return strings.HasPrefix(item, settingPrefix+missing+"=")
		})
		if _, err := configtest.Load(t, settingPrefix, loadConfig, rest); err == nil {
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
			environ := required(t, settingPrefix+"CONFIG_FILE="+configtest.WriteSettings(t, contents))
			if _, err := configtest.Load(t, settingPrefix, loadConfig, environ); err == nil {
				t.Fatal("ran with no usable workload namespace")
			}
		})
	}
}

func TestAdminGRPCIsOffUnlessItsSettingsAreSet(t *testing.T) {
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t))
	if err != nil {
		t.Fatal(err)
	}
	if c.adminGRPCEnabled() {
		t.Fatal("admin gRPC enabled without being configured")
	}
}

func TestForwardListenerUsesOneExplicitAuthAllowlist(t *testing.T) {
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t))
	if err != nil {
		t.Fatal(err)
	}
	if c.Forward.ListenAddress != "0.0.0.0:8888" || c.Forward.TLS == nil || c.Forward.Has(listener.KubernetesBearer) ||
		!c.Forward.Has(listener.SPIFFE) {
		t.Fatalf("unexpected SPIFFE-only forward listener: %+v", c.Forward)
	}
	for name, contents := range map[string]string{
		"missing forward listener": "allowed_service_account_namespaces: [ns]\n",
		"empty auth allowlist":     "allowed_service_account_namespaces: [ns]\nforward: {listen_address: 127.0.0.1:8888}\n",
		"spiffe without tls": `allowed_service_account_namespaces: [ns]
forward:
  listen_address: 127.0.0.1:8888
  auth:
    - mode: spiffe
      ca_file: /svid/ca.crt
`,
		"bearer beside spiffe": `allowed_service_account_namespaces: [ns]
forward:
  listen_address: 127.0.0.1:8888
  tls: {cert_file: /svid/tls.crt, key_file: /svid/tls.key}
  auth:
    - mode: spiffe
      ca_file: /svid/ca.crt
    - mode: kubernetes_bearer
      audiences: [test-non-sandbox]
`,
	} {
		t.Run(name, func(t *testing.T) {
			environ := required(t, settingPrefix+"CONFIG_FILE="+configtest.WriteSettings(t, contents))
			if _, err := configtest.Load(t, settingPrefix, loadConfig, environ); err == nil {
				t.Fatal("accepted incomplete or invalid forward listener configuration")
			}
		})
	}
	configured, err := configtest.Load(t, settingPrefix, loadConfig, required(
		t,
		settingPrefix+"CONFIG_FILE="+configtest.WriteSettings(t, `allowed_service_account_namespaces: [ns]
projected_token_audiences: [destination]
forward:
  listen_address: 127.0.0.1:8888
  tls:
    cert_file: /svid/tls.crt
    key_file: /svid/tls.key
  auth:
    - mode: spiffe
      ca_file: /svid/ca.crt
`),
	))
	if err != nil {
		t.Fatal(err)
	}
	if configured.Forward.ListenAddress != "127.0.0.1:8888" || configured.Forward.TLS == nil ||
		configured.Forward.TLS.CertFile != "/svid/tls.crt" || configured.Forward.TLS.KeyFile != "/svid/tls.key" ||
		!configured.Forward.Has(listener.SPIFFE) {
		t.Fatalf("unexpected forward listener settings: %+v", configured.Forward)
	}
}

func TestAdminGRPCLoadsFromTheFileAndTheEnvironment(t *testing.T) {
	path := configtest.WriteSettings(t, `allowed_service_account_namespaces: [ns]
forward:
  listen_address: 0.0.0.0:8888
  tls: {cert_file: /svid/tls.crt, key_file: /svid/tls.key}
  auth:
    - mode: spiffe
      ca_file: /svid/ca.crt
admin_grpc_audience: file-audience
admin_grpc_callers: [test-ns/test-app, test-ns/test-other]
`)
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(
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
			path := configtest.WriteSettings(t, "allowed_service_account_namespaces: [ns]\n"+contents)
			if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil {
				t.Fatal("accepted")
			}
		})
	}
}

func TestForwardListenerAuthenticationRequiresOnlyItsVariantFields(t *testing.T) {
	for name, testCase := range map[string]struct {
		authentication string
		tls            bool
	}{
		"missing mode":   {authentication: `- audience: hop`},
		"null variant":   {authentication: `- null`},
		"scalar variant": {authentication: `- spiffe`},
		"unknown mode": {authentication: `- mode: unknown
  audience: hop`},
		"missing SPIFFE roots": {authentication: `- mode: spiffe`, tls: true},
		"SPIFFE with bearer audience": {authentication: `- mode: spiffe
  ca_file: /svid/ca.crt
  audience: hop`, tls: true},
		"missing bearer audience": {authentication: `- mode: kubernetes_bearer`},
		"bearer with SPIFFE roots": {authentication: `- mode: kubernetes_bearer
  audience: hop
  ca_file: /svid/ca.crt`},
		"unknown variant field": {authentication: `- mode: kubernetes_bearer
  audience: hop
  ignored: value`},
	} {
		t.Run(name, func(t *testing.T) {
			contents := "allowed_service_account_namespaces: [ns]\nforward:\n  listen_address: 127.0.0.1:8888\n  auth:\n"
			for _, line := range strings.Split(testCase.authentication, "\n") {
				contents += "    " + line + "\n"
			}
			if testCase.tls {
				contents += "  tls: {cert_file: /svid/tls.crt, key_file: /svid/tls.key}\n"
			}
			if _, err := configtest.Load(t, settingPrefix, loadConfig, required(
				t, settingPrefix+"CONFIG_FILE="+configtest.WriteSettings(t, contents),
			)); err == nil {
				t.Fatal("accepted an invalid authentication variant")
			}
		})
	}
}

func TestLocalAPIServerLoadsWholeOrNotAtAll(t *testing.T) {
	settings := func(server string) []string {
		return []string{
			settingPrefix + "RULES_NAMESPACE=rules", settingPrefix + "CREDENTIALS_NAMESPACE=secrets",
			settingPrefix + "CA_CERT=/ca.crt", settingPrefix + "CA_KEY=/ca.key", databaseURLEnv,
			settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, `allowed_service_account_namespaces: [ns]
forward:
  listen_address: 0.0.0.0:8888
  tls: {cert_file: /svid/tls.crt, key_file: /svid/tls.key}
  auth:
    - mode: spiffe
      ca_file: /svid/ca.crt
`+server),
		}
	}
	c, err := configtest.Load(t, settingPrefix, loadConfig, settings(
		"local_api_server: {host: kubernetes.default.svc.test, port: 443, token_file: /token}\n",
	))
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
		if _, err := configtest.Load(t, settingPrefix, loadConfig, settings(partial)); err == nil {
			t.Errorf("%q accepted", partial)
		}
	}
}
