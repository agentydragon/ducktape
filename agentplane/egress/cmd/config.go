package main

import (
	"errors"
	"fmt"
	"net/netip"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/agentydragon/ducktape/util/settings"
)

const settingPrefix = "AGENTPLANE_EGRESS_"

// config is the proxy's deployment contract: each setting is a YAML key of the file named by
// AGENTPLANE_EGRESS_CONFIG_FILE, with AGENTPLANE_EGRESS_* environment overrides for scalar
// settings. Lists and maps are file-only. Settings are loaded by util/settings.
type config struct {
	RulesNamespace                  string            `yaml:"rules_namespace"`
	CredentialsNamespace            string            `yaml:"credentials_namespace"`
	AllowedServiceAccountNamespaces []string          `env:"-" yaml:"allowed_service_account_namespaces"`
	ListenHost                      string            `yaml:"listen_host"`
	ListenPort                      int               `yaml:"listen_port"`
	AdminHost                       string            `yaml:"admin_host"`
	AdminPort                       int               `yaml:"admin_port"`
	AgentAPIHost                    string            `yaml:"agent_api_host"`
	AgentAPIPort                    int               `yaml:"agent_api_port"`
	CACert                          string            `yaml:"ca_cert"`
	CAKey                           string            `yaml:"ca_key"`
	UpstreamCAFile                  string            `yaml:"upstream_ca_file"`
	TokenAudience                   string            `yaml:"token_audience"`
	ProjectedTokenAudiences         []string          `env:"-" yaml:"projected_token_audiences"`
	Kubeconfig                      string            `yaml:"kubeconfig"`
	LogLevels                       map[string]string `env:"-" yaml:"log_levels"`
	ResyncSeconds                   int               `yaml:"resync_seconds"`
	DatabaseURL                     string            `yaml:"database_url"`
	DecisionHistorySize             int               `yaml:"decision_history_size"`
	DecisionRetentionDays           int               `yaml:"decision_retention_days"`
	DecisionQueueSize               int               `yaml:"decision_queue_size"`
	DecisionBatchSize               int               `yaml:"decision_batch_size"`
	ExemptNetworks                  []netip.Prefix    `env:"-" yaml:"exempt_networks"`
}

func defaultConfig() config {
	return config{
		ListenHost: "0.0.0.0", ListenPort: 8888, AdminHost: "0.0.0.0", AdminPort: 8081,
		AgentAPIHost: "0.0.0.0", AgentAPIPort: 8082, ResyncSeconds: 300, DecisionHistorySize: 200,
		DecisionRetentionDays: 7, DecisionQueueSize: 2000, DecisionBatchSize: 100,
	}
}

func loadConfig(environ []string) (config, error) {
	c, err := settings.Load(settings.Options{Prefix: settingPrefix}, defaultConfig(), environ)
	if err != nil {
		return config{}, err
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
