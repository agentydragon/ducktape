package main

import (
	"errors"
	"fmt"
	"math"
	"os"
	"strconv"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/go-viper/mapstructure/v2"
	"github.com/spf13/viper"
)

const (
	settingPrefix                  = "AGENTPLANE_HISTORY_SERVICE_"
	defaultMigrationTimeoutSeconds = 600
)

// config is the History Service's deployment contract: each setting is a YAML key of the file named by
// AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE, and each top-level key also names an AGENTPLANE_HISTORY_SERVICE_*
// environment variable that overrides it. A list takes one element that way, so lists belong in the file.
// An account is written `namespace/name`, or as a `namespace` and `name` pair.
//
// The database URL is a Secret, so it is an environment value.
type config struct {
	DatabaseURL             string  `mapstructure:"database_url"`
	MigrationTimeoutSeconds float64 `mapstructure:"migration_timeout_s"`
	// ReaderAccounts may call the read API; their namespaces are the ones a reader's token may come from.
	ReaderAccounts []auth.Account `mapstructure:"reader_accounts"`
	// Delegation is a separate Action actor grant and only applies to its listed RPC methods.
	Delegation    *delegationConfig `mapstructure:"delegation"`
	TokenAudience string            `mapstructure:"token_audience"`
	// RequestTimeoutSeconds bounds a call's backend work and each write to a watch stream.
	RequestTimeoutSeconds float64       `mapstructure:"request_timeout_s"`
	ListenAddress         string        `mapstructure:"listen_address"`
	Kubeconfig            string        `mapstructure:"kubeconfig"`
	Ingress               ingressConfig `mapstructure:"ingress"`
	FoldConcurrency       int           `mapstructure:"fold_concurrency"`
	// FoldPageSize is how many entries a cycle folds per Session; the history store serves at most 1000.
	FoldPageSize             int     `mapstructure:"fold_page_size"`
	FoldIntervalSeconds      float64 `mapstructure:"fold_interval_s"`
	NativeFrameReadBatchSize int     `mapstructure:"native_frame_read_batch_size"`
	// WatchLeaseSeconds is how long a watch or follow stream lasts before planned renewal.
	WatchLeaseSeconds float64 `mapstructure:"watch_lease_s"`
}

type delegationConfig struct {
	Actor            auth.Account `mapstructure:"actor"`
	CallerNamespaces []string     `mapstructure:"caller_namespaces"`
	Methods          []string     `mapstructure:"methods"`
}

// ingressConfig is the runners' listener, apart from the read API so the network fence can admit the egress proxy
// to it and nothing else.
type ingressConfig struct {
	TokenAudience         string                                `mapstructure:"token_audience"`
	WorkloadTLS           *environment_runtime_transport.Config `mapstructure:"workload_tls"`
	WorkloadListenAddress string                                `mapstructure:"workload_listen_address"`
	// WriterNamespaces are the namespaces whose ServiceAccounts may push: the ones Sandboxes run in.
	WriterNamespaces []string `mapstructure:"writer_namespaces"`
	// Only these service identities may announce centrally hosted Sessions. Empty disables the mode.
	SamplerAccounts []auth.Account `mapstructure:"sampler_accounts"`
	ListenAddress   string         `mapstructure:"listen_address"`
	// SettleDeltas removes an item's streamed delta entries from stored history once its retained completion
	// provably holds their concatenation. Off keeps every entry, for debugging.
	SettleDeltas bool `mapstructure:"settle_deltas"`
}

func defaultConfig() config {
	return config{
		RequestTimeoutSeconds: 15, MigrationTimeoutSeconds: defaultMigrationTimeoutSeconds, ListenAddress: "0.0.0.0:8080",
		Ingress:         ingressConfig{ListenAddress: "0.0.0.0:8081", SettleDeltas: true},
		FoldConcurrency: 4, FoldPageSize: 1000, FoldIntervalSeconds: 2,
		NativeFrameReadBatchSize: 200, WatchLeaseSeconds: 300,
	}
}

func loadConfig() (config, error) {
	c := defaultConfig()
	// Without ExperimentalBindStruct, Unmarshal reads an environment variable only for a bound key.
	settings := viper.NewWithOptions(viper.ExperimentalBindStruct())
	settings.SetEnvPrefix("AGENTPLANE_HISTORY_SERVICE")
	settings.AutomaticEnv()
	if path := os.Getenv(settingPrefix + "CONFIG_FILE"); path != "" {
		settings.SetConfigFile(path)
		settings.SetConfigType("yaml")
		if err := settings.ReadInConfig(); err != nil {
			return config{}, errors.New("could not read History Service settings file")
		}
	}
	if err := settings.UnmarshalExact(&c, viper.DecodeHook(mapstructure.TextUnmarshallerHookFunc())); err != nil {
		return config{}, errors.New("invalid History Service settings")
	}
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

func (c *config) validate() error {
	for _, required := range []struct{ name, value string }{
		{"database_url", c.DatabaseURL},
		{"token_audience", c.TokenAudience},
		{"ingress.token_audience", c.Ingress.TokenAudience},
		{"listen_address", c.ListenAddress},
		{"ingress.listen_address", c.Ingress.ListenAddress},
	} {
		if required.value == "" {
			return fmt.Errorf("%s is required", required.name)
		}
	}
	if len(c.ReaderAccounts) == 0 {
		return errors.New("reader_accounts must list at least one ServiceAccount")
	}
	if c.Delegation != nil && (c.Delegation.Actor.Namespace == "" || c.Delegation.Actor.Name == "" ||
		len(c.Delegation.CallerNamespaces) == 0 || len(c.Delegation.Methods) == 0) {
		return errors.New("delegation requires an actor, caller namespaces and methods")
	}
	if (c.Ingress.WorkloadTLS == nil) != (c.Ingress.WorkloadListenAddress == "") {
		return errors.New("ingress workload_tls and workload_listen_address must be configured together")
	}
	if c.Ingress.WorkloadTLS != nil {
		if err := c.Ingress.WorkloadTLS.Validate(); err != nil {
			return err
		}
	}
	if len(c.Ingress.WriterNamespaces) == 0 {
		return errors.New("ingress.writer_namespaces must list at least one namespace")
	}
	if c.RequestTimeoutSeconds <= 0 || c.RequestTimeoutSeconds > 60 {
		return errors.New("request_timeout_s must be above 0 and at most 60")
	}
	if !validMigrationTimeout(c.MigrationTimeoutSeconds) {
		return errors.New("migration_timeout_s must be between 0.001 and 86400 seconds")
	}
	if c.FoldConcurrency < 1 || c.FoldConcurrency > 64 {
		return errors.New("fold_concurrency must be between 1 and 64")
	}
	if c.FoldPageSize < 1 || c.FoldPageSize > 1000 {
		return errors.New("fold_page_size must be between 1 and 1000")
	}
	if c.NativeFrameReadBatchSize < 1 {
		return errors.New("native_frame_read_batch_size must be positive")
	}
	if c.FoldIntervalSeconds <= 0 || c.WatchLeaseSeconds <= 0 {
		return errors.New("fold_interval_s and watch_lease_s must be positive")
	}
	return nil
}

func seconds(value float64) time.Duration { return time.Duration(value * float64(time.Second)) }

func validMigrationTimeout(value float64) bool {
	return !math.IsNaN(value) && value >= 0.001 && value <= 86400
}

// Migration-only mode needs only the database URL and its optional timeout, not serving settings.
func migrationOnlyTimeout() (time.Duration, error) {
	value := float64(defaultMigrationTimeoutSeconds)
	if raw := os.Getenv(settingPrefix + "MIGRATION_TIMEOUT_S"); raw != "" {
		var err error
		value, err = strconv.ParseFloat(raw, 64)
		if err != nil || !validMigrationTimeout(value) {
			return 0, errors.New("migration_timeout_s must be between 0.001 and 86400 seconds")
		}
	}
	return seconds(value), nil
}
