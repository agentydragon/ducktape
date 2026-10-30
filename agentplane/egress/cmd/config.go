package main

import (
	"errors"
	"fmt"
	"net/netip"
	"os"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/go-viper/mapstructure/v2"
	"github.com/spf13/viper"
)

const settingPrefix = "AGENTPLANE_EGRESS_"

// config is the proxy's deployment contract: each setting is a YAML key of the file named by
// AGENTPLANE_EGRESS_CONFIG_FILE, with AGENTPLANE_EGRESS_* environment overrides for scalar
// settings. Lists and maps are file-only. Viper loads the settings.
//
// The admin listener serves the gRPC health service, and the admin API beside it when
// admin_grpc_audience, the audience of the bearer its callers send, and admin_grpc_callers, the
// ServiceAccounts it admits as `namespace/name`, are set.
type config struct {
	RulesNamespace                  string            `mapstructure:"rules_namespace"`
	CredentialsNamespace            string            `mapstructure:"credentials_namespace"`
	AllowedServiceAccountNamespaces []string          `mapstructure:"allowed_service_account_namespaces"`
	ListenAddress                   string            `mapstructure:"listen_address"`
	AdminListenAddress              string            `mapstructure:"admin_listen_address"`
	AdminGRPCAudience               string            `mapstructure:"admin_grpc_audience"`
	AdminGRPCCallers                []string          `mapstructure:"admin_grpc_callers"`
	AgentAPIListenAddress           string            `mapstructure:"agent_api_listen_address"`
	CACert                          string            `mapstructure:"ca_cert"`
	CAKey                           string            `mapstructure:"ca_key"`
	UpstreamCAFile                  string            `mapstructure:"upstream_ca_file"`
	TokenAudience                   string            `mapstructure:"token_audience"`
	ProjectedTokenAudiences         []string          `mapstructure:"projected_token_audiences"`
	Kubeconfig                      string            `mapstructure:"kubeconfig"`
	LogLevels                       map[string]string `mapstructure:"log_levels"`
	DatabaseURL                     string            `mapstructure:"database_url"`
	DecisionHistorySize             int               `mapstructure:"decision_history_size"`
	DecisionRetentionDays           int               `mapstructure:"decision_retention_days"`
	DecisionQueueSize               int               `mapstructure:"decision_queue_size"`
	DecisionBatchSize               int               `mapstructure:"decision_batch_size"`
	ExemptNetworks                  []netip.Prefix    `mapstructure:"exempt_networks"`
}

func defaultConfig() config {
	return config{
		ListenAddress: "0.0.0.0:8888", AdminListenAddress: "0.0.0.0:8081",
		AgentAPIListenAddress: "0.0.0.0:8082", DecisionHistorySize: 200,
		DecisionRetentionDays: 7, DecisionQueueSize: 2000, DecisionBatchSize: 100,
	}
}

func loadConfig() (config, error) {
	c := defaultConfig()
	settings := viper.New()
	settings.SetEnvPrefix("AGENTPLANE_EGRESS")
	for _, key := range []string{
		"rules_namespace",
		"credentials_namespace",
		"listen_address",
		"admin_listen_address",
		"admin_grpc_audience",
		"agent_api_listen_address",
		"ca_cert",
		"ca_key",
		"upstream_ca_file",
		"token_audience",
		"kubeconfig",
		"database_url",
		"decision_history_size",
		"decision_retention_days",
		"decision_queue_size",
		"decision_batch_size",
	} {
		settings.MustBindEnv(key)
	}
	if path := os.Getenv(settingPrefix + "CONFIG_FILE"); path != "" {
		settings.SetConfigFile(path)
		settings.SetConfigType("yaml")
		if err := settings.ReadInConfig(); err != nil {
			return config{}, errors.New("could not read egress settings file")
		}
	}
	if err := settings.UnmarshalExact(&c, viper.DecodeHook(mapstructure.TextUnmarshallerHookFunc())); err != nil {
		return config{}, errors.New("invalid egress settings")
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

// bounded is an integer setting and the inclusive range it must fall in.
type bounded struct {
	name          string
	value, lo, hi int
}

func (c *config) validate() error {
	for _, required := range []struct{ name, value string }{
		{"rules_namespace", c.RulesNamespace},
		{"credentials_namespace", c.CredentialsNamespace},
		{"ca_cert", c.CACert},
		{"ca_key", c.CAKey},
		{"token_audience", c.TokenAudience},
		{"database_url", c.DatabaseURL},
	} {
		if required.value == "" {
			return fmt.Errorf("%s is required", required.name)
		}
	}
	if len(c.AllowedServiceAccountNamespaces) == 0 || slices.Contains(c.AllowedServiceAccountNamespaces, "") {
		return errors.New("allowed_service_account_namespaces must list at least one namespace, none of them empty")
	}
	if slices.Contains(c.ProjectedTokenAudiences, "") {
		return errors.New("projected_token_audiences must not contain an empty audience")
	}
	for _, limit := range []bounded{
		{"decision_history_size", c.DecisionHistorySize, 1, history.MaxCapacity},
		{"decision_retention_days", c.DecisionRetentionDays, 1, 365},
		{"decision_queue_size", c.DecisionQueueSize, 1, 100000},
		{"decision_batch_size", c.DecisionBatchSize, 1, 1000},
	} {
		if limit.value < limit.lo || limit.value > limit.hi {
			return fmt.Errorf("%s must be between %d and %d", limit.name, limit.lo, limit.hi)
		}
	}
	if err := c.validateAdminGRPC(); err != nil {
		return err
	}
	for logger, level := range c.LogLevels {
		if logger != "root" {
			return errors.New(`log_levels may only name "root"`)
		}
		if _, err := logLevel(level); err != nil {
			return err
		}
	}
	return nil
}
