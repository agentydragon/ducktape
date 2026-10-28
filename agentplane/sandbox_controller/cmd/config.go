package main

import (
	"errors"
	"fmt"

	"github.com/spf13/viper"
)

// config is the Sandbox Controller's deployment contract: each field's key names an
// AGENTPLANE_SANDBOX_CONTROLLER_* environment variable. No setting is a list, so there is no settings
// file. Keep the keys in sync with the fields of settings.py, which the Deployment is rendered from.
type config struct {
	Namespace               string `mapstructure:"namespace"`
	MetricsBindAddress      string `mapstructure:"metrics_bind_address"`
	HealthProbeBindAddress  string `mapstructure:"health_probe_bind_address"`
	RunnerGatewayTarget     string `mapstructure:"runner_gateway_target"`
	RunnerGatewayTokenFile  string `mapstructure:"runner_gateway_token_file"`
	RunnerCertificateIssuer string `mapstructure:"runner_certificate_issuer"`
}

func defaultConfig() config {
	return config{MetricsBindAddress: ":8080", HealthProbeBindAddress: ":8081"}
}

func loadConfig() (config, error) {
	c := defaultConfig()
	// Without ExperimentalBindStruct, Unmarshal reads an environment variable only for a bound key.
	settings := viper.NewWithOptions(viper.ExperimentalBindStruct())
	settings.SetEnvPrefix("AGENTPLANE_SANDBOX_CONTROLLER")
	settings.AutomaticEnv()
	if err := settings.UnmarshalExact(&c); err != nil {
		return config{}, errors.New("invalid Sandbox Controller settings")
	}
	return c, c.validate()
}

func (c config) validate() error {
	for _, required := range []struct{ name, value string }{
		{"namespace", c.Namespace},
		{"runner_gateway_target", c.RunnerGatewayTarget},
		{"runner_gateway_token_file", c.RunnerGatewayTokenFile},
		{"runner_certificate_issuer", c.RunnerCertificateIssuer},
	} {
		if required.value == "" {
			return fmt.Errorf("%s is required", required.name)
		}
	}
	return nil
}
