package main

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
)

const databaseURLEnv = settingPrefix + "DISPATCHER_DATABASE_URL=postgres://user:never-print-this@queue/dispatcher"

const requiredFile = `dispatcher_callers: [agentplane/app, agentplane/notifications]
gateway_callers: [agentplane/runner-gateway]
runner_transports: [forward, reverse]
runner_listen_address: ":8090"
runner_tls: {cert_file: /tmp/test-cert, key_file: /tmp/test-key, ca_file: /tmp/test-ca}
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
	path := writeSettings(t, requiredFile+"wake_callers: [agentplane/app]\n")
	c, err := loadTestConfig(t, required(t,
		settingPrefix+"CONFIG_FILE="+path, settingPrefix+"SANDBOX_CONTROLLER_ACCOUNT=agentplane/sandbox-controller"))
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(c.DispatcherCallers, []auth.Account{{Namespace: "agentplane", Name: "app"}, {Namespace: "agentplane", Name: "notifications"}}) ||
		!slices.Equal(c.WakeCallers, []auth.Account{{Namespace: "agentplane", Name: "app"}}) ||
		!slices.Equal(c.GatewayCallers, []auth.Account{{Namespace: "agentplane", Name: "runner-gateway"}}) ||
		c.DispatcherAudience == c.GatewayAudience || c.ControllerAccount.Name != "sandbox-controller" {
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

func TestErrorsNeverCarryTheDatabaseURL(t *testing.T) {
	for _, bad := range [][]string{
		{settingPrefix + "PROXY_PORT=not-a-number"},
		{settingPrefix + "MAX_QUEUED_PER_SESSION=0"},
		{settingPrefix + "CONFIG_FILE=" + writeSettings(t, requiredFile+"unknown_setting: 1\n")},
		{settingPrefix + "CONFIG_FILE=" + writeSettings(t, "dispatcher_callers: [not-an-account]\ngateway_callers: [agentplane/a]\n")},
		{settingPrefix + "SANDBOX_CONTROLLER_ACCOUNT=not-an-account"},
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

func TestRunnerTransportSelectionIsExplicitAndListenerMatchesReverseMode(t *testing.T) {
	for _, extra := range []string{
		"runner_transports: []\n",
		"runner_transports: [auto]\n",
		"runner_transports: [reverse, reverse]\n",
		"runner_transports: [forward]\nrunner_listen_address: ':8090'\n",
		"runner_transports: [reverse]\nrunner_listen_address: ''\n",
	} {
		// Replace the deployment's transport keys rather than relying on YAML duplicate-key handling.
		base := "dispatcher_callers: [agentplane/app]\ngateway_callers: [agentplane/runner-gateway]\nrunner_tls: {cert_file: /tmp/test-cert, key_file: /tmp/test-key, ca_file: /tmp/test-ca}\n"
		if _, err := loadTestConfig(t, required(t, settingPrefix+"CONFIG_FILE="+writeSettings(t, base+extra))); err == nil {
			t.Fatalf("invalid transport configuration accepted: %s", extra)
		}
	}
	base := "dispatcher_callers: [agentplane/app]\ngateway_callers: [agentplane/runner-gateway]\nrunner_tls: {cert_file: /tmp/test-cert, key_file: /tmp/test-key, ca_file: /tmp/test-ca}\n"
	if _, err := loadTestConfig(t, required(t, settingPrefix+"CONFIG_FILE="+writeSettings(t, base))); err == nil {
		t.Fatal("missing runner transport selection was accepted")
	}
	if _, err := loadTestConfig(t, required(t, settingPrefix+"CONFIG_FILE="+writeSettings(t, base+"runner_transports: [forward]\n"))); err != nil {
		t.Fatalf("explicit forward mode should not open a reverse listener: %v", err)
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
