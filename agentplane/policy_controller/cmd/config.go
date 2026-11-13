package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"gopkg.in/yaml.v3"
	"k8s.io/apimachinery/pkg/util/validation"
)

const configFileEnv = "AGENTPLANE_POLICY_CONTROLLER_CONFIG_FILE"

// config is the Policy Controller's complete non-secret deployment contract. The required YAML
// file is authored through settings.py; service settings have no environment overrides.
type config struct {
	PolicyNamespace        string   `yaml:"policy_namespace"`
	SubjectNamespaces      []string `yaml:"subject_namespaces"`
	AccountsNamespace      string   `yaml:"accounts_namespace"`
	MetricsBindAddress     string   `yaml:"metrics_bind_address"`
	HealthProbeBindAddress string   `yaml:"health_probe_bind_address"`
	GRPCBindAddress        string   `yaml:"grpc_bind_address"`
	GRPCAudience           string   `yaml:"grpc_audience"`
	ReaderAccounts         []string `yaml:"reader_accounts"`
	WriterAccounts         []string `yaml:"writer_accounts"`
	WorkloadIssuer         string   `yaml:"workload_issuer"`
	WorkloadNamespaces     []string `yaml:"workload_namespaces"`
}

func defaultConfig() config {
	return config{MetricsBindAddress: ":8080", HealthProbeBindAddress: ":8081", GRPCBindAddress: ":8082", GRPCAudience: "agentplane-policy"}
}

func loadConfig() (config, error) {
	path := os.Getenv(configFileEnv)
	if strings.TrimSpace(path) == "" {
		return config{}, fmt.Errorf("%s is required", configFileEnv)
	}
	file, err := os.Open(path)
	if err != nil {
		return config{}, errors.New("could not read Policy Controller settings file")
	}
	defer file.Close()

	c := defaultConfig()
	decoder := yaml.NewDecoder(file)
	decoder.KnownFields(true)
	if err := decoder.Decode(&c); err != nil {
		return config{}, errors.New("invalid Policy Controller settings")
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return config{}, errors.New("Policy Controller settings file must contain one YAML document")
	}
	return c, c.validate()
}

func (c config) validate() error {
	for _, setting := range []struct {
		name       string
		namespaces []string
	}{
		{"policy_namespace", []string{c.PolicyNamespace}},
		{"subject_namespaces", c.SubjectNamespaces},
		{"accounts_namespace", []string{c.AccountsNamespace}},
	} {
		if len(setting.namespaces) == 0 {
			return fmt.Errorf("%s is required", setting.name)
		}
		for _, namespace := range setting.namespaces {
			if len(validation.IsDNS1123Label(namespace)) > 0 {
				return fmt.Errorf("%s holds an invalid namespace name", setting.name)
			}
		}
	}
	if (c.WorkloadIssuer == "") != (len(c.WorkloadNamespaces) == 0) {
		return errors.New("workload_issuer and workload_namespaces are set together")
	}
	for _, namespace := range c.WorkloadNamespaces {
		if len(validation.IsDNS1123Label(namespace)) > 0 {
			return errors.New("workload_namespaces holds an invalid namespace name")
		}
	}
	for _, name := range append(append([]string(nil), c.ReaderAccounts...), c.WriterAccounts...) {
		var account auth.Account
		if err := account.UnmarshalText([]byte(name)); err != nil {
			return fmt.Errorf("invalid PolicyService account allowlist")
		}
	}
	return nil
}
