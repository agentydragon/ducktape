package app

import (
	"math"
	"slices"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher"
	"github.com/agentydragon/ducktape/agentplane/testing/configtest"
)

const databaseURLEnv = settingPrefix + "DISPATCHER_DATABASE_URL=postgres://user:never-print-this@queue/dispatcher"

const requiredFile = `sandbox_namespace: agentplane
policy_namespace: agentplane
runner_token_file: /var/run/token
runner_control_target_template: 'environments.{namespace}.svc:8082'
gateway_tls: {ca_file: /tmp/workload-ca, server_uri: spiffe://agentplane/session-dispatcher}
rpc:
  listen_address: ':8080'
  tls: {cert_file: /tmp/workload-cert, key_file: /tmp/workload-key}
  auth:
    - {mode: spiffe, ca_file: /tmp/workload-ca}
`

// required includes the explicit database Secret runtime input plus the static settings file.
func required(t *testing.T, extra ...string) []string {
	t.Helper()
	return append([]string{
		settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, requiredFile), databaseURLEnv,
	}, extra...)
}

func TestPolicyNamespaceIsRequired(t *testing.T) {
	for name, contents := range map[string]string{
		"missing": strings.Replace(requiredFile, "policy_namespace: agentplane\n", "", 1),
		"empty":   strings.Replace(requiredFile, "policy_namespace: agentplane", `policy_namespace: ""`, 1),
	} {
		t.Run(name, func(t *testing.T) {
			path := configtest.WriteSettings(t, contents)
			if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil {
				t.Fatal("accepted a missing policy_namespace")
			}
		})
	}
}

func TestServiceCallerListsKeepAuthenticationAndWakeSeparate(t *testing.T) {
	path := configtest.WriteSettings(t, requiredFile+`dispatcher_service_callers: [spiffe://agentplane/app, spiffe://agentplane/notifications]
wake_service_callers: [spiffe://agentplane/notifications]
notice_acknowledgement_service_callers: [spiffe://agentplane/app]
dispatcher_service_delegation:
  actors: [{spiffe_id: spiffe://agentplane/actions}]
  methods: [/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput]
`)
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path))
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(c.DispatcherServiceCallers, []string{peertls.AppURI, peertls.NotificationsURI}) ||
		!slices.Equal(c.WakeServiceCallers, []string{peertls.NotificationsURI}) || !slices.Equal(c.NoticeAcknowledgementServiceCallers, []string{peertls.AppURI}) ||
		c.DispatcherServiceDelegation == nil ||
		!slices.Equal(c.DispatcherServiceDelegation.Actors, []auth.ServiceDelegator{{SPIFFEID: peertls.ActionsURI}}) ||
		!slices.Equal(c.DispatcherServiceDelegation.Methods, []string{"/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput"}) {
		t.Fatalf("policy boundaries were merged: %+v", c)
	}
}

func TestSharedListenerRequiresSVIDTLS(t *testing.T) {
	for name, contents := range map[string]string{
		"SVID without TLS":    strings.Replace(requiredFile, "  tls: {cert_file: /tmp/workload-cert, key_file: /tmp/workload-key}\n", "", 1),
		"missing SPIFFE mode": strings.Replace(requiredFile, "    - {mode: spiffe, ca_file: /tmp/workload-ca}\n", "", 1),
	} {
		t.Run(name, func(t *testing.T) {
			path := configtest.WriteSettings(t, contents)
			if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil {
				t.Fatal("invalid listener settings were accepted")
			}
		})
	}
}

func TestDispatcherServiceDelegationIsScopedToServiceMethodsAndActionActors(t *testing.T) {
	base := `dispatcher_service_delegation:
  actors: [{spiffe_id: "spiffe://agentplane/actions"}]
  methods: [/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput]
`
	path := configtest.WriteSettings(t, requiredFile+base)
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path))
	if err != nil {
		t.Fatal(err)
	}
	if c.DispatcherServiceDelegation == nil || len(c.DispatcherServiceDelegation.Actors) != 1 ||
		c.DispatcherServiceDelegation.Actors[0].SPIFFEID != peertls.ActionsURI ||
		!slices.Equal(c.DispatcherServiceDelegation.Methods, []string{"/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput"}) {
		t.Fatalf("Dispatcher service delegation = %+v", c.DispatcherServiceDelegation)
	}

	for name, settings := range map[string]string{
		"EnvironmentService method": strings.Replace(base, "/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput", "/agentplane.environments.v1.EnvironmentService/ListEnvironments", 1),
		"unknown method":            strings.Replace(base, "/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput", "/agentplane.runner_gateway.dispatcher.SessionDispatcher/NotAnRPC", 1),
		"Connection provenance":     strings.Replace(base, `spiffe_id: "spiffe://agentplane/actions"`, `spiffe_id: "spiffe://agentplane/actions", connection_provenance: true`, 1),
		"Identity URI actor":        strings.Replace(base, "spiffe://agentplane/actions", "spiffe://agentplane/test/sandbox/credential-id", 1),
	} {
		t.Run(name, func(t *testing.T) {
			path := configtest.WriteSettings(t, requiredFile+settings)
			if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil {
				t.Fatal("invalid Dispatcher service delegation was accepted")
			}
		})
	}
}

func TestEmptyWakeCallerListIsAccepted(t *testing.T) {
	path := configtest.WriteSettings(t, requiredFile+"wake_service_callers: []\n")
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path))
	if err != nil {
		t.Fatalf("empty wake caller list: %v", err)
	}
	if len(c.WakeServiceCallers) != 0 {
		t.Fatalf("empty wake caller list loaded as %v", c.WakeServiceCallers)
	}
}

func TestHostProcessTargetsAreAccepted(t *testing.T) {
	settings := strings.Replace(requiredFile, "listen_address: ':8080'", "listen_address: 127.0.0.1:41001", 1)
	settings = strings.Replace(settings, "runner_control_target_template: 'environments.{namespace}.svc:8082'",
		"runner_control_target_template: 127.0.0.1:41001", 1) + `
kubeconfig: /tmp/fake-kubeconfig
`
	path := configtest.WriteSettings(t, settings)
	if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path)); err != nil {
		t.Fatal(err)
	}
}

func TestKubernetesClientQPSFlowsFromSettingsToRESTConfig(t *testing.T) {
	kubeconfig := configtest.WriteSettings(t, `apiVersion: v1
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
			settings := requiredFile + "kubeconfig: " + kubeconfig + "\n"
			if test.setting != "" {
				settings += "kubernetes_client_qps: " + test.setting + "\n"
			}
			path := configtest.WriteSettings(t, settings)
			c, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path))
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
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t))
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

func TestSettingsFileValuesIgnoreGenericEnvironmentOverrides(t *testing.T) {
	settings := strings.Replace(requiredFile, "listen_address: ':8080'", "listen_address: 127.0.0.1:9000", 1)
	path := configtest.WriteSettings(t, settings+"dispatcher_service_callers: [spiffe://agentplane/app]\n")
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(
		t,
		settingPrefix+"CONFIG_FILE="+path,
		settingPrefix+"RPC__LISTEN_ADDRESS=127.0.0.1:9999",
		settingPrefix+"DISPATCHER_SERVICE_CALLERS=spiffe://agentplane/actions",
	))
	if err != nil {
		t.Fatal(err)
	}
	if c.RPC.ListenAddress != "127.0.0.1:9000" ||
		!slices.Equal(c.DispatcherServiceCallers, []string{peertls.AppURI}) {
		t.Fatalf("settings file values changed under environment overrides: %+v", c)
	}
}

func TestSettingsAreRefusedWithoutEchoingRuntimeSecretValues(t *testing.T) {
	for _, file := range []string{
		requiredFile + "max_queued_per_session: 0\n",
		requiredFile + "unknown_setting: 1\n",
		requiredFile + "dispatcher_database_url: postgres://file.invalid/dispatcher\n",
		requiredFile + "delegation: {actor: agentplane/actions, identity_callers: true, caller_namespaces: [agentplane], methods: [x]}\n",
		"dispatcher_callers: [not-an-account]\ngateway_callers: [agentplane/a]\n",
	} {
		path := configtest.WriteSettings(t, file)
		_, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path))
		if err == nil {
			t.Fatalf("settings file was accepted: %s", file)
		}
		if strings.Contains(err.Error(), "never-print-this") {
			t.Fatalf("error carries the database URL: %v", err)
		}
	}
	if _, err := configtest.Load(t, settingPrefix, loadConfig,
		required(t, settingPrefix+"DISPATCHER_DATABASE_URL=not-a-postgres-url")); err == nil {
		t.Fatal("invalid runtime database URL was accepted")
	}
}

func TestSettingsWithoutADefaultAreRequired(t *testing.T) {
	if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t)); err != nil {
		t.Fatal(err)
	}
	for _, missing := range []string{"DISPATCHER_DATABASE_URL"} {
		rest := slices.DeleteFunc(required(t), func(item string) bool {
			return strings.HasPrefix(item, settingPrefix+missing+"=")
		})
		if _, err := configtest.Load(t, settingPrefix, loadConfig, rest); err == nil {
			t.Fatalf("ran without %s", missing)
		}
	}
	for name, key := range map[string]string{
		"sandbox_namespace": "sandbox_namespace: agentplane\n",
		"runner_token_file": "runner_token_file: /var/run/token\n",
	} {
		contents := strings.Replace(requiredFile, key, "", 1)
		path := configtest.WriteSettings(t, contents)
		if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil {
			t.Fatalf("ran without settings file field %s", name)
		}
	}
}

func TestSettingsFilePathIsRequiredAndMustBeReadable(t *testing.T) {
	if _, err := configtest.Load(t, settingPrefix, loadConfig, []string{databaseURLEnv}); err == nil {
		t.Fatal("started without a settings file path")
	}
	if _, err := configtest.Load(t, settingPrefix, loadConfig, []string{
		settingPrefix + "CONFIG_FILE=/settings/file/does/not/exist", databaseURLEnv,
	}); err == nil {
		t.Fatal("started without the required settings file")
	}
	if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t,
		settingPrefix+"CONFIG_FILE="+configtest.WriteSettings(t, requiredFile+"---\n"))); err == nil {
		t.Fatal("accepted more than one YAML document")
	}
}

func TestOptionalCentralSamplerTarget(t *testing.T) {
	path := configtest.WriteSettings(t, requiredFile+`central_sampler:
  target: sampler:8080
  token_file: /run/token
  tls: {ca_file: /run/ca.pem, server_uri: spiffe://agentplane/central-sampler}
`)
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path))
	if err != nil || c.CentralSampler == nil || c.CentralSampler.Target != "sampler:8080" || c.CentralSampler.TLS == nil || c.CentralSampler.TLS.ServerURI != peertls.CentralSamplerURI {
		t.Fatalf("configured sampler: %v %v", c.CentralSampler, err)
	}
	c.CentralSampler.TLS.ServerURI = peertls.PolicyControllerURI
	if err := c.validate(); err == nil {
		t.Fatal("accepted a central sampler target with another service identity")
	}
	c.CentralSampler.TLS = nil
	c.CentralSampler.TokenFile = ""
	if err := c.validate(); err == nil {
		t.Fatal("accepted target without token file")
	}
}

func TestSVIDCallersMayOmitBearerTokenFiles(t *testing.T) {
	settings := strings.Replace(requiredFile, "runner_token_file: /var/run/token\n", "", 1)
	settings = strings.Replace(settings,
		"gateway_tls: {ca_file: /tmp/workload-ca, server_uri: spiffe://agentplane/session-dispatcher}",
		"gateway_tls: {ca_file: /tmp/workload-ca, server_uri: spiffe://agentplane/session-dispatcher, cert_file: /tmp/sessions-cert, key_file: /tmp/sessions-key, client_uri: spiffe://agentplane/sessions}", 1)
	settings += `central_sampler:
  target: sampler:8080
  tls: {ca_file: /tmp/workload-ca, server_uri: spiffe://agentplane/central-sampler, cert_file: /tmp/sessions-cert, key_file: /tmp/sessions-key, client_uri: spiffe://agentplane/sessions}
`
	path := configtest.WriteSettings(t, settings)
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path))
	if err != nil {
		t.Fatal(err)
	}
	if c.RunnerTokenFile != "" || c.GatewayTLS.ClientURI != peertls.SessionsURI || c.CentralSampler.TokenFile != "" || c.CentralSampler.TLS.ClientURI != peertls.SessionsURI {
		t.Fatalf("SVID caller settings were not retained: gateway=%+v sampler=%+v", c.GatewayTLS, c.CentralSampler)
	}

	for name, mutate := range map[string]func(*config){
		"wrong Gateway caller": func(c *config) { c.GatewayTLS.ClientURI = peertls.AppURI },
		"mixed Gateway auth":   func(c *config) { c.RunnerTokenFile = "/run/token" },
		"wrong sampler caller": func(c *config) { c.CentralSampler.TLS.ClientURI = peertls.AppURI },
		"mixed sampler auth":   func(c *config) { c.CentralSampler.TokenFile = "/run/token" },
	} {
		t.Run(name, func(t *testing.T) {
			invalid := c
			invalid.GatewayTLS = cloneClientTLS(c.GatewayTLS)
			invalid.CentralSampler = cloneCentralTarget(c.CentralSampler)
			mutate(&invalid)
			if err := invalid.validate(); err == nil {
				t.Fatal("accepted invalid SVID caller settings")
			}
		})
	}
}

func cloneClientTLS(source *peertls.ClientTLS) *peertls.ClientTLS {
	if source == nil {
		return nil
	}
	copy := *source
	return &copy
}

func cloneCentralTarget(source *dispatcher.CentralTarget) *dispatcher.CentralTarget {
	if source == nil {
		return nil
	}
	copy := *source
	copy.TLS = cloneClientTLS(source.TLS)
	return &copy
}

func TestRuntimeProxyPeerAcceptsEachCutoverReceiver(t *testing.T) {
	for _, uri := range []string{peertls.SessionDispatcherURI, peertls.SandboxControllerURI, peertls.ActionsURI} {
		t.Run(uri, func(t *testing.T) {
			contents := strings.Replace(requiredFile, peertls.SessionDispatcherURI, uri, 1)
			path := configtest.WriteSettings(t, contents)
			c, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path))
			if uri == peertls.ActionsURI {
				if err == nil {
					t.Fatal("accepted unrelated Runtime proxy server identity")
				}
			} else if err != nil || c.GatewayTLS.ServerURI != uri {
				t.Fatalf("Runtime proxy peer %q: %v", uri, err)
			}
		})
	}
}

func TestNoticeWriterMustBeAdmittedDispatcherService(t *testing.T) {
	path := configtest.WriteSettings(t, requiredFile+"notice_acknowledgement_service_callers: [spiffe://agentplane/app]\n")
	if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil || !strings.Contains(err.Error(), "not a Dispatcher service caller") {
		t.Fatalf("unadmitted service grant: %v", err)
	}
}
