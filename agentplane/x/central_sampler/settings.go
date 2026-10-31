package centralsampler

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/netip"
	"net/url"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/responses"
)

const (
	defaultExecutorInitializeTimeoutSeconds = 30
	defaultShutdownDrainSeconds             = 40
	defaultShutdownBudgetSeconds            = 55
	maxExecutorInitializeTimeoutSeconds     = int64((1<<63 - 1) / int64(time.Second))
)

type AuthType string

const (
	AuthTypeBearer          AuthType = "bearer"
	AuthTypeAnthropicAPIKey AuthType = "anthropic_api_key"
)

// ModelConfig contains the wire and tool-call encodings supported by one model route.
type ModelConfig struct {
	Connection        string                `json:"connection"`
	ProviderModel     string                `json:"provider_model"`
	API               protocol.APIStyle     `json:"api"`
	Responses         responses.Config      `json:"responses,omitzero"`
	AnthropicMessages *anthropic.Config     `json:"anthropic_messages,omitempty"`
	ExecEncoding      protocol.ExecEncoding `json:"exec_encoding"`
	ReasoningEfforts  []string              `json:"reasoning_efforts"`
}

func (c ModelConfig) Validate() error {
	if c.Connection == "" {
		return errors.New("model connection is required")
	}
	if c.ProviderModel == "" {
		return errors.New("model provider_model is required")
	}
	if len(c.ReasoningEfforts) == 0 {
		return errors.New("model reasoning_efforts must not be empty")
	}
	if c.ExecEncoding != protocol.ExecEncodingFreeform && c.ExecEncoding != protocol.ExecEncodingJSON {
		return errors.New("exec_encoding must be freeform or json")
	}
	switch c.API {
	case protocol.APIStyleResponses:
		if err := c.Responses.Validate(); err != nil {
			return err
		}
		if c.AnthropicMessages != nil {
			return errors.New("anthropic_messages settings are only valid for the anthropic_messages API")
		}
	case protocol.APIStyleAnthropicMessages:
		if c.Responses != (responses.Config{}) {
			return errors.New("responses settings are only valid for the responses API")
		}
		if c.ExecEncoding != protocol.ExecEncodingJSON {
			return errors.New("anthropic_messages requires exec_encoding json")
		}
		if c.AnthropicMessages == nil {
			return errors.New("anthropic_messages settings are required for the anthropic_messages API")
		}
		for _, effort := range c.ReasoningEfforts {
			if !slices.Contains([]string{"low", "medium", "high", "xhigh", "max"}, effort) {
				return fmt.Errorf("anthropic reasoning effort %q is unsupported", effort)
			}
		}
		if err := c.AnthropicMessages.Validate(); err != nil {
			return err
		}
	default:
		return errors.New("model api must be responses or anthropic_messages")
	}
	return nil
}

func (c ModelConfig) Compatible(other ModelConfig) bool {
	if c.Connection != other.Connection || c.API != other.API || c.ExecEncoding != other.ExecEncoding {
		return false
	}
	switch c.API {
	case protocol.APIStyleResponses:
		return c.Responses.Compatible(other.Responses)
	case protocol.APIStyleAnthropicMessages:
		return c.ProviderModel == other.ProviderModel && c.Responses == other.Responses && c.AnthropicMessages.Compatible(other.AnthropicMessages)
	default:
		return false
	}
}

type ConnectionConfig struct {
	BaseURL string     `json:"base_url"`
	Auth    AuthConfig `json:"auth"`
}

type AuthConfig struct {
	Type           AuthType `json:"type"`
	Credential     string   `json:"credential,omitempty"`
	CredentialFile string   `json:"credential_file,omitempty"`
}

func (c ConnectionConfig) Validate() error {
	parsed, err := url.Parse(c.BaseURL)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Hostname() == "" || strings.HasSuffix(parsed.Host, ":") || parsed.User != nil || parsed.RawQuery != "" || parsed.ForceQuery || parsed.Fragment != "" || strings.ContainsAny(c.BaseURL, "?#") {
		return errors.New("connection base_url must be a safe http(s) URL without credentials, query, or fragment")
	}
	if rawPort := parsed.Port(); rawPort != "" {
		port, err := strconv.Atoi(rawPort)
		if err != nil || port < 1 || port > 65535 {
			return errors.New("connection base_url must use a valid TCP port")
		}
	}
	if (c.Auth.Credential == "") == (c.Auth.CredentialFile == "") {
		return errors.New("connection auth requires exactly one of credential and credential_file")
	}
	switch c.Auth.Type {
	case AuthTypeBearer, AuthTypeAnthropicAPIKey:
	default:
		return errors.New("connection auth.type must be bearer or anthropic_api_key")
	}
	return nil
}

func (a AuthConfig) ReadCredential() (string, error) {
	if (a.Credential == "") == (a.CredentialFile == "") {
		return "", errors.New("connection auth requires exactly one of credential and credential_file")
	}
	if a.CredentialFile == "" {
		return a.Credential, nil
	}
	data, err := os.ReadFile(a.CredentialFile)
	if err != nil {
		return "", fmt.Errorf("read provider credential file: %w", err)
	}
	credential := strings.TrimSpace(string(data))
	if credential == "" {
		return "", errors.New("provider credential file is empty")
	}
	return credential, nil
}

type Settings struct {
	Models                           map[string]ModelConfig      `json:"models"`
	Connections                      map[string]ConnectionConfig `json:"connections"`
	DatabaseURL                      string                      `json:"database_url"`
	Listen                           string                      `json:"listen"`
	Audience                         string                      `json:"audience"`
	AllowedAccounts                  []string                    `json:"allowed_accounts"`
	CodeModeHost                     string                      `json:"code_mode_host"`
	ExecutorCodex                    string                      `json:"executor_codex"`
	Logs                             string                      `json:"logs"`
	HistoryTarget                    string                      `json:"history_target"`
	HistoryTokenFile                 string                      `json:"history_token_file"`
	GatewayTarget                    string                      `json:"gateway_target"`
	GatewayTokenFile                 string                      `json:"gateway_token_file"`
	DirectExecutorTarget             string                      `json:"direct_executor_target"`
	DirectExecutorInsecureLocaltest  bool                        `json:"direct_executor_insecure_localtest"`
	MaxLiveSessions                  int                         `json:"max_live_sessions"`
	TurnTimeoutSeconds               int                         `json:"turn_timeout_seconds"`
	RPCTimeoutSeconds                int                         `json:"rpc_timeout_seconds"`
	ExecutorInitializeTimeoutSeconds *int                        `json:"executor_initialize_timeout_seconds,omitempty"`
	ShutdownDrainSeconds             *int                        `json:"shutdown_drain_seconds,omitempty"`
	ShutdownBudgetSeconds            *int                        `json:"shutdown_budget_seconds,omitempty"`
	LeaseSeconds                     int                         `json:"lease_seconds"`
	AutoCompactTokens                *int                        `json:"auto_compact_tokens"`
	Kubeconfig                       string                      `json:"kubeconfig"`
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

func (s Settings) Validate() error {
	for name, value := range map[string]string{"database_url": s.DatabaseURL, "listen": s.Listen, "audience": s.Audience, "code_mode_host": s.CodeModeHost, "executor_codex": s.ExecutorCodex, "logs": s.Logs} {
		if value == "" {
			return fmt.Errorf("%s is required", name)
		}
	}
	if len(s.Models) == 0 || len(s.AllowedAccounts) == 0 {
		return errors.New("models and allowed_accounts must not be empty")
	}
	if len(s.Connections) == 0 {
		return errors.New("connections must not be empty")
	}
	for name, connection := range s.Connections {
		if name == "" {
			return errors.New("connection name must not be empty")
		}
		if err := connection.Validate(); err != nil {
			return fmt.Errorf("connection %s: %w", name, err)
		}
	}
	for model, config := range s.Models {
		if model == "" {
			return errors.New("model route must not be empty")
		}
		if err := config.Validate(); err != nil {
			return fmt.Errorf("model %s: %w", model, err)
		}
		connection, ok := s.Connections[config.Connection]
		if !ok {
			return fmt.Errorf("model %s references unknown connection %s", model, config.Connection)
		}
		if config.API == protocol.APIStyleResponses && connection.Auth.Type != AuthTypeBearer {
			return fmt.Errorf("model %s responses API requires bearer connection auth", model)
		}
		if config.API == protocol.APIStyleAnthropicMessages && connection.Auth.Type != AuthTypeAnthropicAPIKey {
			return fmt.Errorf("model %s anthropic_messages API requires anthropic_api_key connection auth", model)
		}
	}
	for _, account := range s.AllowedAccounts {
		parts := strings.Split(account, "/")
		if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
			return errors.New("allowed_accounts must use namespace/name")
		}
	}
	if (s.HistoryTarget == "") != (s.HistoryTokenFile == "") {
		return errors.New("history target and token file must be configured together")
	}
	gateway := s.GatewayTarget != ""
	if gateway != (s.GatewayTokenFile != "") {
		return errors.New("gateway target and token file must be configured together")
	}
	direct := s.DirectExecutorTarget != ""
	if gateway == direct {
		return errors.New("configure exactly one gateway or direct executor target")
	}
	if direct != s.DirectExecutorInsecureLocaltest {
		return errors.New("direct executor target and explicit insecure localtest mode must be configured together")
	}
	if direct {
		address, err := netip.ParseAddrPort(s.DirectExecutorTarget)
		if err != nil || !address.Addr().IsLoopback() {
			return errors.New("insecure direct executor target must use a loopback IP address")
		}
	}
	remaining := strings.ReplaceAll(strings.ReplaceAll(s.GatewayTarget, "{namespace}", ""), "{sandbox}", "")
	if strings.ContainsAny(remaining, "{}") {
		return errors.New("gateway target supports only namespace and sandbox placeholders")
	}
	if s.MaxLiveSessions <= 0 || s.TurnTimeoutSeconds <= 0 || s.RPCTimeoutSeconds <= 0 || s.LeaseSeconds <= 30 {
		return errors.New("resource limits and timeouts must be positive; lease_seconds must exceed 30")
	}
	if s.ExecutorInitializeTimeoutSeconds != nil {
		seconds := int64(*s.ExecutorInitializeTimeoutSeconds)
		if seconds <= 0 || seconds > maxExecutorInitializeTimeoutSeconds {
			return errors.New("executor_initialize_timeout_seconds must be positive and fit in time.Duration")
		}
	}
	for name, seconds := range map[string]*int{"shutdown_drain_seconds": s.ShutdownDrainSeconds, "shutdown_budget_seconds": s.ShutdownBudgetSeconds} {
		if seconds != nil && (*seconds <= 0 || int64(*seconds) > maxExecutorInitializeTimeoutSeconds) {
			return fmt.Errorf("%s must be positive and fit in time.Duration", name)
		}
	}
	if s.ShutdownBudgetTimeout()-s.ShutdownDrainTimeout() < 15*time.Second {
		return errors.New("shutdown_budget_seconds must leave at least 15 seconds after shutdown_drain_seconds for cleanup")
	}
	if s.AutoCompactTokens != nil && *s.AutoCompactTokens <= 0 {
		return errors.New("auto_compact_tokens must be positive")
	}
	return nil
}

func (s Settings) Connection(model string) (ConnectionConfig, error) {
	config, ok := s.Models[model]
	if !ok {
		return ConnectionConfig{}, fmt.Errorf("unknown model %s", model)
	}
	connection, ok := s.Connections[config.Connection]
	if !ok {
		return ConnectionConfig{}, fmt.Errorf("model %s references unknown connection %s", model, config.Connection)
	}
	return connection, nil
}

func (s Settings) ExecutorInitializeTimeout() time.Duration {
	seconds := defaultExecutorInitializeTimeoutSeconds
	if s.ExecutorInitializeTimeoutSeconds != nil {
		seconds = *s.ExecutorInitializeTimeoutSeconds
	}
	return time.Duration(seconds) * time.Second
}

func (s Settings) ShutdownDrainTimeout() time.Duration {
	seconds := defaultShutdownDrainSeconds
	if s.ShutdownDrainSeconds != nil {
		seconds = *s.ShutdownDrainSeconds
	}
	return time.Duration(seconds) * time.Second
}

func (s Settings) ShutdownBudgetTimeout() time.Duration {
	seconds := defaultShutdownBudgetSeconds
	if s.ShutdownBudgetSeconds != nil {
		seconds = *s.ShutdownBudgetSeconds
	}
	return time.Duration(seconds) * time.Second
}

var executorDNSLabel = regexp.MustCompile(`^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$`)

func (s Settings) GatewayAddress(namespace, sandbox string) (string, error) {
	if s.GatewayTarget == "" {
		return "", errors.New("gateway transport is not configured")
	}
	if strings.Contains(s.GatewayTarget, "{") {
		for _, name := range []string{namespace, sandbox} {
			if len(name) > 63 || !executorDNSLabel.MatchString(name) {
				return "", errors.New("gateway template requires DNS-label executor names")
			}
		}
	}
	return strings.ReplaceAll(strings.ReplaceAll(s.GatewayTarget, "{namespace}", namespace), "{sandbox}", sandbox), nil
}

func (s Settings) AllowsAccount(namespace, name string) bool {
	return slices.Contains(s.AllowedAccounts, namespace+"/"+name)
}
