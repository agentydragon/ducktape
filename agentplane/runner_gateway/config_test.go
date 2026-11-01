package main

import (
	"math"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
)

const databaseURLEnv = settingPrefix + "DISPATCHER_DATABASE_URL=postgres://user:never-print-this@queue/dispatcher"

const requiredFile = `dispatcher_callers: [agentplane/app, agentplane/notifications]
gateway_callers: [agentplane/runner-gateway]
runner_security: {mode: mtls, tls: {cert_file: /tmp/test-cert, key_file: /tmp/test-key, ca_file: /tmp/test-ca}}
`

// required is the environment of a deployment that sets every setting without a default.
func required(t *testing.T, extra ...string) []string {
	t.Helper()
	return append([]string{
		settingPrefix + "SANDBOX_NAMESPACE=agentplane", settingPrefix + "RUNNER_TOKEN_FILE=/var/run/token",
		settingPrefix + "DISPATCHER_AUDIENCE=session-dispatcher", settingPrefix + "GATEWAY_AUDIENCE=runner-gateway",
		settingPrefix + "POD_IP=10.0.0.5",
		settingPrefix + "CONFIG_FILE=" + writeSettings(t, requiredFile), databaseURLEnv,
	}, extra...)
}

func loadTestConfig(t *testing.T, environ []string) (config, error) {
	t.Helper()
	for _, entry := range os.Environ() {
		name, _, _ := strings.Cut(entry, "=")
		if strings.HasPrefix(name, settingPrefix) {
			t.Setenv(name, "")
		}
	}
	for _, entry := range environ {
		name, value, _ := strings.Cut(entry, "=")
		t.Setenv(name, value)
	}
	return loadConfig()
}

func writeSettings(t *testing.T, contents string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "settings.yaml")
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestCallerListsComeFromTheFileAndKeepAuthAndWakePoliciesSeparate(t *testing.T) {
	path := writeSettings(t, requiredFile+"wake_callers: [agentplane/app]\ndelegation:\n  actor: agentplane/actions\n  caller_namespaces: [agentplane]\n  methods: [/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput]\n")
	c, err := loadTestConfig(t, required(t,
		settingPrefix+"CONFIG_FILE="+path, settingPrefix+"SANDBOX_CONTROLLER_ACCOUNT=agentplane/sandbox-controller"))
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(c.DispatcherCallers, []auth.Account{{Namespace: "agentplane", Name: "app"}, {Namespace: "agentplane", Name: "notifications"}}) ||
		!slices.Equal(c.WakeCallers, []auth.Account{{Namespace: "agentplane", Name: "app"}}) ||
		!slices.Equal(c.GatewayCallers, []auth.Account{{Namespace: "agentplane", Name: "runner-gateway"}}) ||
		c.DispatcherAudience == c.GatewayAudience || c.ControllerAccount.Name != "sandbox-controller" || c.Delegation == nil ||
		c.Delegation.Actor != (auth.Account{Namespace: "agentplane", Name: "actions"}) ||
		!slices.Equal(c.Delegation.CallerNamespaces, []string{"agentplane"}) ||
		!slices.Equal(c.Delegation.Methods, []string{"/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput"}) {
		t.Fatalf("policy boundaries were merged: %+v", c)
	}
}

func TestAWakeCallerThatIsNotADispatcherCallerIsRefused(t *testing.T) {
	path := writeSettings(t, requiredFile+"wake_callers: [agentplane/unknown]\n")
	if _, err := loadTestConfig(t, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil {
		t.Fatal("unauthorized wake caller accepted")
	}
}

func TestEmptyWakeCallerListIsAccepted(t *testing.T) {
	path := writeSettings(t, requiredFile+"wake_callers: []\n")
	c, err := loadTestConfig(t, required(t, settingPrefix+"CONFIG_FILE="+path))
	if err != nil {
		t.Fatalf("empty wake caller list: %v", err)
	}
	if len(c.WakeCallers) != 0 {
		t.Fatalf("empty wake caller list loaded as %v", c.WakeCallers)
	}
}

func TestHostProcessTargetsAreAcceptedAndTheAdvertisedPortIsBounded(t *testing.T) {
	host := []string{
		settingPrefix + "LISTEN_ADDRESS=127.0.0.1:41001", settingPrefix + "PROXY_PORT=42000",
		settingPrefix + "RUNNER_CONTROL_TARGET_TEMPLATE=127.0.0.1:41001", settingPrefix + "KUBECONFIG=/tmp/fake-kubeconfig",
	}
	if _, err := loadTestConfig(t, required(t, host...)); err != nil {
		t.Fatal(err)
	}
	for _, port := range []string{"0", "65536"} {
		if _, err := loadTestConfig(t, required(t, settingPrefix+"PROXY_PORT="+port)); err == nil {
			t.Fatalf("invalid advertised proxy port %s was accepted", port)
		}
	}
}

func TestKubernetesClientQPSFlowsFromSettingsToRESTConfig(t *testing.T) {
	kubeconfig := writeSettings(t, `apiVersion: v1
kind: Config
clusters:
- name: test
  cluster: {server: "https://127.0.0.1:6443", insecure-skip-tls-verify: true}
contexts:
- name: test
  context: {cluster: test, user: test}
current-context: test
users:
- name: test
  user: {}
`)
	for _, test := range []struct {
		name, setting string
		want          float32
	}{
		{name: "client-go default", want: 0},
		{name: "limiter disabled", setting: "-1", want: -1},
		{name: "positive limit", setting: "25.5", want: 25.5},
	} {
		t.Run(test.name, func(t *testing.T) {
			environ := required(t, settingPrefix+"KUBECONFIG="+kubeconfig)
			if test.setting != "" {
				environ = append(environ, settingPrefix+"KUBERNETES_CLIENT_QPS="+test.setting)
			}
			c, err := loadTestConfig(t, environ)
			if err != nil {
				t.Fatal(err)
			}
			restConfig, err := kubernetesConfig(c)
			if err != nil {
				t.Fatal(err)
			}
			if restConfig.QPS != test.want {
				t.Fatalf("REST QPS = %v, want %v", restConfig.QPS, test.want)
			}
		})
	}
	c, err := loadTestConfig(t, required(t))
	if err != nil {
		t.Fatal(err)
	}
	for _, invalid := range []float32{-2, -0.5, float32(math.NaN()), float32(math.Inf(1))} {
		c.KubernetesClientQPS = invalid
		if err := c.validate(); err == nil {
			t.Fatalf("invalid QPS %v accepted", invalid)
		}
	}
}

func TestErrorsNeverCarryTheDatabaseURL(t *testing.T) {
	for _, bad := range [][]string{
		{settingPrefix + "PROXY_PORT=not-a-number"},
		{settingPrefix + "MAX_QUEUED_PER_SESSION=0"},
		{settingPrefix + "CONFIG_FILE=" + writeSettings(t, requiredFile+"unknown_setting: 1\n")},
		{settingPrefix + "CONFIG_FILE=" + writeSettings(t, "dispatcher_callers: [not-an-account]\ngateway_callers: [agentplane/a]\n")},
		{settingPrefix + "SANDBOX_CONTROLLER_ACCOUNT=not-an-account"},
		{settingPrefix + "CONFIG_FILE=" + writeSettings(t, requiredFile+"delegation: {actor: agentplane/actions}\n")},
	} {
		_, err := loadTestConfig(t, required(t, bad...))
		if err == nil {
			t.Fatalf("%v accepted", bad)
		}
		if strings.Contains(err.Error(), "never-print-this") {
			t.Fatalf("error carries the database URL: %v", err)
		}
	}
}

func TestRunnerSecurityMustBeExplicitAndConsistent(t *testing.T) {
	for _, test := range []struct {
		name, security string
		valid          bool
	}{
		{name: "insecure", security: "{mode: insecure}", valid: true},
		{name: "missing mode", security: "{}"},
		{name: "unknown mode", security: "{mode: automatic}"},
		{name: "missing mtls credentials", security: "{mode: mtls}"},
		{name: "insecure with credentials", security: "{mode: insecure, tls: {cert_file: /a, key_file: /b, ca_file: /c}}"},
	} {
		t.Run(test.name, func(t *testing.T) {
			contents := "dispatcher_callers: [agentplane/app]\ngateway_callers: [agentplane/runner-gateway]\nrunner_security: " + test.security + "\n"
			_, err := loadTestConfig(t, required(t, settingPrefix+"CONFIG_FILE="+writeSettings(t, contents)))
			if (err == nil) != test.valid {
				t.Fatalf("loadConfig error = %v, valid = %t", err, test.valid)
			}
		})
	}
}

func TestSettingsWithoutADefaultAreRequired(t *testing.T) {
	if _, err := loadTestConfig(t, required(t)); err != nil {
		t.Fatal(err)
	}
	for _, missing := range []string{
		"SANDBOX_NAMESPACE", "RUNNER_TOKEN_FILE", "DISPATCHER_AUDIENCE", "GATEWAY_AUDIENCE",
		"POD_IP", "DISPATCHER_DATABASE_URL",
	} {
		rest := slices.DeleteFunc(required(t), func(item string) bool {
			return strings.HasPrefix(item, settingPrefix+missing+"=")
		})
		if _, err := loadTestConfig(t, rest); err == nil {
			t.Fatalf("ran without %s", missing)
		}
	}
	for name, contents := range map[string]string{
		"no Dispatcher callers": "gateway_callers: [agentplane/a]\n",
		"no Gateway callers":    "dispatcher_callers: [agentplane/a]\n",
	} {
		if _, err := loadTestConfig(t, required(t, settingPrefix+"CONFIG_FILE="+writeSettings(t, contents))); err == nil {
			t.Fatalf("ran with %s", name)
		}
	}
}

func TestPodNameDefaultsToTheHostname(t *testing.T) {
	hostname, err := os.Hostname()
	if err != nil {
		t.Fatal(err)
	}
	c, err := loadTestConfig(t, required(t))
	if err != nil {
		t.Fatal(err)
	}
	if c.PodName != hostname {
		t.Fatalf("pod name %q, hostname %q", c.PodName, hostname)
	}
	c, err = loadTestConfig(t, required(t, settingPrefix+"POD_NAME=runner-gateway-0"))
	if err != nil {
		t.Fatal(err)
	}
	if c.PodName != "runner-gateway-0" {
		t.Fatalf("explicit pod name ignored: %q", c.PodName)
	}
}

func TestOptionalCentralSamplerTarget(t *testing.T) {
	path := writeSettings(t, requiredFile+`central_sampler: {target: sampler:8080, token_file: /run/token}
`)
	c, err := loadTestConfig(t, required(t, settingPrefix+"CONFIG_FILE="+path))
	if err != nil || c.CentralSampler == nil || c.CentralSampler.Target != "sampler:8080" {
		t.Fatalf("configured sampler: %v %v", c.CentralSampler, err)
	}
	c.CentralSampler.TokenFile = ""
	if err := c.validate(); err == nil {
		t.Fatal("accepted target without token file")
	}
}
