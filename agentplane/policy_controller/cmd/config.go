package main

import (
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/spf13/viper"
	"k8s.io/apimachinery/pkg/util/validation"
)

// config is the Policy Controller's deployment contract: each field's key names an
// AGENTPLANE_POLICY_CONTROLLER_* environment variable, and a list is written as its elements joined by
// commas. Keep the keys in sync with the fields of settings.py, which the Deployment is rendered from.
type config struct {
	PolicyNamespace        string   `mapstructure:"policy_namespace"`
	SubjectNamespaces      []string `mapstructure:"subject_namespaces"`
	AccountsNamespace      string   `mapstructure:"accounts_namespace"`
	MetricsBindAddress     string   `mapstructure:"metrics_bind_address"`
	HealthProbeBindAddress string   `mapstructure:"health_probe_bind_address"`
	GRPCBindAddress        string   `mapstructure:"grpc_bind_address"`
	GRPCAudience           string   `mapstructure:"grpc_audience"`
	ReaderAccounts         []string `mapstructure:"reader_accounts"`
	WriterAccounts         []string `mapstructure:"writer_accounts"`
	WorkloadIssuer         string   `mapstructure:"workload_issuer"`
	WorkloadNamespaces     []string `mapstructure:"workload_namespaces"`
}

func defaultConfig() config {
	return config{MetricsBindAddress: ":8080", HealthProbeBindAddress: ":8081", GRPCBindAddress: ":8082", GRPCAudience: "agentplane-policy"}
}

func loadConfig() (config, error) {
	c := defaultConfig()
	// Without ExperimentalBindStruct, Unmarshal reads an environment variable only for a bound key.
	settings := viper.NewWithOptions(viper.ExperimentalBindStruct())
	settings.SetEnvPrefix("AGENTPLANE_POLICY_CONTROLLER")
	settings.AutomaticEnv()
	if err := settings.UnmarshalExact(&c); err != nil {
		return config{}, errors.New("invalid Policy Controller settings")
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
