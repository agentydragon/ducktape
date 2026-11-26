package main

import (
	"errors"
	"fmt"
	"math"
	"os"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/configfile"
	"github.com/agentydragon/ducktape/agentplane/eventblock"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/listener"
)

const (
	settingPrefix                  = "AGENTPLANE_HISTORY_SERVICE_"
	defaultMigrationTimeoutSeconds = 600
)

// config is the History Service's deployment contract: static settings come from the YAML file named by
// AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE. The database URL is the only service-specific runtime environment input.
//
// The database URL is a Secret, so it is an environment value.
type config struct {
	DatabaseURL             string  `mapstructure:"-"`
	MigrationTimeoutSeconds float64 `mapstructure:"migration_timeout_s"`
	// ReaderServiceCallers are exact component SPIFFE IDs admitted as backend-wide History readers.
	ReaderServiceCallers []string                 `mapstructure:"reader_service_callers"`
	ServiceDelegation    *serviceDelegationConfig `mapstructure:"service_delegation"`
	// RequestTimeoutSeconds bounds a call's backend work and each write to a watch stream.
	RequestTimeoutSeconds float64         `mapstructure:"request_timeout_s"`
	RPC                   listener.Config `mapstructure:"rpc"`
	Kubeconfig            string          `mapstructure:"kubeconfig"`
	// PolicyNamespace holds the Identities a Sandbox SVID may name.
	PolicyNamespace string        `mapstructure:"policy_namespace"`
	Ingress         ingressConfig `mapstructure:"ingress"`
	FoldConcurrency int           `mapstructure:"fold_concurrency"`
	// FoldPageSize is how many entries a cycle folds per Session; the history store serves at most 1000.
	FoldPageSize             int     `mapstructure:"fold_page_size"`
	FoldIntervalSeconds      float64 `mapstructure:"fold_interval_s"`
	NativeFrameReadBatchSize int     `mapstructure:"native_frame_read_batch_size"`
	// WatchLeaseSeconds is how long a watch or follow stream lasts before planned renewal.
	WatchLeaseSeconds float64 `mapstructure:"watch_lease_s"`
}

type serviceDelegationConfig struct {
	Actors  []auth.ServiceDelegator `mapstructure:"actors"`
	Methods []string                `mapstructure:"methods"`
}

// ingressConfig is the Session journal ingress's authorization and storage policy. Its RPC shares the service listener.
type ingressConfig struct {
	EventBlockWrites            bool           `mapstructure:"event_block_writes"`
	EventBlockProfile           blockProfile   `mapstructure:"event_block_profile"`
	EventBlockTargetBytesByKind map[string]int `mapstructure:"event_block_target_bytes_by_kind"`

	// WriterNamespaces scopes the live workload Identity credential records resolved for journal writers.
	WriterNamespaces []string `mapstructure:"writer_namespaces"`
	// SamplerServiceActor is the exact service SVID admitted to push central sampler journals.
	SamplerServiceActor string `mapstructure:"sampler_service_actor"`
	// SettleDeltas removes an item's streamed delta entries from stored history once its retained completion
	// provably holds their concatenation. Off keeps every entry, for debugging.
	SettleDeltas bool `mapstructure:"settle_deltas"`
	// CompactDeltas packs original streamed events into bounded, lossless storage blocks while live.
	CompactDeltas bool `mapstructure:"compact_deltas"`
	// CompactExecutionEvidence packs BoundaryRecord and ExecutionProgress events after every reader
	// replica supports those block kinds. Keep this disabled during a mixed-version rollout.
	CompactExecutionEvidence bool `mapstructure:"compact_execution_evidence"`
}

type blockProfile struct {
	TargetBytes   int    `mapstructure:"target_bytes"`
	TargetEntries int    `mapstructure:"target_entries"`
	Codec         string `mapstructure:"codec"`
	Level         int    `mapstructure:"level"`
}

func (c ingressConfig) packers() (map[historypb.EventKind]*eventblock.Packer, error) {
	profile := c.EventBlockProfile
	if profile.Codec != "zstd" {
		return nil, fmt.Errorf("invalid event block codec %q", profile.Codec)
	}
	byKind := map[historypb.EventKind]int{}
	for name, size := range c.EventBlockTargetBytesByKind {
		n, ok := historypb.EventKind_value["EVENT_KIND_"+strings.ToUpper(name)]
		if !ok || n == 0 {
			return nil, fmt.Errorf("invalid event block kind %q", name)
		}
		byKind[historypb.EventKind(n)] = size
	}
	build := func(size int) (*eventblock.Packer, error) {
		return eventblock.NewPacker(eventblock.Profile{TargetBytes: size, TargetEntries: profile.TargetEntries, Level: profile.Level})
	}
	base, err := build(profile.TargetBytes)
	if err != nil {
		return nil, err
	}
	result := map[historypb.EventKind]*eventblock.Packer{}
	for n := range historypb.EventKind_name {
		if n != 0 {
			result[historypb.EventKind(n)] = base
		}
	}
	for k, v := range byKind {
		p, err := build(v)
		if err != nil {
			return nil, err
		}
		result[k] = p
	}
	return result, nil
}

func defaultConfig() config {
	return config{
		RequestTimeoutSeconds: 15, MigrationTimeoutSeconds: defaultMigrationTimeoutSeconds,
		RPC:             listener.Config{ListenAddress: "0.0.0.0:8080"},
		Ingress:         ingressConfig{SettleDeltas: true, EventBlockProfile: blockProfile{65536, 32, "zstd", 3}, EventBlockTargetBytesByKind: map[string]int{"BOUNDARY_RECORD": 2 << 20}},
		FoldConcurrency: 4, FoldPageSize: 1000, FoldIntervalSeconds: 2,
		NativeFrameReadBatchSize: 200, WatchLeaseSeconds: 300,
	}
}

func loadConfig() (config, error) {
	path := strings.TrimSpace(os.Getenv(settingPrefix + "CONFIG_FILE"))
	if path == "" {
		return config{}, fmt.Errorf("%s is required", settingPrefix+"CONFIG_FILE")
	}
	c := defaultConfig()
	if _, err := configfile.DecodeMapstructureFile(path, &c, configfile.MapstructureOptions{
		DecodeHook: listener.AuthenticationDecodeHook,
	}); err != nil {
		return config{}, configfile.RedactMapstructureError("History Service", err)
	}
	c.DatabaseURL = os.Getenv(settingPrefix + "DATABASE_URL")
	if err := c.validate(); err != nil {
		return config{}, err
	}
	return c, nil
}

func (c *config) validate() error {
	for _, required := range []struct{ name, value string }{
		{"database_url", c.DatabaseURL},
		{"policy_namespace", c.PolicyNamespace},
	} {
		if required.value == "" {
			return fmt.Errorf("%s is required", required.name)
		}
	}
	if err := auth.ValidateServiceCallers(c.ReaderServiceCallers); err != nil {
		return fmt.Errorf("invalid reader_service_callers: %w", err)
	}
	if c.ServiceDelegation != nil && (len(c.ServiceDelegation.Actors) == 0 || len(c.ServiceDelegation.Methods) == 0) {
		return errors.New("service_delegation requires actors and methods")
	}
	if c.ServiceDelegation != nil {
		for _, actor := range c.ServiceDelegation.Actors {
			if err := auth.ValidateServiceCallers([]string{actor.SPIFFEID}); err != nil {
				return fmt.Errorf("invalid service_delegation actor: %w", err)
			}
		}
	}
	if c.Ingress.SamplerServiceActor != "" {
		if err := auth.ValidateServiceCallers([]string{c.Ingress.SamplerServiceActor}); err != nil {
			return fmt.Errorf("invalid ingress.sampler_service_actor: %w", err)
		}
	}
	if err := c.RPC.Validate(); err != nil {
		return fmt.Errorf("rpc listener: %w", err)
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
