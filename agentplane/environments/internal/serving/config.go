// Package serving composes EnvironmentService independently of leader-only reconciliation.
package serving

import (
	"errors"
	"fmt"
	"slices"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
)

// Config is opt-in until the accepting Controller image is pinned before deployment settings.
type Config struct {
	RPC                listener.Config          `mapstructure:"rpc"`
	PolicyNamespace    string                   `mapstructure:"policy_namespace"`
	ServiceReaders     []string                 `mapstructure:"service_readers"`
	ServiceManagers    []string                 `mapstructure:"service_managers"`
	ServiceLaunchers   []string                 `mapstructure:"service_launchers"`
	ServiceOperators   []string                 `mapstructure:"service_operators"`
	WakeServiceCallers []string                 `mapstructure:"wake_service_callers"`
	ServiceDelegation  *ServiceDelegationConfig `mapstructure:"service_delegation"`
	RuntimeProxy       *RuntimeProxyConfig      `mapstructure:"runtime_proxy"`
	RuntimeCatalog     runtimeconfig.Catalog    `mapstructure:"runtime_catalog"`
}

type ServiceDelegationConfig struct {
	Actors  []auth.ServiceDelegator `mapstructure:"actors"`
	Methods []string                `mapstructure:"methods"`
}

func (c Config) Validate() error {
	if c.PolicyNamespace == "" {
		return errors.New("policy_namespace is required")
	}
	if c.RuntimeProxy != nil {
		if err := c.RuntimeProxy.Validate(); err != nil {
			return fmt.Errorf("runtime_proxy: %w", err)
		}
	}
	if err := c.RPC.Validate(); err != nil {
		return fmt.Errorf("rpc: %w", err)
	}
	for _, callers := range [][]string{c.ServiceReaders, c.ServiceManagers, c.ServiceLaunchers, c.ServiceOperators, c.WakeServiceCallers} {
		if err := auth.ValidateServiceCallers(callers); err != nil {
			return err
		}
	}
	if d := c.ServiceDelegation; d != nil {
		if len(d.Actors) == 0 || len(d.Methods) == 0 {
			return errors.New("service_delegation requires actors and methods")
		}
		allowed := []string{
			environmentpb.EnvironmentService_ListEnvironments_FullMethodName,
			environmentpb.EnvironmentService_WatchEnvironments_FullMethodName,
			environmentpb.EnvironmentService_GetEnvironment_FullMethodName,
			environmentpb.EnvironmentService_WatchEnvironment_FullMethodName,
			environmentpb.EnvironmentService_ResumeEnvironment_FullMethodName,
		}
		for _, method := range d.Methods {
			if !slices.Contains(allowed, method) {
				return fmt.Errorf("service_delegation method %q is not delegatable", method)
			}
		}
		for _, actor := range d.Actors {
			if actor.ConnectionProvenance {
				return errors.New("EnvironmentService delegation requires Action provenance")
			}
			if err := auth.ValidateServiceCallers([]string{actor.SPIFFEID}); err != nil {
				return err
			}
		}
	}

	return nil
}
