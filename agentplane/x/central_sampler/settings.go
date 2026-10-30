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
)

const (
	defaultExecutorInitializeTimeoutSeconds = 30
	maxExecutorInitializeTimeoutSeconds     = int64((1<<63 - 1) / int64(time.Second))
)

type APIStyle string

const (
	APIStyleResponses         APIStyle = "responses"
	APIStyleAnthropicMessages APIStyle = "anthropic_messages"
)

type ResponsesDialect string

const (
	ResponsesDialectLite     ResponsesDialect = "lite"
	ResponsesDialectStandard ResponsesDialect = "standard"
)

type ExecEncoding string

const (
	ExecEncodingFreeform ExecEncoding = "freeform"
	ExecEncodingJSON     ExecEncoding = "json"
)

type AuthType string

const (
	AuthTypeBearer          AuthType = "bearer"
	AuthTypeAnthropicAPIKey AuthType = "anthropic_api_key"
)

// ModelConfig contains the wire and tool-call encodings supported by one model route.
type ModelConfig struct {
	Connection        string                   `json:"connection"`
	ProviderModel     string                   `json:"provider_model"`
	API               APIStyle                 `json:"api"`
	Responses         ResponsesConfig          `json:"responses,omitzero"`
	AnthropicMessages *AnthropicMessagesConfig `json:"anthropic_messages,omitempty"`
	ExecEncoding      ExecEncoding             `json:"exec_encoding"`
	ReasoningEfforts  []string                 `json:"reasoning_efforts"`
}

type ResponsesConfig struct {
	Dialect ResponsesDialect `json:"dialect"`
}

type AnthropicMessagesConfig struct {
	MaxTokens           int                         `json:"max_tokens"`
	Thinking            *AnthropicThinkingConfig    `json:"thinking,omitempty"`
	PromptCache         *AnthropicPromptCacheConfig `json:"prompt_cache,omitempty"`
	CacheDiagnostics    bool                        `json:"cache_diagnostics,omitempty"`
	PerMessageEffort    bool                        `json:"per_message_effort,omitempty"`
	StrictToolInput     bool                        `json:"strict_tool_input,omitempty"`
	EagerInputStreaming bool                        `json:"eager_input_streaming,omitempty"`
	Compaction          *AnthropicCompactionConfig  `json:"compaction,omitempty"`
}

type AnthropicThinkingConfig struct {
	Type         string                       `json:"type"`
	BudgetTokens *int                         `json:"budget_tokens,omitempty"`
	BlockBinding *AnthropicBlockBindingConfig `json:"block_binding,omitempty"`
}

type AnthropicBlockBindingConfig struct {
	PrefixMismatchBehavior string `json:"prefix_mismatch_behavior"`
}

type AnthropicPromptCacheConfig struct {
	TTL string `json:"ttl"`
}

type AnthropicCompactionConfig struct {
	MaxTokens         int  `json:"max_tokens"`
	AutoCompactTokens *int `json:"auto_compact_tokens,omitempty"`
	KeepRecentTurns   int  `json:"keep_recent_turns,omitempty"`
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
	if c.ExecEncoding != ExecEncodingFreeform && c.ExecEncoding != ExecEncodingJSON {
		return errors.New("exec_encoding must be freeform or json")
	}
	switch c.API {
	case APIStyleResponses:
		if c.Responses.Dialect != ResponsesDialectLite && c.Responses.Dialect != ResponsesDialectStandard {
			return errors.New("responses.dialect must be lite or standard")
		}
		if c.AnthropicMessages != nil {
			return errors.New("anthropic_messages settings are only valid for the anthropic_messages API")
		}
	case APIStyleAnthropicMessages:
		if c.Responses != (ResponsesConfig{}) {
			return errors.New("responses settings are only valid for the responses API")
		}
		if c.ExecEncoding != ExecEncodingJSON {
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

func (c *AnthropicMessagesConfig) Validate() error {
	if c.MaxTokens <= 0 {
		return errors.New("anthropic_messages.max_tokens must be positive")
	}
	if c.Thinking != nil {
		switch c.Thinking.Type {
		case "adaptive", "disabled":
			if c.Thinking.BudgetTokens != nil {
				return errors.New("thinking.budget_tokens is only valid with thinking.type enabled")
			}
		case "enabled":
			if c.Thinking.BudgetTokens == nil || *c.Thinking.BudgetTokens <= 0 || *c.Thinking.BudgetTokens >= c.MaxTokens {
				return errors.New("thinking.type enabled requires budget_tokens between zero and max_tokens")
			}
		default:
			return errors.New("thinking.type must be adaptive, disabled, or enabled")
		}
		if c.Thinking.BlockBinding != nil {
			if c.Thinking.Type != "adaptive" {
				return errors.New("thinking.block_binding requires thinking.type adaptive")
			}
			switch c.Thinking.BlockBinding.PrefixMismatchBehavior {
			case "error", "drop_block":
			default:
				return errors.New("thinking.block_binding.prefix_mismatch_behavior must be error or drop_block")
			}
		}
	}
	if c.PerMessageEffort && (c.Thinking == nil || c.Thinking.Type != "adaptive") {
		return errors.New("per_message_effort requires thinking.type adaptive")
	}
	if c.PromptCache != nil && c.PromptCache.TTL != "5m" && c.PromptCache.TTL != "1h" {
		return errors.New("prompt_cache.ttl must be 5m or 1h")
	}
	if c.Compaction != nil {
		if c.Compaction.MaxTokens <= 0 {
			return errors.New("compaction.max_tokens must be positive")
		}
		if c.Compaction.AutoCompactTokens != nil && *c.Compaction.AutoCompactTokens <= 0 {
			return errors.New("compaction.auto_compact_tokens must be positive")
		}
		if c.Compaction.KeepRecentTurns < 0 {
			return errors.New("compaction.keep_recent_turns must not be negative")
		}
	}
	return nil
}

func (c ModelConfig) Compatible(other ModelConfig) bool {
	if c.Connection != other.Connection || c.API != other.API || c.ExecEncoding != other.ExecEncoding {
		return false
	}
	switch c.API {
	case APIStyleResponses:
		return c.Responses == other.Responses
	case APIStyleAnthropicMessages:
		return c.ProviderModel == other.ProviderModel && c.Responses == other.Responses && anthropicMessagesEqual(c.AnthropicMessages, other.AnthropicMessages)
	default:
		return false
	}
}

func anthropicMessagesEqual(left, right *AnthropicMessagesConfig) bool {
	if left == nil || right == nil {
		return left == right
	}
	if left.MaxTokens != right.MaxTokens || left.CacheDiagnostics != right.CacheDiagnostics || left.PerMessageEffort != right.PerMessageEffort || left.StrictToolInput != right.StrictToolInput || left.EagerInputStreaming != right.EagerInputStreaming {
		return false
	}
	if !anthropicThinkingEqual(left.Thinking, right.Thinking) || !anthropicPromptCacheEqual(left.PromptCache, right.PromptCache) || !anthropicCompactionEqual(left.Compaction, right.Compaction) {
		return false
	}
	return true
}

func anthropicThinkingEqual(left, right *AnthropicThinkingConfig) bool {
	if left == nil || right == nil {
		return left == right
	}
	if left.Type != right.Type || !intPointerEqual(left.BudgetTokens, right.BudgetTokens) {
		return false
	}
	if left.BlockBinding == nil || right.BlockBinding == nil {
		return left.BlockBinding == right.BlockBinding
	}
	return left.BlockBinding.PrefixMismatchBehavior == right.BlockBinding.PrefixMismatchBehavior
}

func anthropicPromptCacheEqual(left, right *AnthropicPromptCacheConfig) bool {
	if left == nil || right == nil {
		return left == right
	}
	return left.TTL == right.TTL
}

func anthropicCompactionEqual(left, right *AnthropicCompactionConfig) bool {
	if left == nil || right == nil {
		return left == right
	}
	return left.MaxTokens == right.MaxTokens && left.KeepRecentTurns == right.KeepRecentTurns && intPointerEqual(left.AutoCompactTokens, right.AutoCompactTokens)
}

func intPointerEqual(left, right *int) bool {
	if left == nil || right == nil {
		return left == right
	}
	return *left == *right
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
		if config.API == APIStyleResponses && connection.Auth.Type != AuthTypeBearer {
			return fmt.Errorf("model %s responses API requires bearer connection auth", model)
		}
		if config.API == APIStyleAnthropicMessages && connection.Auth.Type != AuthTypeAnthropicAPIKey {
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
