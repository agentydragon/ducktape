package settings

import (
	"encoding/json"
	"errors"
	"io"
	"os"

	"github.com/agentydragon/ducktape/agentplane/eventblock"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
)

type Actions struct {
	Target string         `json:"target"`
	TLS    peertls.Config `json:"tls"`
}

type Settings struct {
	EnvironmentNamespace                   string                      `json:"environment_namespace"`
	Actions                                *Actions                    `json:"actions,omitempty"`
	ServiceCallers                         []string                    `json:"service_callers,omitempty"`
	AdminServiceCallers                    []string                    `json:"admin_service_callers,omitempty"`
	GatewayTLS                             *peertls.ClientTLS          `json:"gateway_tls,omitempty"`
	HistoryTLS                             *peertls.ClientTLS          `json:"history_tls,omitempty"`
	ListenTLS                              *peertls.ServerTLSListener  `json:"listen_tls,omitempty"`
	HealthListen                           *string                     `json:"health_listen,omitempty"`
	Models                                 map[string]ModelConfig      `json:"models"`
	Connections                            map[string]ConnectionConfig `json:"connections"`
	DatabaseURL                            string                      `json:"database_url"`
	PlatformInstructions                   *string                     `json:"platform_instructions,omitempty"`
	CodeModeHost                           string                      `json:"code_mode_host"`
	ExecutorCodex                          string                      `json:"executor_codex"`
	Logs                                   string                      `json:"logs"`
	HistoryTarget                          string                      `json:"history_target"`
	HistoryTokenFile                       string                      `json:"history_token_file"`
	GatewayTarget                          string                      `json:"gateway_target"`
	GatewayTokenFile                       string                      `json:"gateway_token_file"`
	DirectExecutorTarget                   string                      `json:"direct_executor_target"`
	DirectExecutorInsecureLocaltest        bool                        `json:"direct_executor_insecure_localtest"`
	MaxLiveSessions                        int                         `json:"max_live_sessions"`
	ContextSourceWrites                    bool                        `json:"context_source_writes,omitempty"`
	EventBlockWrites                       bool                        `json:"event_block_writes,omitempty"`
	EventBlockV2Writes                     bool                        `json:"event_block_v2_writes,omitempty"`
	HistoryExportPageEntries               *int                        `json:"history_export_page_entries,omitempty"`
	HistoryExportPagesPerTurn              *int                        `json:"history_export_pages_per_turn,omitempty"`
	FollowEventsPageEntries                *int                        `json:"follow_events_page_entries,omitempty"`
	EventBlockProfile                      eventblock.Profile          `json:"event_block_profile,omitzero"`
	ProviderResponseHeaderTimeoutSeconds   *int                        `json:"provider_response_header_timeout_seconds,omitempty"`
	ProviderIdleTimeoutSeconds             *int                        `json:"provider_idle_timeout_seconds,omitempty"`
	ProviderProgressLeaseCoalescingSeconds *int                        `json:"provider_progress_lease_coalescing_seconds,omitempty"`
	ProviderRetryInitialBackoffSeconds     *int                        `json:"provider_retry_initial_backoff_seconds,omitempty"`
	ProviderRetryMaxBackoffSeconds         *int                        `json:"provider_retry_max_backoff_seconds,omitempty"`
	ProviderAttemptBudget                  *int                        `json:"provider_attempt_budget,omitempty"`
	TurnTimeoutSeconds                     *int                        `json:"turn_timeout_seconds,omitempty"`
	RPCTimeoutSeconds                      int                         `json:"rpc_timeout_seconds"`
	CodeModeObservationTimeoutSeconds      *int                        `json:"code_mode_observation_timeout_seconds,omitempty"`
	ExecutorInitializeTimeoutSeconds       *int                        `json:"executor_initialize_timeout_seconds,omitempty"`
	ExecutorReconnectTimeoutSeconds        *int                        `json:"executor_reconnect_timeout_seconds,omitempty"`
	ShutdownDrainSeconds                   *int                        `json:"shutdown_drain_seconds,omitempty"`
	ShutdownBudgetSeconds                  *int                        `json:"shutdown_budget_seconds,omitempty"`
	LeaseSeconds                           int                         `json:"lease_seconds"`
	SetupTimeoutSeconds                    *int                        `json:"setup_timeout_seconds,omitempty"`
}

// MaxJournalPageEntries is the store limit for a single Events call.
const MaxJournalPageEntries = 4096

func (s Settings) ExportPageEntries() int {
	if s.HistoryExportPageEntries == nil {
		return 16
	}
	return *s.HistoryExportPageEntries
}

func (s Settings) ExportPagesPerTurn() int {
	if s.HistoryExportPagesPerTurn == nil {
		return 16
	}
	return *s.HistoryExportPagesPerTurn
}

func (s Settings) FollowPageEntries() int {
	if s.FollowEventsPageEntries == nil {
		return 256
	}
	return *s.FollowEventsPageEntries
}

const defaultHealthListen = ":8081"

// HealthListenAddress returns the gRPC health-only listener address.
func (s Settings) HealthListenAddress() string {
	if s.HealthListen == nil {
		return defaultHealthListen
	}
	return *s.HealthListen
}

func LoadSettings(path string) (Settings, error) {
	file, err := os.Open(path)
	if err != nil {
		return Settings{}, err
	}
	defer file.Close()
	settings, err := decodeSettings(file)
	if err != nil {
		return Settings{}, err
	}
	if databaseURL, ok := os.LookupEnv("AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL"); ok {
		settings.DatabaseURL = databaseURL
	}
	return settings, settings.Validate()
}

func DecodeSettings(reader io.Reader) (Settings, error) {
	settings, err := decodeSettings(reader)
	if err != nil {
		return Settings{}, err
	}
	return settings, settings.Validate()
}

func decodeSettings(reader io.Reader) (Settings, error) {
	// Deviation: preserve the sampler's existing strict JSON configuration contract during the Go rewrite.
	decoder := json.NewDecoder(reader)
	decoder.DisallowUnknownFields()
	var settings Settings
	if err := decoder.Decode(&settings); err != nil {
		return Settings{}, err
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		return Settings{}, errors.New("configuration must contain exactly one JSON object")
	}
	return settings, nil
}
