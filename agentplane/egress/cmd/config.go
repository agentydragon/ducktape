package main

import (
	"errors"
	"fmt"
	"io"
	"net/netip"
	"os"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/egress/history"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
	"github.com/go-viper/mapstructure/v2"
	"gopkg.in/yaml.v3"
)

const (
	configFileEnv  = "AGENTPLANE_EGRESS_CONFIG_FILE"
	databaseURLEnv = "AGENTPLANE_EGRESS_DATABASE_URL"
)

// config is the proxy's complete, non-secret deployment contract. It comes from the required
// YAML file named by configFileEnv. DatabaseURL is supplied separately from a Secret-backed
// environment variable and is deliberately excluded from the file decoder.
//
// The gRPC listener serves rules and health, and the admin API beside them when
// admin_grpc_audience, the audience of the bearer its callers send, and admin_grpc_callers, the
// ServiceAccounts it admits as `namespace/name`, are set.
//
// local_api_server is where kubernetesServiceAccount credentials naming the `local` API server act,
// and the token file holding the proxy's own bearer for it; unset, those credentials are unavailable.
type config struct {
	RulesNamespace                  string            `mapstructure:"rules_namespace"`
	CredentialsNamespace            string            `mapstructure:"credentials_namespace"`
	AllowedServiceAccountNamespaces []string          `mapstructure:"allowed_service_account_namespaces"`
	Forward                         listener.Config   `mapstructure:"forward"`
	TLSPassthroughTargets           []string          `mapstructure:"tls_passthrough_targets"`
	AdminListenAddress              string            `mapstructure:"admin_listen_address"`
	AdminGRPCAudience               string            `mapstructure:"admin_grpc_audience"`
	AdminGRPCCallers                []string          `mapstructure:"admin_grpc_callers"`
	CACert                          string            `mapstructure:"ca_cert"`
	CAKey                           string            `mapstructure:"ca_key"`
	UpstreamCAFile                  string            `mapstructure:"upstream_ca_file"`
	Kubeconfig                      string            `mapstructure:"kubeconfig"`
	LogLevels                       map[string]string `mapstructure:"log_levels"`
	DatabaseURL                     string            `mapstructure:"-"`
	DecisionHistorySize             int               `mapstructure:"decision_history_size"`
	DecisionRetentionDays           int               `mapstructure:"decision_retention_days"`
	DecisionQueueSize               int               `mapstructure:"decision_queue_size"`
	DecisionBatchSize               int               `mapstructure:"decision_batch_size"`
	ExemptNetworks                  []netip.Prefix    `mapstructure:"exempt_networks"`
	LocalAPIServer                  *apiServerConfig  `mapstructure:"local_api_server"`
}

// apiServerConfig is an API server the proxy impersonates ServiceAccounts on: the host and port
// requests to it name, and the file holding the proxy's bearer for it, read on every use.
type apiServerConfig struct {
	Host      string `mapstructure:"host"`
	Port      int    `mapstructure:"port"`
	TokenFile string `mapstructure:"token_file"`
}

func defaultConfig() config {
	return config{
		AdminListenAddress: "0.0.0.0:8081", DecisionHistorySize: 200,
		DecisionRetentionDays: 7, DecisionQueueSize: 2000, DecisionBatchSize: 100,
	}
}

func loadConfig() (config, error) {
	path, ok := os.LookupEnv(configFileEnv)
	if !ok || strings.TrimSpace(path) == "" {
		return config{}, fmt.Errorf("%s is required", configFileEnv)
	}
	file, err := os.Open(path)
	if err != nil {
		return config{}, fmt.Errorf("open egress settings file: %w", err)
	}
	defer file.Close()

	c := defaultConfig()
	decoder := yaml.NewDecoder(file)
	var settings map[string]any
	if err := decoder.Decode(&settings); err != nil {
		return config{}, fmt.Errorf("decode egress settings file: %w", err)
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		if err != nil {
			return config{}, fmt.Errorf("decode egress settings file: %w", err)
		}
		return config{}, errors.New("egress settings file must contain one YAML document")
	}
	settingsDecoder, err := mapstructure.NewDecoder(&mapstructure.DecoderConfig{
		Result: &c, TagName: "mapstructure", ErrorUnused: true,
		MatchName: func(mapKey, fieldName string) bool { return mapKey == fieldName },
		DecodeHook: mapstructure.ComposeDecodeHookFunc(
			mapstructure.TextUnmarshallerHookFunc(), listener.AuthenticationDecodeHook,
		),
	})
	if err != nil {
		return config{}, fmt.Errorf("prepare egress settings decoder: %w", err)
	}
	if err := settingsDecoder.Decode(settings); err != nil {
		return config{}, fmt.Errorf("decode egress settings file: %w", err)
	}
	c.DatabaseURL = os.Getenv(databaseURLEnv)
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
		{"database_url", c.DatabaseURL},
	} {
		if strings.TrimSpace(required.value) == "" {
			return fmt.Errorf("%s is required", required.name)
		}
	}
	if len(c.AllowedServiceAccountNamespaces) == 0 || slices.Contains(c.AllowedServiceAccountNamespaces, "") {
		return errors.New("allowed_service_account_namespaces must list at least one namespace, none of them empty")
	}
	if err := c.Forward.Validate(); err != nil {
		return fmt.Errorf("forward listener: %w", err)
	}
	if c.Forward.TLS == nil {
		return errors.New("forward listener requires TLS")
	}
	if !c.Forward.Has(listener.SPIFFE) || c.Forward.Has(listener.KubernetesBearer) {
		return errors.New("forward listener authenticates SPIFFE alone")
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
	if server := c.LocalAPIServer; server != nil &&
		(server.Host == "" || server.TokenFile == "" || server.Port < 1 || server.Port > 65535) {
		return errors.New("local_api_server needs a host, a port between 1 and 65535 and a token_file")
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
