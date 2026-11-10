package main

import (
	"context"
	"errors"
	"math"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/configtest"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	authenticationv1 "k8s.io/api/authentication/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"
)

const databaseURLEnv = settingPrefix + "DISPATCHER_DATABASE_URL=postgres://user:never-print-this@queue/dispatcher"

const requiredFile = `dispatcher_callers: [agentplane/app, agentplane/notifications]
gateway_callers: [agentplane/runner-gateway]
runner_security: {mode: mtls, tls: {cert_file: /tmp/test-cert, key_file: /tmp/test-key, ca_file: /tmp/test-ca}}
gateway_tls: {ca_file: /tmp/workload-ca, server_uri: spiffe://agentplane/session-dispatcher}
rpc:
  listen_address: ':8080'
  tls: {cert_file: /tmp/workload-cert, key_file: /tmp/workload-key}
  auth:
    - {mode: spiffe, ca_file: /tmp/workload-ca}
    - {mode: kubernetes_bearer, audiences: [session-dispatcher, runner-gateway]}
`

type configTestTokenReviews struct{}

type configTestWakeBudget struct{}

func (configTestWakeBudget) ReserveWakeAttempt(context.Context, lifecycle.Caller, string, int64) (time.Duration, error) {
	return 0, nil
}

func (configTestTokenReviews) Create(
	_ context.Context,
	request *authenticationv1.TokenReview,
	_ metav1.CreateOptions,
) (*authenticationv1.TokenReview, error) {
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true,
		Audiences:     request.Spec.Audiences,
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:agentplane:workspace",
			UID:      "execution-account-uid",
		},
	}}, nil
}

// required is the environment of a deployment that sets every setting without a default.
func required(t *testing.T, extra ...string) []string {
	t.Helper()
	return append([]string{
		settingPrefix + "SANDBOX_NAMESPACE=agentplane", settingPrefix + "RUNNER_TOKEN_FILE=/var/run/token",
		settingPrefix + "DISPATCHER_AUDIENCE=session-dispatcher", settingPrefix + "GATEWAY_AUDIENCE=runner-gateway",
		settingPrefix + "POD_IP=10.0.0.5",
		settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, requiredFile), databaseURLEnv,
	}, extra...)
}

func TestCallerListsComeFromTheFileAndKeepAuthWakeAndOperatorPoliciesSeparate(t *testing.T) {
	path := configtest.WriteSettings(t, requiredFile+"wake_callers: [agentplane/notifications]\noperator_callers: [agentplane/app]\ndelegation:\n  actor: agentplane/actions\n  caller_namespaces: [agentplane]\n  methods:\n    - /agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput\n    - /agentplane.environments.v1.EnvironmentService/ListEnvironments\n    - /agentplane.environments.v1.EnvironmentService/GetEnvironment\n")
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t,
		settingPrefix+"CONFIG_FILE="+path, settingPrefix+"SANDBOX_CONTROLLER_ACCOUNT=agentplane/sandbox-controller"))
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(c.DispatcherCallers, []auth.Account{{Namespace: "agentplane", Name: "app"}, {Namespace: "agentplane", Name: "notifications"}}) ||
		!slices.Equal(c.WakeCallers, []auth.Account{{Namespace: "agentplane", Name: "notifications"}}) ||
		!slices.Equal(c.OperatorCallers, []auth.Account{{Namespace: "agentplane", Name: "app"}}) ||
		!slices.Equal(c.GatewayCallers, []auth.Account{{Namespace: "agentplane", Name: "runner-gateway"}}) ||
		c.DispatcherAudience == c.GatewayAudience || c.ControllerAccount.Name != "sandbox-controller" || c.Delegation == nil ||
		c.Delegation.Actor != (auth.Account{Namespace: "agentplane", Name: "actions"}) ||
		!slices.Equal(c.Delegation.CallerNamespaces, []string{"agentplane"}) ||
		!slices.Equal(c.Delegation.Methods, []string{
			"/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput",
			"/agentplane.environments.v1.EnvironmentService/ListEnvironments",
			"/agentplane.environments.v1.EnvironmentService/GetEnvironment",
		}) {
		t.Fatalf("policy boundaries were merged: %+v", c)
	}
}

func TestSharedListenerDeclaresMethodBearerAudiences(t *testing.T) {
	path := configtest.WriteSettings(t, requiredFile)
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path))
	if err != nil {
		t.Fatal(err)
	}
	if !c.RPC.Has(listener.SPIFFE) || !c.RPC.Has(listener.KubernetesBearer) ||
		!c.RPC.AllowsBearerAudience(c.DispatcherAudience) || !c.RPC.AllowsBearerAudience(c.GatewayAudience) ||
		c.RPC.AllowsBearerAudience("unexpected") {
		t.Fatalf("rpc auth = %+v", c.RPC.Auth)
	}
}

func TestSharedListenerRequiresSVIDTLSAndBothBearerAudiences(t *testing.T) {
	for name, contents := range map[string]string{
		"SVID without TLS":            strings.Replace(requiredFile, "  tls: {cert_file: /tmp/workload-cert, key_file: /tmp/workload-key}\n", "", 1),
		"missing Dispatcher audience": strings.Replace(requiredFile, "audiences: [session-dispatcher, runner-gateway]", "audiences: [runner-gateway]", 1),
		"missing Gateway audience":    strings.Replace(requiredFile, "audiences: [session-dispatcher, runner-gateway]", "audiences: [session-dispatcher]", 1),
	} {
		t.Run(name, func(t *testing.T) {
			path := configtest.WriteSettings(t, contents)
			if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil {
				t.Fatal("invalid listener settings were accepted")
			}
		})
	}
}

func TestOperatorCallersMustAlsoBeAuthenticatedDispatcherCallers(t *testing.T) {
	path := configtest.WriteSettings(t, requiredFile+"operator_callers: [agentplane/unknown]\n")
	if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil {
		t.Fatal("operator caller not admitted by Dispatcher auth was accepted")
	}
	path = configtest.WriteSettings(t, requiredFile+"operator_callers: []\n")
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path))
	if err != nil {
		t.Fatalf("empty operator caller list: %v", err)
	}
	if len(c.OperatorCallers) != 0 {
		t.Fatalf("empty operator caller list loaded as %v", c.OperatorCallers)
	}
}

func TestConfiguredHarnessNamesRoundTripThroughEnvironmentService(t *testing.T) {
	settings := requiredFile + `
runtime_catalog:
  models:
    models:
      - model: model-a
        display_name: Model A
        reasoning_efforts: [high]
    harnesses:
      HARNESS_CLAUDE: [model-a]
  templates:
    runner-vm:
      workspace_root: /state/workspaces
      workspace_provenance: DEPLOYMENT_CONFIG
      workspace_root_enforced: false
      creates_missing_cwd: true
`
	config, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+configtest.WriteSettings(t, settings)))
	if err != nil {
		t.Fatal(err)
	}
	if got := config.RuntimeCatalog.Models.Harnesses; len(got) != 1 || len(got["HARNESS_CLAUDE"]) != 1 || got["HARNESS_CLAUDE"][0] != "model-a" {
		t.Fatalf("loadConfig returned noncanonical harness catalog: %#v", got)
	}

	scheme := runtime.NewScheme()
	if err := api.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	sandboxUID := types.UID("sandbox-uid")
	controller := true
	sandbox := &api.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Namespace: "agentplane", Name: "workspace", UID: sandboxUID},
		Spec:       api.SandboxSpec{Template: "runner-vm"},
	}
	executionAccount := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: "agentplane", Name: "workspace", UID: "execution-account-uid",
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: api.GroupVersion.String(), Kind: "Sandbox", Name: "workspace", UID: sandboxUID, Controller: &controller,
		}},
	}}
	objects := clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(sandbox, executionAccount).Build()
	lifecycleManager, err := lifecycle.NewManager(lifecycle.Options{
		Namespace: "agentplane", Objects: objects, WakeBudget: configTestWakeBudget{},
	})
	if err != nil {
		t.Fatal(err)
	}
	service, err := environments.New("agentplane", objects, config.RuntimeCatalog, lifecycleManager, nil, environments.ManagementOptions{})
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := auth.NewResolver(configTestTokenReviews{}, config.DispatcherAudience, []string{"agentplane"}, auth.WithVerdictTTL(0))
	if err != nil {
		t.Fatal(err)
	}
	connection := grpcmethods.Serve(t, environments.Methods(resolver), func(server *grpc.Server) {
		environmentpb.RegisterEnvironmentServiceServer(server, service)
	})
	client := environmentpb.NewEnvironmentServiceClient(connection)
	ctx := metadata.AppendToOutgoingContext(t.Context(), "authorization", "Bearer test-token")
	response, err := client.ListEnvironments(ctx, &environmentpb.ListEnvironmentsRequest{Limit: 10})
	if err != nil {
		t.Fatal(err)
	}
	if len(response.Items) != 1 || len(response.Items[0].GetConfiguration().GetConfigurations()) != 1 {
		t.Fatalf("discovery response = %+v", response)
	}
	harnessName := response.Items[0].GetConfiguration().GetConfigurations()[0].GetHarness()
	harnessValue, ok := sessionpb.Harness_value[harnessName]
	if !ok {
		t.Fatalf("discovery returned harness %q, which does not decode as SessionSpec.Harness", harnessName)
	}
	harness := sessionpb.Harness(harnessValue)
	if harness != sessionpb.Harness_HARNESS_CLAUDE {
		t.Fatalf("discovery harness decoded to %s", harness)
	}
}

func TestAWakeCallerThatIsNotADispatcherCallerIsRefused(t *testing.T) {
	path := configtest.WriteSettings(t, requiredFile+"wake_callers: [agentplane/unknown]\n")
	if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path)); err == nil {
		t.Fatal("unauthorized wake caller accepted")
	}
}

func TestEmptyWakeCallerListIsAccepted(t *testing.T) {
	path := configtest.WriteSettings(t, requiredFile+"wake_callers: []\n")
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path))
	if err != nil {
		t.Fatalf("empty wake caller list: %v", err)
	}
	if len(c.WakeCallers) != 0 {
		t.Fatalf("empty wake caller list loaded as %v", c.WakeCallers)
	}
}

func TestHostProcessTargetsAreAcceptedAndTheAdvertisedPortIsBounded(t *testing.T) {
	host := []string{
		settingPrefix + "RPC_LISTEN_ADDRESS=127.0.0.1:41001", settingPrefix + "PROXY_PORT=42000",
		settingPrefix + "RUNNER_CONTROL_TARGET_TEMPLATE=127.0.0.1:41001", settingPrefix + "KUBECONFIG=/tmp/fake-kubeconfig",
	}
	if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, host...)); err != nil {
		t.Fatal(err)
	}
	for _, port := range []string{"0", "65536"} {
		if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"PROXY_PORT="+port)); err == nil {
			t.Fatalf("invalid advertised proxy port %s was accepted", port)
		}
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
			environ := required(t, settingPrefix+"KUBECONFIG="+kubeconfig)
			if test.setting != "" {
				environ = append(environ, settingPrefix+"KUBERNETES_CLIENT_QPS="+test.setting)
			}
			c, err := configtest.Load(t, settingPrefix, loadConfig, environ)
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

func TestErrorsNeverCarryTheDatabaseURL(t *testing.T) {
	for _, bad := range [][]string{
		{settingPrefix + "PROXY_PORT=not-a-number"},
		{settingPrefix + "MAX_QUEUED_PER_SESSION=0"},
		{settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, requiredFile+"unknown_setting: 1\n")},
		{settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, "dispatcher_callers: [not-an-account]\ngateway_callers: [agentplane/a]\n")},
		{settingPrefix + "SANDBOX_CONTROLLER_ACCOUNT=not-an-account"},
		{settingPrefix + "CONFIG_FILE=" + configtest.WriteSettings(t, requiredFile+"delegation: {actor: agentplane/actions}\n")},
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
			contents := strings.Replace(
				requiredFile,
				"runner_security: {mode: mtls, tls: {cert_file: /tmp/test-cert, key_file: /tmp/test-key, ca_file: /tmp/test-ca}}",
				"runner_security: "+test.security,
				1,
			)
			_, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+configtest.WriteSettings(t, contents)))
			if (err == nil) != test.valid {
				t.Fatalf("loadConfig error = %v, valid = %t", err, test.valid)
			}
		})
	}
}

func TestSettingsWithoutADefaultAreRequired(t *testing.T) {
	if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t)); err != nil {
		t.Fatal(err)
	}
	for _, missing := range []string{
		"SANDBOX_NAMESPACE", "RUNNER_TOKEN_FILE", "DISPATCHER_AUDIENCE", "GATEWAY_AUDIENCE",
		"POD_IP", "DISPATCHER_DATABASE_URL",
	} {
		rest := slices.DeleteFunc(required(t), func(item string) bool {
			return strings.HasPrefix(item, settingPrefix+missing+"=")
		})
		if _, err := configtest.Load(t, settingPrefix, loadConfig, rest); err == nil {
			t.Fatalf("ran without %s", missing)
		}
	}
	for name, contents := range map[string]string{
		"no Dispatcher callers": "gateway_callers: [agentplane/a]\n",
		"no Gateway callers":    "dispatcher_callers: [agentplane/a]\n",
	} {
		if _, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+configtest.WriteSettings(t, contents))); err == nil {
			t.Fatalf("ran with %s", name)
		}
	}
}

func TestPodNameDefaultsToTheHostname(t *testing.T) {
	hostname, err := os.Hostname()
	if err != nil {
		t.Fatal(err)
	}
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t))
	if err != nil {
		t.Fatal(err)
	}
	if c.PodName != hostname {
		t.Fatalf("pod name %q, hostname %q", c.PodName, hostname)
	}
	c, err = configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"POD_NAME=runner-gateway-0"))
	if err != nil {
		t.Fatal(err)
	}
	if c.PodName != "runner-gateway-0" {
		t.Fatalf("explicit pod name ignored: %q", c.PodName)
	}
}

func TestOptionalCentralSamplerTarget(t *testing.T) {
	path := configtest.WriteSettings(t, requiredFile+`central_sampler: {target: sampler:8080, token_file: /run/token}
`)
	c, err := configtest.Load(t, settingPrefix, loadConfig, required(t, settingPrefix+"CONFIG_FILE="+path))
	if err != nil || c.CentralSampler == nil || c.CentralSampler.Target != "sampler:8080" {
		t.Fatalf("configured sampler: %v %v", c.CentralSampler, err)
	}
	c.CentralSampler.TokenFile = ""
	if err := c.validate(); err == nil {
		t.Fatal("accepted target without token file")
	}
}

// dispatcherReviews is the external TokenReview response used by the startup identity check.
type dispatcherReviews struct {
	t         *testing.T
	principal auth.Principal
	fail      bool
}

func (r dispatcherReviews) Create(_ context.Context, request *authenticationv1.TokenReview, _ metav1.CreateOptions) (*authenticationv1.TokenReview, error) {
	if len(request.Spec.Audiences) != 1 || request.Spec.Audiences[0] != "gateway-audience" {
		r.t.Fatalf("startup TokenReview used audiences %v", request.Spec.Audiences)
	}
	if r.fail {
		return nil, errors.New("TokenReview unavailable")
	}
	return &authenticationv1.TokenReview{Status: authenticationv1.TokenReviewStatus{
		Authenticated: true, Audiences: request.Spec.Audiences,
		User: authenticationv1.UserInfo{
			Username: "system:serviceaccount:" + r.principal.Account.Namespace + ":" + r.principal.Account.Name,
			UID:      "gateway-account-uid",
			Extra: map[string]authenticationv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {r.principal.PodName},
				"authentication.kubernetes.io/pod-uid":  {r.principal.PodUID},
			},
		},
	}}, nil
}

func TestDispatcherCredentialProvesTheCurrentWorkloadAtStartup(t *testing.T) {
	account := auth.Account{Namespace: "agentplane", Name: "gateway"}
	for _, test := range []struct {
		name        string
		principal   auth.Principal
		podAccount  string
		reviewFails bool
		valid       bool
	}{
		{name: "current Pod", principal: auth.Principal{Account: account, PodName: "gateway-pod", PodUID: "current-pod-uid"}, podAccount: "gateway", valid: true},
		{name: "another namespace", principal: auth.Principal{Account: auth.Account{Namespace: "foreign", Name: "gateway"}, PodName: "gateway-pod", PodUID: "current-pod-uid"}, podAccount: "gateway"},
		{name: "another Pod", principal: auth.Principal{Account: account, PodName: "other-pod", PodUID: "current-pod-uid"}, podAccount: "gateway"},
		{name: "replaced Pod", principal: auth.Principal{Account: account, PodName: "gateway-pod", PodUID: "old-pod-uid"}, podAccount: "gateway"},
		{name: "no Pod binding", principal: auth.Principal{Account: account}, podAccount: "gateway"},
		{name: "Pod account mismatch", principal: auth.Principal{Account: account, PodName: "gateway-pod", PodUID: "current-pod-uid"}, podAccount: "app"},
		{name: "unlisted account", principal: auth.Principal{Account: auth.Account{Namespace: "agentplane", Name: "app"}, PodName: "gateway-pod", PodUID: "current-pod-uid"}, podAccount: "app"},
		{name: "review unavailable", reviewFails: true, podAccount: "gateway"},
	} {
		t.Run(test.name, func(t *testing.T) {
			c := config{Namespace: "agentplane", PodName: "gateway-pod", RunnerTokenFile: configtest.WriteSettings(t, "test-workload-token"), GatewayCallers: []auth.Account{account, {Namespace: "foreign", Name: "gateway"}}}
			resolver, err := auth.NewResolver(dispatcherReviews{t: t, principal: test.principal, fail: test.reviewFails}, "gateway-audience", []string{"agentplane", "foreign"})
			if err != nil {
				t.Fatal(err)
			}
			scheme := runtime.NewScheme()
			if err := corev1.AddToScheme(scheme); err != nil {
				t.Fatal(err)
			}
			pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Namespace: "agentplane", Name: "gateway-pod", UID: "current-pod-uid"}, Spec: corev1.PodSpec{ServiceAccountName: test.podAccount}}
			objects := clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(pod).Build()
			got, err := dispatcherAccount(t.Context(), c, resolver, objects)
			if test.valid {
				if err != nil || got != account {
					t.Fatalf("startup account=%v, error=%v", got, err)
				}
			} else if err == nil {
				t.Fatalf("startup admitted account %v", got)
			}
		})
	}
}
