package main

import (
	"errors"
	"fmt"

	"github.com/spf13/viper"
	"k8s.io/apimachinery/pkg/util/validation"
)

// config is the Policy Controller's deployment contract: each field's key names an
// AGENTPLANE_POLICY_CONTROLLER_* environment variable, and a list is written as its elements joined by
// commas. Keep the keys in sync with the fields of settings.py, which the Deployment is rendered from.
type config struct {
	PolicyNamespace        string   `mapstructure:"policy_namespace"`
	SubjectNamespaces      []string `mapstructure:"subject_namespaces"`
	MetricsBindAddress     string   `mapstructure:"metrics_bind_address"`
	HealthProbeBindAddress string   `mapstructure:"health_probe_bind_address"`
}

func defaultConfig() config {
	return config{MetricsBindAddress: ":8080", HealthProbeBindAddress: ":8081"}
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
	return nil
}
