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
	ListenHost                      string            `mapstructure:"listen_host"`
	ListenPort                      int               `mapstructure:"listen_port"`
	AdminHost                       string            `mapstructure:"admin_host"`
	AdminPort                       int               `mapstructure:"admin_port"`
	AdminGRPCAudience               string            `mapstructure:"admin_grpc_audience"`
	AdminGRPCCallers                []string          `mapstructure:"admin_grpc_callers"`
	AgentAPIHost                    string            `mapstructure:"agent_api_host"`
	AgentAPIPort                    int               `mapstructure:"agent_api_port"`
	CACert                          string            `mapstructure:"ca_cert"`
	CAKey                           string            `mapstructure:"ca_key"`
	UpstreamCAFile                  string            `mapstructure:"upstream_ca_file"`
	TokenAudience                   string            `mapstructure:"token_audience"`
	ProjectedTokenAudiences         []string          `mapstructure:"projected_token_audiences"`
	Kubeconfig                      string            `mapstructure:"kubeconfig"`
	LogLevels                       map[string]string `mapstructure:"log_levels"`
	ResyncSeconds                   int               `mapstructure:"resync_seconds"`
	DatabaseURL                     string            `mapstructure:"database_url"`
	DecisionHistorySize             int               `mapstructure:"decision_history_size"`
	DecisionRetentionDays           int               `mapstructure:"decision_retention_days"`
	DecisionQueueSize               int               `mapstructure:"decision_queue_size"`
	DecisionBatchSize               int               `mapstructure:"decision_batch_size"`
	ExemptNetworks                  []netip.Prefix    `mapstructure:"exempt_networks"`
}

func defaultConfig() config {
	return config{
		ListenHost: "0.0.0.0", ListenPort: 8888, AdminHost: "0.0.0.0", AdminPort: 8081,
		AgentAPIHost: "0.0.0.0", AgentAPIPort: 8082, ResyncSeconds: 300, DecisionHistorySize: 200,
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
		"listen_host",
		"listen_port",
		"admin_host",
		"admin_port",
		"admin_grpc_audience",
		"agent_api_host",
		"agent_api_port",
		"ca_cert",
		"ca_key",
		"upstream_ca_file",
		"token_audience",
		"kubeconfig",
		"resync_seconds",
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
		{"listen_port", c.ListenPort, 0, 65535},
		{"admin_port", c.AdminPort, 0, 65535},
		{"agent_api_port", c.AgentAPIPort, 0, 65535},
		// client-go relists a watch shorter than a second, so the informer takes two at the least.
		{"resync_seconds", c.ResyncSeconds, 2, 86400},
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
