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

// settingsWithForward supplies the independent forward auth required by tests of other settings.
func settingsWithForward(t *testing.T, contents string) string {
	t.Helper()
	return configtest.WriteSettings(t, `forward:
  listen_address: 0.0.0.0:8888
  auth:
    - mode: kubernetes_bearer
      audience: hop
`+contents)
}

// required is the environment of a deployment that sets every setting without a default.
func required(t *testing.T, extra ...string) []string {
	t.Helper()
	return append([]string{
		settingPrefix + "RULES_NAMESPACE=rules", settingPrefix + "CREDENTIALS_NAMESPACE=secrets",
		settingPrefix + "CA_CERT=/ca.crt", settingPrefix + "CA_KEY=/ca.key",
		settingPrefix + "CONFIG_FILE=" + settingsWithForward(t, "allowed_service_account_namespaces: [ns]\n"), databaseURLEnv,
	}, extra...)
}

func TestCollectionsLoadFromTheFileAndScalarsFromTheEnvironment(t *testing.T) {
	path := configtest.WriteSettings(t, `allowed_service_account_namespaces: [file-namespace]
projected_token_audiences: [file-audience]
exempt_networks: [127.0.0.0/8]
forward:
  listen_address: 127.0.0.1:5555
  auth:
    - mode: kubernetes_bearer
      audience: file-hop-audience
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
	if c.Forward.ListenAddress != "127.0.0.1:5555" || c.Forward.BearerAudience() != "file-hop-audience" ||
		c.AdminListenAddress != "127.0.0.1:1111" || c.AgentAPIListenAddress != "0.0.0.0:8082" ||
		!slices.Equal(c.AllowedServiceAccountNamespaces, []string{"file-namespace"}) ||
		!slices.Equal(c.ProjectedTokenAudiences, []string{"file-audience"}) ||
		!slices.Equal(c.ExemptNetworks, []netip.Prefix{netip.MustParsePrefix("127.0.0.0/8")}) {
		t.Fatalf("unexpected merged config: %+v", c)
	}
}

func TestErrorsNeverCarryTheDatabaseURL(t *testing.T) {
	for _, bad := range [][]string{
		{settingPrefix + "DECISION_BATCH_SIZE=0"},
		{settingPrefix + "CONFIG_FILE=" + settingsWithForward(t, "allowed_service_account_namespaces: [ns]\nlisten_address: 127.0.0.1:8888\n")},
		{settingPrefix + "CONFIG_FILE=" + settingsWithForward(t, "allowed_service_account_namespaces: [ns]\ntoken_audience: legacy\n")},
		{settingPrefix + "CONFIG_FILE=" + settingsWithForward(t, "allowed_service_account_namespaces: [ns]\nlog_levels: {agentplane.other: INFO}\n")},
		{settingPrefix + "CONFIG_FILE=" + settingsWithForward(t, "allowed_service_account_namespaces: [ns]\nexempt_networks: [not-a-prefix]\n")},
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

func TestForwardListenerUsesOneExplicitBearerAuthMode(t *testing.T) {
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t))
	if err != nil {
		t.Fatal(err)
	}
	if c.Forward.ListenAddress != "0.0.0.0:8888" || len(c.Forward.Auth) != 1 ||
		!c.Forward.Has(listener.KubernetesBearer) || c.Forward.BearerAudience() != "hop" {
		t.Fatalf("unexpected forward listener: %+v", c.Forward)
	}

	for name, contents := range map[string]string{
		"missing listener": "allowed_service_account_namespaces: [ns]\n",
		"empty auth":       "allowed_service_account_namespaces: [ns]\nforward: {listen_address: 127.0.0.1:8888, auth: []}\n",
		"null auth entry": `allowed_service_account_namespaces: [ns]
forward: {listen_address: 127.0.0.1:8888, auth: [null]}
`,
		"scalar auth entry": `allowed_service_account_namespaces: [ns]
forward: {listen_address: 127.0.0.1:8888, auth: [kubernetes_bearer]}
`,
		"missing mode": `allowed_service_account_namespaces: [ns]
forward: {listen_address: 127.0.0.1:8888, auth: [{audience: hop}]}
`,
		"unknown mode": `allowed_service_account_namespaces: [ns]
forward: {listen_address: 127.0.0.1:8888, auth: [{mode: spiffe, audience: hop}]}
`,
		"missing audience": `allowed_service_account_namespaces: [ns]
forward: {listen_address: 127.0.0.1:8888, auth: [{mode: kubernetes_bearer}]}
`,
		"extra field": `allowed_service_account_namespaces: [ns]
forward: {listen_address: 127.0.0.1:8888, auth: [{mode: kubernetes_bearer, audience: hop, ca_file: /roots}]}
`,
	} {
		t.Run(name, func(t *testing.T) {
			path := configtest.WriteSettings(t, contents)
			if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil {
				t.Fatal("accepted invalid forward listener configuration")
			}
		})
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
			environ := required(t, settingPrefix+"CONFIG_FILE="+settingsWithForward(t, contents))
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

func TestAdminGRPCLoadsFromTheFileAndTheEnvironment(t *testing.T) {
	path := settingsWithForward(t, `allowed_service_account_namespaces: [ns]
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
			path := settingsWithForward(t, "allowed_service_account_namespaces: [ns]\n"+contents)
			if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil {
				t.Fatal("accepted")
			}
		})
	}
}
