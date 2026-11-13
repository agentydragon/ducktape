package settings

import (
	"encoding/json"
	"errors"
	"io"
	"os"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
)

type Settings struct {
	GatewayTLS                       *environment_runtime_transport.ClientTLS `mapstructure:"gateway_tls" json:"gateway_tls,omitempty"`
	HistoryTLS                       *environment_runtime_transport.ClientTLS `mapstructure:"history_tls" json:"history_tls,omitempty"`
	Models                           map[string]ModelConfig                   `json:"models"`
	Connections                      map[string]ConnectionConfig              `json:"connections"`
	DatabaseURL                      string                                   `json:"database_url"`
	Listen                           string                                   `json:"listen"`
	Audience                         string                                   `json:"audience"`
	AllowedAccounts                  []string                                 `json:"allowed_accounts"`
	PlatformInstructions             *string                                  `json:"platform_instructions,omitempty"`
	CodeModeHost                     string                                   `json:"code_mode_host"`
	ExecutorCodex                    string                                   `json:"executor_codex"`
	Logs                             string                                   `json:"logs"`
	HistoryTarget                    string                                   `json:"history_target"`
	HistoryTokenFile                 string                                   `json:"history_token_file"`
	GatewayTarget                    string                                   `json:"gateway_target"`
	GatewayTokenFile                 string                                   `json:"gateway_token_file"`
	DirectExecutorTarget             string                                   `json:"direct_executor_target"`
	DirectExecutorInsecureLocaltest  bool                                     `json:"direct_executor_insecure_localtest"`
	MaxLiveSessions                  int                                      `json:"max_live_sessions"`
	TurnTimeoutSeconds               int                                      `json:"turn_timeout_seconds"`
	RPCTimeoutSeconds                int                                      `json:"rpc_timeout_seconds"`
	ExecutorInitializeTimeoutSeconds *int                                     `json:"executor_initialize_timeout_seconds,omitempty"`
	ExecutorReconnectTimeoutSeconds  *int                                     `json:"executor_reconnect_timeout_seconds,omitempty"`
	ShutdownDrainSeconds             *int                                     `json:"shutdown_drain_seconds,omitempty"`
	ShutdownBudgetSeconds            *int                                     `json:"shutdown_budget_seconds,omitempty"`
	LeaseSeconds                     int                                      `json:"lease_seconds"`
	AutoCompactTokens                *int                                     `json:"auto_compact_tokens"`
	Kubeconfig                       string                                   `json:"kubeconfig"`
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
