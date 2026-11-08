package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"gopkg.in/yaml.v3"
)

const configFileEnv = "AGENTPLANE_SANDBOX_CONTROLLER_CONFIG_FILE"

// config is the Sandbox Controller's deployment contract. Keep its YAML keys in sync with settings.py,
// which renders the Deployment's required settings file.
type config struct {
	Namespace               string `yaml:"namespace"`
	MetricsBindAddress      string `yaml:"metrics_bind_address"`
	HealthProbeBindAddress  string `yaml:"health_probe_bind_address"`
	RunnerGatewayTarget     string `yaml:"runner_gateway_target"`
	RunnerGatewayTokenFile  string `yaml:"runner_gateway_token_file"`
	RunnerCertificateIssuer string `yaml:"runner_certificate_issuer"`
}

func defaultConfig() config {
	return config{MetricsBindAddress: ":8080", HealthProbeBindAddress: ":8081"}
}

func loadConfig() (config, error) {
	path, ok := os.LookupEnv(configFileEnv)
	if !ok || strings.TrimSpace(path) == "" {
		return config{}, fmt.Errorf("%s is required", configFileEnv)
	}
	file, err := os.Open(path)
	if err != nil {
		return config{}, fmt.Errorf("open Sandbox Controller settings file: %w", err)
	}
	defer file.Close()

	c := defaultConfig()
	decoder := yaml.NewDecoder(file)
	decoder.KnownFields(true)
	if err := decoder.Decode(&c); err != nil {
		return config{}, fmt.Errorf("decode Sandbox Controller settings file: %w", err)
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		if err != nil {
			return config{}, fmt.Errorf("decode Sandbox Controller settings file: %w", err)
		}
		return config{}, errors.New("Sandbox Controller settings file must contain one YAML document")
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

func (c config) validate() error {
	for _, required := range []struct{ name, value string }{
		{"namespace", c.Namespace},
		{"metrics_bind_address", c.MetricsBindAddress},
		{"health_probe_bind_address", c.HealthProbeBindAddress},
		{"runner_gateway_target", c.RunnerGatewayTarget},
		{"runner_gateway_token_file", c.RunnerGatewayTokenFile},
		{"runner_certificate_issuer", c.RunnerCertificateIssuer},
	} {
		if strings.TrimSpace(required.value) == "" {
			return fmt.Errorf("%s is required", required.name)
		}
	}
	return nil
}
