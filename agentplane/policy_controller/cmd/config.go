package main

import (
	"errors"
	"fmt"
	"os"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/configfile"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	"k8s.io/apimachinery/pkg/util/validation"
)

const configFileEnv = "AGENTPLANE_POLICY_CONTROLLER_CONFIG_FILE"

// config is the Policy Controller's complete non-secret deployment contract. The required YAML
// file is authored through settings.py; service settings have no environment overrides.
type config struct {
	PolicyNamespace        string                     `yaml:"policy_namespace"`
	SubjectNamespaces      []string                   `yaml:"subject_namespaces"`
	AccountsNamespace      string                     `yaml:"accounts_namespace"`
	MetricsBindAddress     string                     `yaml:"metrics_bind_address"`
	HealthProbeBindAddress string                     `yaml:"health_probe_bind_address"`
	GRPCTLS                *peertls.ServerTLSListener `yaml:"grpc_tls"`
	ReaderServices         []string                   `yaml:"reader_services"`
	WriterServices         []string                   `yaml:"writer_services"`
	WorkloadIssuer         string                     `yaml:"workload_issuer"`
	WorkloadNamespaces     []string                   `yaml:"workload_namespaces"`
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
	if err := configfile.DecodeKnownFieldsFile(path, &c); err != nil {
		var pathError *os.PathError
		switch {
		case errors.As(err, &pathError):
			return config{}, errors.New("could not read Policy Controller settings file")
		case errors.Is(err, configfile.ErrMultipleDocuments):
			return config{}, errors.New("Policy Controller settings file must contain one YAML document")
		default:
			return config{}, errors.New("invalid Policy Controller settings")
		}
	}
	return c, c.validate()
}

func (c config) validate() error {
	if c.GRPCTLS == nil {
		return errors.New("grpc_tls is required")
	}
	if err := c.GRPCTLS.Validate(); err != nil {
		return fmt.Errorf("grpc_tls: %w", err)
	}
	if c.GRPCTLS.Listen == c.MetricsBindAddress || c.GRPCTLS.Listen == c.HealthProbeBindAddress {
		return errors.New("grpc_tls.listen must differ from metrics and health listeners")
	}
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
	for _, callers := range [][]string{c.ReaderServices, c.WriterServices} {
		if err := auth.ValidateServiceCallers(callers); err != nil {
			return fmt.Errorf("invalid PolicyService service allowlist")
		}
	}
	if (len(c.ReaderServices) > 0 || len(c.WriterServices) > 0) && c.GRPCTLS.ClientCAFile == "" {
		return errors.New("PolicyService service allowlists require grpc_tls.client_ca_file")
	}
	return nil
}
