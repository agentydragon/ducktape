package main

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/configfile"
	"github.com/agentydragon/ducktape/agentplane/environments/internal/serving"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
)

const configFileEnv = "AGENTPLANE_ENVIRONMENTS_CONFIG_FILE"

// config is the Sandbox Controller's deployment contract. Keep its YAML keys in sync with settings.py,
// which renders the Deployment's required settings file.
type config struct {
	EnvironmentService *serving.Config `mapstructure:"environment_service"`
	// PolicyService, when set, supplies EnvironmentService's Identities through PolicyService.WatchIdentities instead
	// of reading Identity resources. The reconciler keeps reading Identity resources either way.
	// CLEANUP(added 2026-11-26): require it and drop EnvironmentService's Kubernetes Identity reads once the staging
	//   manifests set it (agentplane/plans/task_dag.md IDENTITY_READERS_BEHIND_POLICY_SERVICE).
	PolicyService          *identitycache.Peer `mapstructure:"policy_service"`
	RunnerGatewayTLS       *peertls.ClientTLS  `mapstructure:"runner_gateway_tls"`
	RunnerSecurity         peertls.Security    `mapstructure:"runner_security"`
	Namespace              string              `mapstructure:"namespace"`
	MetricsBindAddress     string              `mapstructure:"metrics_bind_address"`
	HealthProbeBindAddress string              `mapstructure:"health_probe_bind_address"`
	RunnerGatewayTarget    string              `mapstructure:"runner_gateway_target"`
	RunnerGatewayTokenFile string              `mapstructure:"runner_gateway_token_file"`
}

func defaultConfig() config {
	return config{MetricsBindAddress: ":8080", HealthProbeBindAddress: ":8081"}
}

func loadConfig() (config, error) {
	path := os.Getenv(configFileEnv)
	if strings.TrimSpace(path) == "" {
		return config{}, fmt.Errorf("%s is required", configFileEnv)
	}
	c := defaultConfig()
	if _, err := configfile.DecodeMapstructureFile(path, &c, configfile.MapstructureOptions{
		ExactFieldNames: true,
	}); err != nil {
		return config{}, fmt.Errorf("decode Sandbox Controller settings file: %w", err)
	}
	if c.EnvironmentService != nil {
		c.EnvironmentService.RuntimeCatalog.Models.NormalizeHarnessNames()
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

func (c config) validate() error {
	if c.EnvironmentService != nil {
		if err := c.EnvironmentService.Validate(); err != nil {
			return fmt.Errorf("environment_service: %w", err)
		}
	}
	if c.PolicyService != nil {
		if c.EnvironmentService == nil {
			return errors.New("policy_service requires environment_service, its only reader")
		}
		if err := c.PolicyService.Validate(peertls.SandboxControllerURI); err != nil {
			return err
		}
	}
	if err := c.RunnerSecurity.Validate(); err != nil {
		return fmt.Errorf("runner_security: %w", err)
	}
	if err := c.RunnerGatewayTLS.ValidateCaller(c.RunnerGatewayTokenFile, peertls.SandboxControllerURI); err != nil {
		return fmt.Errorf("runner_gateway caller authentication: %w", err)
	}
	for _, required := range []struct{ name, value string }{
		{"namespace", c.Namespace},
		{"metrics_bind_address", c.MetricsBindAddress},
		{"health_probe_bind_address", c.HealthProbeBindAddress},
		{"runner_gateway_target", c.RunnerGatewayTarget},
	} {
		if strings.TrimSpace(required.value) == "" {
			return fmt.Errorf("%s is required", required.name)
		}
	}
	return nil
}
