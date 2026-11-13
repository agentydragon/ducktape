package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"math"
	"net/url"
	"os"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/go-viper/mapstructure/v2"
	"gopkg.in/yaml.v3"
)

const (
	settingPrefix            = "AGENTPLANE_RUNNER_GATEWAY_"
	dispatcherDatabaseURLEnv = settingPrefix + "DISPATCHER_DATABASE_URL"
	runnerGatewayPodNameEnv  = settingPrefix + "POD_NAME"
	runnerGatewayPodIPEnv    = settingPrefix + "POD_IP"
)

// config is the Gateway's deployment contract: static settings come from the YAML file named by
// AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE. The database URL, Pod name and Pod IP are explicit runtime inputs.
// Callers and the controller account are written `namespace/name`.
//
// The database URL is a Secret and the Pod IP comes from the Downward API, so both are environment
// values. Kubernetes sets the Pod hostname to its name unless an explicit hostname is configured.
type config struct {
	RunnerSecurity environment_runtime_transport.Security `mapstructure:"runner_security"`
	Namespace      string                                 `mapstructure:"sandbox_namespace"`
	// PolicyNamespace holds the Identities a workload SVID may name.
	PolicyNamespace             string                                   `mapstructure:"policy_namespace"`
	DispatcherDatabaseURL       string                                   `mapstructure:"-"`
	PodName                     string                                   `mapstructure:"-"`
	PodIP                       string                                   `mapstructure:"-"`
	RunnerTokenFile             string                                   `mapstructure:"runner_token_file"`
	Kubeconfig                  string                                   `mapstructure:"kubeconfig"`
	KubernetesClientQPS         float32                                  `mapstructure:"kubernetes_client_qps"`
	RPC                         listener.Config                          `mapstructure:"rpc"`
	GatewayTLS                  *environment_runtime_transport.ClientTLS `mapstructure:"gateway_tls"`
	ProxyPort                   int                                      `mapstructure:"proxy_port"`
	RunnerControlTargetTemplate string                                   `mapstructure:"runner_control_target_template"`
	DispatcherAudience          string                                   `mapstructure:"dispatcher_audience"`
	GatewayAudience             string                                   `mapstructure:"gateway_audience"`
	EnvironmentReaders          []auth.Account                           `mapstructure:"environment_readers"`
	EnvironmentManagers         []auth.Account                           `mapstructure:"environment_managers"`
	EnvironmentLaunchers        []auth.Account                           `mapstructure:"environment_launchers"`
	DispatcherCallers           []auth.Account                           `mapstructure:"dispatcher_callers"`
	Delegation                  *delegationConfig                        `mapstructure:"delegation"`
	GatewayCallers              []auth.Account                           `mapstructure:"gateway_callers"`
	WakeCallers                 []auth.Account                           `mapstructure:"wake_callers"`
	OperatorCallers             []auth.Account                           `mapstructure:"operator_callers"`
	ControllerAccount           *auth.Account                            `mapstructure:"sandbox_controller_account"`
	QueueCap                    int                                      `mapstructure:"max_queued_per_session"`
	WatchLeaseSeconds           int                                      `mapstructure:"dispatcher_watch_lease_seconds"`
	RunnerTimeoutSeconds        int                                      `mapstructure:"runner_request_timeout_seconds"`

	CentralSampler  *dispatcher.CentralTarget `mapstructure:"central_sampler"`
	ExecutorCallers []auth.Account            `mapstructure:"executor_callers"`
	RuntimeCatalog  runtimeconfig.Catalog     `mapstructure:"runtime_catalog"`
}

// delegationConfig is an independent trust grant. It does not inherit the direct caller or wake
// allowlists, and its method list contains exact full gRPC method names served by this process.
type delegationConfig struct {
	Actor auth.Account `mapstructure:"actor"`
	// IdentityCallers lets the actor assert Identities in policy_namespace.
	IdentityCallers bool     `mapstructure:"identity_callers"`
	Methods         []string `mapstructure:"methods"`
}

func defaultConfig() config {
	return config{
		ProxyPort:                   sandboxapi.ControlServicePort,
		RunnerControlTargetTemplate: sandboxapi.ControlServiceTargetTemplate(),
		QueueCap:                    1000, WatchLeaseSeconds: 300, RunnerTimeoutSeconds: 10,
	}
}

func loadConfig() (config, error) {
	path := strings.TrimSpace(os.Getenv(settingPrefix + "CONFIG_FILE"))
	if path == "" {
		return config{}, fmt.Errorf("%s is required", settingPrefix+"CONFIG_FILE")
	}
	contents, err := os.ReadFile(path)
	if err != nil {
		return config{}, errors.New("could not read Runner Gateway settings file")
	}
	var settings map[string]any
	decoder := yaml.NewDecoder(bytes.NewReader(contents))
	if err := decoder.Decode(&settings); err != nil {
		return config{}, errors.New("could not decode Runner Gateway settings file")
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		if err != nil {
			return config{}, errors.New("could not decode Runner Gateway settings file")
		}
		return config{}, errors.New("Runner Gateway settings file must contain one YAML document")
	}

	c := defaultConfig()
	decodeHook := mapstructure.ComposeDecodeHookFunc(
		mapstructure.TextUnmarshallerHookFunc(),
		mapstructure.DecodeHookFuncType(listener.AuthenticationDecodeHook),
	)
	mapDecoder, err := mapstructure.NewDecoder(&mapstructure.DecoderConfig{
		Result: &c, TagName: "mapstructure", ErrorUnused: true, DecodeHook: decodeHook,
	})
	if err != nil {
		return config{}, errors.New("invalid Runner Gateway settings")
	}
	if err := mapDecoder.Decode(settings); err != nil {
		return config{}, errors.New("invalid Runner Gateway settings")
	}
	c.DispatcherDatabaseURL = os.Getenv(dispatcherDatabaseURLEnv)
	c.PodName = os.Getenv(runnerGatewayPodNameEnv)
	c.PodIP = os.Getenv(runnerGatewayPodIPEnv)
	c.RuntimeCatalog.Models.NormalizeHarnessNames()
	if strings.TrimSpace(c.PodName) == "" {
		var err error
		if c.PodName, err = os.Hostname(); err != nil {
			return config{}, err
		}
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

func (c *config) validate() error {
	for _, required := range []struct{ name, value string }{
		{"sandbox_namespace", c.Namespace},
		{"policy_namespace", c.PolicyNamespace},
		{"dispatcher_database_url", c.DispatcherDatabaseURL},
		{"pod_ip", c.PodIP},
		{"runner_token_file", c.RunnerTokenFile},
		{"dispatcher_audience", c.DispatcherAudience},
		{"gateway_audience", c.GatewayAudience},
		{"runner_control_target_template", c.RunnerControlTargetTemplate},
	} {
		if required.value == "" {
			return fmt.Errorf("%s is required", required.name)
		}
	}
	databaseURL, err := url.Parse(c.DispatcherDatabaseURL)
	if err != nil || (databaseURL.Scheme != "postgres" && databaseURL.Scheme != "postgresql") {
		return errors.New("dispatcher_database_url must be a PostgreSQL URL")
	}
	if err := c.RunnerSecurity.Validate(); err != nil {
		return fmt.Errorf("runner_security: %w", err)
	}
	if err := c.RPC.Validate(); err != nil {
		return fmt.Errorf("rpc: %w", err)
	}
	if !c.RPC.Has(listener.SPIFFE) || !c.RPC.Has(listener.KubernetesBearer) {
		return errors.New("rpc.auth must include spiffe and kubernetes_bearer")
	}
	if c.DispatcherAudience == c.GatewayAudience || !c.RPC.AllowsBearerAudience(c.DispatcherAudience) || !c.RPC.AllowsBearerAudience(c.GatewayAudience) {
		return errors.New("rpc kubernetes_bearer audiences must include distinct dispatcher_audience and gateway_audience")
	}
	if len(c.rpcBearerAudiences()) != 2 {
		return errors.New("rpc kubernetes_bearer must configure exactly dispatcher_audience and gateway_audience")
	}
	if c.GatewayTLS == nil {
		return errors.New("gateway_tls is required for the Gateway's control-Service calls")
	}
	if err := c.GatewayTLS.Validate(); err != nil {
		return fmt.Errorf("gateway_tls: %w", err)
	}
	if c.GatewayTLS.ServerURI != environment_runtime_transport.SessionDispatcherURI {
		return errors.New("gateway_tls.server_uri must identify the Session Dispatcher service")
	}
	if len(c.DispatcherCallers) == 0 || len(c.GatewayCallers) == 0 {
		return errors.New("dispatcher_callers and gateway_callers must each list at least one ServiceAccount")
	}
	if c.Delegation != nil && (c.Delegation.Actor.Namespace == "" || c.Delegation.Actor.Name == "" ||
		!c.Delegation.IdentityCallers || len(c.Delegation.Methods) == 0) {
		return errors.New("delegation requires an actor, identity_callers, and methods")
	}
	if c.ProxyPort < 1 || c.ProxyPort > 65535 {
		return errors.New("proxy_port must be between 1 and 65535")
	}
	if math.IsNaN(float64(c.KubernetesClientQPS)) || math.IsInf(float64(c.KubernetesClientQPS), 0) {
		return errors.New("kubernetes_client_qps must be finite")
	}
	if c.KubernetesClientQPS < 0 && c.KubernetesClientQPS != -1 {
		return errors.New("kubernetes_client_qps must be -1 or nonnegative")
	}
	for _, positive := range []struct {
		name  string
		value int
	}{
		{"max_queued_per_session", c.QueueCap},
		{"dispatcher_watch_lease_seconds", c.WatchLeaseSeconds},
		{"runner_request_timeout_seconds", c.RunnerTimeoutSeconds},
	} {
		if positive.value < 1 {
			return fmt.Errorf("%s must be positive", positive.name)
		}
	}
	for _, account := range c.EnvironmentReaders {
		if account.Namespace == "" || account.Name == "" {
			return errors.New("environment_readers requires namespace and name")
		}
	}
	for _, account := range c.EnvironmentManagers {
		if account.Namespace == "" || account.Name == "" {
			return errors.New("environment_managers requires namespace and name")
		}
	}
	for _, account := range c.EnvironmentLaunchers {
		if account.Namespace == "" || account.Name == "" {
			return errors.New("environment_launchers requires namespace and name")
		}
	}
	for _, account := range c.WakeCallers {
		if !slices.Contains(c.DispatcherCallers, account) {
			return fmt.Errorf("wake caller %s/%s is not a Dispatcher caller", account.Namespace, account.Name)
		}
	}
	for _, account := range c.OperatorCallers {
		if !slices.Contains(c.DispatcherCallers, account) {
			return fmt.Errorf("operator caller %s/%s is not a Dispatcher caller", account.Namespace, account.Name)
		}
	}
	if target := c.CentralSampler; target != nil && (target.Target == "" || target.TokenFile == "") {
		return errors.New("central_sampler requires a target and token_file")
	}

	return nil
}

func (c config) rpcBearerAudiences() []string {
	for _, method := range c.RPC.Auth {
		if bearer, ok := method.(*listener.KubernetesBearerAuth); ok && bearer != nil {
			return bearer.Audiences
		}
	}
	return nil
}

// rpcForMethodAudience binds a method family to its own audience and caller mode on the shared listener.
func (c config) rpcForMethodAudience(audience string, allowSVID bool) listener.Config {
	result := c.RPC
	result.Auth = nil
	for _, method := range c.RPC.Auth {
		switch method := method.(type) {
		case *listener.SPIFFEAuth:
			if allowSVID {
				result.Auth = append(result.Auth, &listener.SPIFFEAuth{CAFile: method.CAFile})
			}
		case *listener.KubernetesBearerAuth:
			if c.RPC.AllowsBearerAudience(audience) {
				result.Auth = append(result.Auth, &listener.KubernetesBearerAuth{Audiences: []string{audience}})
			}
		}
	}
	return result
}
