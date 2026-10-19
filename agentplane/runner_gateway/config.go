package main

import (
	"errors"
	"fmt"
	"os"
	"slices"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/go-viper/mapstructure/v2"
	"github.com/spf13/viper"
)

const settingPrefix = "AGENTPLANE_RUNNER_GATEWAY_"

// config is the Gateway's deployment contract: each setting is a YAML key of the file named by
// AGENTPLANE_RUNNER_GATEWAY_CONFIG_FILE, with AGENTPLANE_RUNNER_GATEWAY_* environment overrides for
// scalar settings. Lists are file-only. Viper loads the settings. Callers and the controller account
// are written `namespace/name`.
//
// The database URL is a Secret and the Pod IP comes from the Downward API, so both are environment
// values. Kubernetes sets the Pod hostname to its name unless an explicit hostname is configured.
type config struct {
	Namespace                   string         `mapstructure:"sandbox_namespace"`
	DispatcherDatabaseURL       string         `mapstructure:"dispatcher_database_url"`
	PodName                     string         `mapstructure:"pod_name"`
	PodIP                       string         `mapstructure:"pod_ip"`
	RunnerTokenFile             string         `mapstructure:"runner_token_file"`
	Kubeconfig                  string         `mapstructure:"kubeconfig"`
	ListenAddress               string         `mapstructure:"listen_address"`
	HTTPListenAddress           string         `mapstructure:"http_listen_address"`
	ProxyPort                   int            `mapstructure:"proxy_port"`
	RunnerControlTargetTemplate string         `mapstructure:"runner_control_target_template"`
	DispatcherAudience          string         `mapstructure:"dispatcher_audience"`
	GatewayAudience             string         `mapstructure:"gateway_audience"`
	RunnerAudience              string         `mapstructure:"runner_audience"`
	DispatcherCallers           []auth.Account `mapstructure:"dispatcher_callers"`
	GatewayCallers              []auth.Account `mapstructure:"gateway_callers"`
	WakeCallers                 []auth.Account `mapstructure:"wake_callers"`
	ControllerAccount           *auth.Account  `mapstructure:"sandbox_controller_account"`
	QueueCap                    int            `mapstructure:"max_queued_per_session"`
	WatchLeaseSeconds           int            `mapstructure:"dispatcher_watch_lease_seconds"`
	RunnerTimeoutSeconds        int            `mapstructure:"runner_request_timeout_seconds"`
}

func defaultConfig() config {
	return config{
		ListenAddress: ":8080", ProxyPort: sandboxapi.ControlServicePort,
		RunnerControlTargetTemplate: sandboxapi.ControlServiceTargetTemplate(),
		QueueCap:                    1000, WatchLeaseSeconds: 300, RunnerTimeoutSeconds: 10,
	}
}

func loadConfig() (config, error) {
	c := defaultConfig()
	settings := viper.New()
	settings.SetEnvPrefix("AGENTPLANE_RUNNER_GATEWAY")
	for _, key := range []string{
		"sandbox_namespace",
		"dispatcher_database_url",
		"pod_name",
		"pod_ip",
		"runner_token_file",
		"kubeconfig",
		"listen_address",
		"http_listen_address",
		"proxy_port",
		"runner_control_target_template",
		"dispatcher_audience",
		"gateway_audience",
		"runner_audience",
		"sandbox_controller_account",
		"max_queued_per_session",
		"dispatcher_watch_lease_seconds",
		"runner_request_timeout_seconds",
	} {
		settings.MustBindEnv(key)
	}
	if path := os.Getenv(settingPrefix + "CONFIG_FILE"); path != "" {
		settings.SetConfigFile(path)
		settings.SetConfigType("yaml")
		if err := settings.ReadInConfig(); err != nil {
			return config{}, errors.New("could not read Runner Gateway settings file")
		}
	}
	if err := settings.UnmarshalExact(&c, viper.DecodeHook(mapstructure.TextUnmarshallerHookFunc())); err != nil {
		return config{}, errors.New("invalid Runner Gateway settings")
	}
	if c.PodName == "" {
		var err error
		if c.PodName, err = os.Hostname(); err != nil {
			return config{}, err
		}
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

func (c *config) validate() error {
	for _, required := range []struct{ name, value string }{
		{"sandbox_namespace", c.Namespace},
		{"dispatcher_database_url", c.DispatcherDatabaseURL},
		{"pod_ip", c.PodIP},
		{"runner_token_file", c.RunnerTokenFile},
		{"dispatcher_audience", c.DispatcherAudience},
		{"gateway_audience", c.GatewayAudience},
		{"runner_audience", c.RunnerAudience},
		{"listen_address", c.ListenAddress},
		{"runner_control_target_template", c.RunnerControlTargetTemplate},
	} {
		if required.value == "" {
			return fmt.Errorf("%s is required", required.name)
		}
	}
	if len(c.DispatcherCallers) == 0 || len(c.GatewayCallers) == 0 {
		return errors.New("dispatcher_callers and gateway_callers must each list at least one ServiceAccount")
	}
	if c.ProxyPort < 1 || c.ProxyPort > 65535 {
		return errors.New("proxy_port must be between 1 and 65535")
	}
	for _, positive := range []struct {
		name  string
		value int
	}{
		{"max_queued_per_session", c.QueueCap},
		{"dispatcher_watch_lease_seconds", c.WatchLeaseSeconds},
		{"runner_request_timeout_seconds", c.RunnerTimeoutSeconds},
	} {
		if positive.value < 1 {
			return fmt.Errorf("%s must be positive", positive.name)
		}
	}
	for _, account := range c.WakeCallers {
		if !slices.Contains(c.DispatcherCallers, account) {
			return fmt.Errorf("wake caller %s/%s is not a Dispatcher caller", account.Namespace, account.Name)
		}
	}
	return nil
}
