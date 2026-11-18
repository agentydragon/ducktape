package app

import (
	"errors"
	"fmt"
	"math"
	"net/url"
	"os"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/configfile"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher"
)

const (
	settingPrefix            = "AGENTPLANE_SESSIONS_"
	dispatcherDatabaseURLEnv = settingPrefix + "DISPATCHER_DATABASE_URL"
)

// config is the Gateway's deployment contract: static settings come from the YAML file named by
// AGENTPLANE_SESSIONS_CONFIG_FILE.
// The database URL is an explicit runtime input.
// Service caller grants contain exact SPIFFE IDs.
type config struct {
	Namespace string `mapstructure:"sandbox_namespace"`
	// PolicyNamespace holds the Identities a workload SVID may name.
	PolicyNamespace             string                   `mapstructure:"policy_namespace"`
	DispatcherDatabaseURL       string                   `mapstructure:"-"`
	RunnerTokenFile             string                   `mapstructure:"runner_token_file"`
	Kubeconfig                  string                   `mapstructure:"kubeconfig"`
	KubernetesClientQPS         float32                  `mapstructure:"kubernetes_client_qps"`
	RPC                         listener.Config          `mapstructure:"rpc"`
	GatewayTLS                  *peertls.ClientTLS       `mapstructure:"gateway_tls"`
	RunnerControlTargetTemplate string                   `mapstructure:"runner_control_target_template"`
	WakeServiceCallers          []string                 `mapstructure:"wake_service_callers"`
	DispatcherServiceCallers    []string                 `mapstructure:"dispatcher_service_callers"`
	DispatcherServiceDelegation *serviceDelegationConfig `mapstructure:"dispatcher_service_delegation"`
	QueueCap                    int                      `mapstructure:"max_queued_per_session"`
	WatchLeaseSeconds           int                      `mapstructure:"dispatcher_watch_lease_seconds"`
	RunnerTimeoutSeconds        int                      `mapstructure:"runner_request_timeout_seconds"`

	CentralSampler *dispatcher.CentralTarget `mapstructure:"central_sampler"`
}

// serviceDelegationConfig is a separate certificate actor grant scoped to exact methods.
type serviceDelegationConfig struct {
	Actors  []auth.ServiceDelegator `mapstructure:"actors"`
	Methods []string                `mapstructure:"methods"`
}

func defaultConfig() config {
	return config{
		RunnerControlTargetTemplate: sandboxapi.RuntimeProxyTargetTemplate(),
		QueueCap:                    1000, WatchLeaseSeconds: 300, RunnerTimeoutSeconds: 10,
	}
}

func loadConfig() (config, error) {
	path := strings.TrimSpace(os.Getenv(settingPrefix + "CONFIG_FILE"))
	if path == "" {
		return config{}, fmt.Errorf("%s is required", settingPrefix+"CONFIG_FILE")
	}
	c := defaultConfig()
	if _, err := configfile.DecodeMapstructureFile(path, &c, configfile.MapstructureOptions{
		DecodeHook: listener.AuthenticationDecodeHook,
	}); err != nil {
		return config{}, configfile.RedactMapstructureError("Runner Gateway", err)
	}
	c.DispatcherDatabaseURL = os.Getenv(dispatcherDatabaseURLEnv)
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

func (c *config) validate() error {
	serviceCallers := map[string][]string{
		"dispatcher_service_callers": c.DispatcherServiceCallers,
		"wake_service_callers":       c.WakeServiceCallers,
	}
	for name, callers := range serviceCallers {
		if err := auth.ValidateServiceCallers(callers); err != nil {
			return fmt.Errorf("%s: %w", name, err)
		}
	}
	if hasServiceCallers(serviceCallers) || c.DispatcherServiceDelegation != nil {
		if !c.RPC.Has(listener.SPIFFE) {
			return errors.New("service caller settings require rpc.auth to include spiffe")
		}
	}
	if c.DispatcherServiceDelegation != nil {
		if len(c.DispatcherServiceDelegation.Actors) == 0 || len(c.DispatcherServiceDelegation.Methods) == 0 {
			return errors.New("dispatcher_service_delegation requires actors and methods")
		}
		dispatcherMethods := dispatcher.Methods(auth.Rule{})
		for _, method := range c.DispatcherServiceDelegation.Methods {
			if _, ok := dispatcherMethods[method]; !ok {
				return fmt.Errorf("dispatcher_service_delegation method %q must belong to SessionDispatcher", method)
			}
		}
		var actors []string
		for _, actor := range c.DispatcherServiceDelegation.Actors {
			if actor.ConnectionProvenance {
				return errors.New("Dispatcher service delegation requires Action provenance")
			}
			actors = append(actors, actor.SPIFFEID)
		}
		if err := auth.ValidateServiceCallers(actors); err != nil {
			return err
		}
	}

	for _, required := range []struct{ name, value string }{
		{"sandbox_namespace", c.Namespace},
		{"policy_namespace", c.PolicyNamespace},
		{"dispatcher_database_url", c.DispatcherDatabaseURL},
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
	if err := c.RPC.Validate(); err != nil {
		return fmt.Errorf("rpc: %w", err)
	}
	// The shared listener decoder still accepts legacy modes; these receivers only use SPIFFE.
	if !c.RPC.Has(listener.SPIFFE) {
		return errors.New("rpc.auth must include spiffe")
	}
	if c.GatewayTLS == nil {
		return errors.New("gateway_tls is required for the Gateway's control-Service calls")
	}
	if err := c.GatewayTLS.Validate(); err != nil {
		return fmt.Errorf("gateway_tls: %w", err)
	}
	if c.GatewayTLS.ServerURI != peertls.SessionDispatcherURI && c.GatewayTLS.ServerURI != peertls.SandboxControllerURI {
		return errors.New("gateway_tls.server_uri must identify the Gateway or Environments Runtime proxy")
	}
	if err := c.GatewayTLS.ValidateCaller(c.RunnerTokenFile, peertls.SessionsURI); err != nil {
		return fmt.Errorf("gateway_tls caller authentication: %w", err)
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
	for _, service := range c.WakeServiceCallers {
		if !slices.Contains(c.DispatcherServiceCallers, service) {
			return fmt.Errorf("wake service caller %s is not a Dispatcher service caller", service)
		}
	}
	if target := c.CentralSampler; target != nil {
		if target.Target == "" {
			return errors.New("central_sampler requires a target")
		}
		if target.TLS != nil && target.TLS.ServerURI != peertls.CentralSamplerURI {
			return errors.New("central_sampler.tls.server_uri must identify the Central Sampler")
		}
		if err := target.TLS.ValidateCaller(target.TokenFile, peertls.SessionsURI); err != nil {
			return fmt.Errorf("central_sampler caller authentication: %w", err)
		}
	}

	return nil
}

func hasServiceCallers(callers map[string][]string) bool {
	for _, values := range callers {
		if len(values) > 0 {
			return true
		}
	}
	return false
}
