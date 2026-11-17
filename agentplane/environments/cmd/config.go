package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/environments/internal/serving"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"github.com/go-viper/mapstructure/v2"

	"gopkg.in/yaml.v3"
)

const (
	configFileEnv        = "AGENTPLANE_SANDBOX_CONTROLLER_CONFIG_FILE"
	renamedConfigFileEnv = "AGENTPLANE_ENVIRONMENTS_CONFIG_FILE"
)

// config is the Sandbox Controller's deployment contract. Keep its YAML keys in sync with settings.py,
// which renders the Deployment's required settings file.
type config struct {
	EnvironmentService     *serving.Config    `mapstructure:"environment_service"`
	RunnerGatewayTLS       *peertls.ClientTLS `mapstructure:"runner_gateway_tls"`
	RunnerSecurity         peertls.Security   `mapstructure:"runner_security"`
	Namespace              string             `mapstructure:"namespace"`
	MetricsBindAddress     string             `mapstructure:"metrics_bind_address"`
	HealthProbeBindAddress string             `mapstructure:"health_probe_bind_address"`
	RunnerGatewayTarget    string             `mapstructure:"runner_gateway_target"`
	RunnerGatewayTokenFile string             `mapstructure:"runner_gateway_token_file"`
}

func defaultConfig() config {
	return config{MetricsBindAddress: ":8080", HealthProbeBindAddress: ":8081"}
}

func loadConfig() (config, error) {
	// CLEANUP(added 2026-11-17): Remove the old bootstrap name after the operator's
	// last-caller census certifies that old Deployments and rollback consumers are gone.
	path, renamedPath := os.Getenv(configFileEnv), os.Getenv(renamedConfigFileEnv)
	if path != "" && renamedPath != "" && path != renamedPath {
		return config{}, fmt.Errorf("%s and %s conflict", configFileEnv, renamedConfigFileEnv)
	}
	if renamedPath != "" {
		path = renamedPath
	}
	if strings.TrimSpace(path) == "" {
		return config{}, fmt.Errorf("%s is required", configFileEnv)
	}
	file, err := os.Open(path)
	if err != nil {
		return config{}, fmt.Errorf("open Sandbox Controller settings file: %w", err)
	}
	defer file.Close()

	c := defaultConfig()
	decoder := yaml.NewDecoder(file)
	var settings map[string]any
	if err := decoder.Decode(&settings); err != nil {
		return config{}, fmt.Errorf("decode Sandbox Controller settings file: %w", err)
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		if err != nil {
			return config{}, fmt.Errorf("decode Sandbox Controller settings file: %w", err)
		}
		return config{}, errors.New("Sandbox Controller settings file must contain one YAML document")
	}
	strict, err := mapstructure.NewDecoder(&mapstructure.DecoderConfig{
		Result: &c, TagName: "mapstructure", ErrorUnused: true,
		DecodeHook: mapstructure.ComposeDecodeHookFunc(mapstructure.TextUnmarshallerHookFunc(), listener.AuthenticationDecodeHook),
		MatchName:  func(mapKey, fieldName string) bool { return mapKey == fieldName },
	})
	if err != nil {
		return config{}, err
	}
	if err := strict.Decode(settings); err != nil {
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
