// Package serving composes EnvironmentService independently of leader-only reconciliation.
package serving

import (
	"errors"
	"fmt"
	"slices"

	environmentservice "github.com/agentydragon/ducktape/agentplane/environments/internal/service"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
)

// Config is opt-in until the accepting Controller image is pinned before deployment settings.
type Config struct {
	RPC                listener.Config          `mapstructure:"rpc"`
	PolicyNamespace    string                   `mapstructure:"policy_namespace"`
	Audience           string                   `mapstructure:"audience"`
	Readers            []auth.Account           `mapstructure:"readers"`
	Managers           []auth.Account           `mapstructure:"managers"`
	Launchers          []auth.Account           `mapstructure:"launchers"`
	Operators          []auth.Account           `mapstructure:"operators"`
	ServiceReaders     []string                 `mapstructure:"service_readers"`
	ServiceManagers    []string                 `mapstructure:"service_managers"`
	ServiceLaunchers   []string                 `mapstructure:"service_launchers"`
	ServiceOperators   []string                 `mapstructure:"service_operators"`
	WakeCallers        []auth.Account           `mapstructure:"wake_callers"`
	WakeServiceCallers []string                 `mapstructure:"wake_service_callers"`
	ServiceDelegation  *ServiceDelegationConfig `mapstructure:"service_delegation"`
	Delegation         *DelegationConfig        `mapstructure:"delegation"`
	RuntimeCatalog     runtimeconfig.Catalog    `mapstructure:"runtime_catalog"`
}

type ServiceDelegationConfig struct {
	Actors  []auth.ServiceDelegator `mapstructure:"actors"`
	Methods []string                `mapstructure:"methods"`
}

type DelegationConfig struct {
	Actor           auth.Account `mapstructure:"actor"`
	IdentityCallers bool         `mapstructure:"identity_callers"`
	Methods         []string     `mapstructure:"methods"`
}

func (c Config) Validate() error {
	if c.PolicyNamespace == "" || c.Audience == "" {
		return errors.New("policy_namespace and audience are required")
	}
	if err := c.RPC.Validate(); err != nil {
		return fmt.Errorf("rpc: %w", err)
	}
	if !c.RPC.Has(listener.SPIFFE) {
		return errors.New("EnvironmentService rpc.auth must include spiffe")
	}
	for _, method := range c.RPC.Auth {
		if bearer, ok := method.(*listener.KubernetesBearerAuth); ok &&
			(len(bearer.Audiences) != 1 || bearer.Audiences[0] != c.Audience) {
			return errors.New("EnvironmentService bearer audience must equal audience")
		}
	}
	for _, accounts := range [][]auth.Account{c.Readers, c.Managers, c.Launchers, c.Operators, c.WakeCallers} {
		for _, account := range accounts {
			if account.Namespace == "" || account.Name == "" {
				return errors.New("EnvironmentService accounts require namespace and name")
			}
		}
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
	if d := c.Delegation; d != nil {
		if d.Actor.Namespace == "" || d.Actor.Name == "" || !d.IdentityCallers || len(d.Methods) == 0 {
			return errors.New("delegation requires actor, identity_callers and methods")
		}
		methods := environmentservice.Methods(nil)
		for _, method := range d.Methods {
			if _, ok := methods[method]; !ok {
				return fmt.Errorf("delegation method %q must belong to EnvironmentService", method)
			}
		}
	}
	return nil
}
