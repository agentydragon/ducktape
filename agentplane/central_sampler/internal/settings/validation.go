package settings

import (
	"errors"
	"fmt"
	"net/netip"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

const (
	defaultSetupTimeoutSeconds                  = 3600
	defaultCodeModeObservationTimeoutSeconds    = 300
	defaultExecutorInitializeTimeoutSeconds     = 30
	defaultExecutorReconnectTimeoutSeconds      = 120
	defaultProviderResponseHeaderTimeoutSeconds = 120
	defaultProviderIdleTimeoutSeconds           = 600
	defaultShutdownDrainSeconds                 = 40
	defaultShutdownBudgetSeconds                = 55
	maxExecutorInitializeTimeoutSeconds         = int64((1<<63 - 1) / int64(time.Second))
)

// ValidationError identifies a setting and a static validation rule through Field and Rule. Its
// wrapped cause may retain configured values for callers and must not be logged.
type ValidationError struct {
	field string
	cause string
	err   error
}

func (e *ValidationError) Error() string {
	message := e.field + ": " + e.cause
	if e.err != nil {
		message += ": " + e.err.Error()
	}
	return message
}

func (e *ValidationError) Unwrap() error { return e.err }

func (e *ValidationError) Field() string { return e.field }
func (e *ValidationError) Rule() string  { return e.cause }

func invalidSetting(field, cause string) error {
	return &ValidationError{field: field, cause: cause}
}

func invalidSettingWithCause(field, cause string, err error) error {
	return &ValidationError{field: field, cause: cause, err: err}
}

func prefixValidationError(prefix string, err error) error {
	var validation *ValidationError
	if errors.As(err, &validation) {
		return &ValidationError{field: prefix + "." + validation.Field(), cause: validation.Rule(), err: err}
	}
	return &ValidationError{field: prefix, cause: "is invalid", err: err}
}

func (s Settings) Validate() error {
	if s.PlatformInstructions != nil && strings.TrimSpace(*s.PlatformInstructions) == "" {
		return invalidSetting("platform_instructions", "must not be empty when configured")
	}
	for name, value := range map[string]string{
		"database_url":   s.DatabaseURL,
		"listen":         s.Listen,
		"audience":       s.Audience,
		"code_mode_host": s.CodeModeHost,
		"executor_codex": s.ExecutorCodex,
		"logs":           s.Logs,
	} {
		if value == "" {
			return invalidSetting(name, "is required")
		}
	}
	if len(s.Models) == 0 || len(s.AllowedAccounts) == 0 {
		return invalidSetting("models,allowed_accounts", "must not be empty")
	}
	if len(s.Connections) == 0 {
		return invalidSetting("connections", "must not be empty")
	}
	for name, connection := range s.Connections {
		if name == "" {
			return invalidSetting("connections", "connection name must not be empty")
		}
		if err := connection.Validate(); err != nil {
			return fmt.Errorf("connection %s: %w", name, prefixValidationError("connections.*", err))
		}
	}
	for model, config := range s.Models {
		if model == "" {
			return invalidSetting("models", "model route must not be empty")
		}
		if err := config.Validate(); err != nil {
			return fmt.Errorf("model %s: %w", model, prefixValidationError("models.*", err))
		}
		connection, ok := s.Connections[config.Connection]
		if !ok {
			return invalidSettingWithCause("models.*.connection", "references an unknown connection", fmt.Errorf("model %s references unknown connection %s", model, config.Connection))
		}
		if config.API == protocol.APIStyleResponses && connection.Auth.Type != AuthTypeBearer && connection.Auth.Type != AuthTypeAgentplaneIngress {
			return invalidSettingWithCause("models.*.api", "responses API requires bearer or agentplane_ingress connection auth", fmt.Errorf("model %s responses API requires bearer or agentplane_ingress connection auth", model))
		}
		if config.API == protocol.APIStyleAnthropicMessages && connection.Auth.Type != AuthTypeAnthropicAPIKey {
			return invalidSettingWithCause("models.*.api", "anthropic_messages API requires anthropic_api_key connection auth", fmt.Errorf("model %s anthropic_messages API requires anthropic_api_key connection auth", model))
		}
	}
	for _, account := range s.AllowedAccounts {
		parts := strings.Split(account, "/")
		if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
			return invalidSetting("allowed_accounts", "must use namespace/name")
		}
	}
	if (s.HistoryTarget == "") != (s.HistoryTokenFile == "") {
		return invalidSetting("history_target,history_token_file", "must be configured together")
	}
	gateway := s.GatewayTarget != ""
	if gateway != (s.GatewayTokenFile != "") {
		return invalidSetting("gateway_target,gateway_token_file", "must be configured together")
	}
	direct := s.DirectExecutorTarget != ""
	if gateway == direct {
		return invalidSetting("gateway_target,direct_executor_target", "configure exactly one gateway or direct executor target")
	}
	if direct != s.DirectExecutorInsecureLocaltest {
		return invalidSetting("direct_executor_target,direct_executor_insecure_localtest", "must be configured together")
	}
	if direct {
		address, err := netip.ParseAddrPort(s.DirectExecutorTarget)
		if err != nil || !address.Addr().IsLoopback() {
			return invalidSetting("direct_executor_target", "must use a loopback IP address")
		}
	}
	remaining := strings.ReplaceAll(strings.ReplaceAll(s.GatewayTarget, "{namespace}", ""), "{sandbox}", "")
	if strings.ContainsAny(remaining, "{}") {
		return invalidSetting("gateway_target", "supports only namespace and sandbox placeholders")
	}
	if s.MaxLiveSessions <= 0 || s.RPCTimeoutSeconds <= 0 || s.LeaseSeconds <= 30 {
		return invalidSetting("max_live_sessions,rpc_timeout_seconds,lease_seconds", "must be positive; lease_seconds must exceed 30")
	}
	for name, seconds := range map[string]int{
		"rpc_timeout_seconds": s.RPCTimeoutSeconds,
		"lease_seconds":       s.LeaseSeconds,
	} {
		if int64(seconds) > maxExecutorInitializeTimeoutSeconds {
			return invalidSetting(name, "must fit in time.Duration")
		}
	}
	observationSeconds := defaultCodeModeObservationTimeoutSeconds
	if s.CodeModeObservationTimeoutSeconds != nil {
		observationSeconds = *s.CodeModeObservationTimeoutSeconds
	}
	if observationSeconds <= 0 || int64(observationSeconds) > maxExecutorInitializeTimeoutSeconds-int64(s.RPCTimeoutSeconds) {
		return invalidSetting("code_mode_observation_timeout_seconds", "must be positive and fit with rpc_timeout_seconds in time.Duration")
	}
	if s.SetupTimeoutSeconds != nil && (*s.SetupTimeoutSeconds <= 0 || int64(*s.SetupTimeoutSeconds) > maxExecutorInitializeTimeoutSeconds) {
		return invalidSetting("setup_timeout_seconds", "must be positive and fit in time.Duration")
	}
	if s.TurnTimeoutSeconds != nil && (*s.TurnTimeoutSeconds <= 0 || int64(*s.TurnTimeoutSeconds) > maxExecutorInitializeTimeoutSeconds) {
		return invalidSetting("turn_timeout_seconds", "must be positive and fit in time.Duration")
	}
	if s.ExecutorInitializeTimeoutSeconds != nil {
		seconds := int64(*s.ExecutorInitializeTimeoutSeconds)
		if seconds <= 0 || seconds > maxExecutorInitializeTimeoutSeconds {
			return invalidSetting("executor_initialize_timeout_seconds", "must be positive and fit in time.Duration")
		}
	}
	if s.ExecutorReconnectTimeoutSeconds != nil {
		seconds := int64(*s.ExecutorReconnectTimeoutSeconds)
		if seconds <= 0 || seconds > maxExecutorInitializeTimeoutSeconds {
			return invalidSetting("executor_reconnect_timeout_seconds", "must be positive and fit in time.Duration")
		}
	}
	for name, seconds := range map[string]*int{
		"provider_response_header_timeout_seconds": s.ProviderResponseHeaderTimeoutSeconds,
		"provider_idle_timeout_seconds":            s.ProviderIdleTimeoutSeconds,
	} {
		if seconds != nil && (*seconds <= 0 || int64(*seconds) > maxExecutorInitializeTimeoutSeconds) {
			return invalidSetting(name, "must be positive and fit in time.Duration")
		}
	}
	headerSeconds := int64(s.ProviderResponseHeaderTimeout() / time.Second)
	idleSeconds := int64(s.ProviderIdleTimeout() / time.Second)
	rpcSlack := int64(s.RPCTimeoutSeconds)
	if rpcSlack > maxExecutorInitializeTimeoutSeconds/3 ||
		headerSeconds > maxExecutorInitializeTimeoutSeconds-idleSeconds ||
		headerSeconds+idleSeconds > maxExecutorInitializeTimeoutSeconds-3*rpcSlack {
		return invalidSetting("provider_response_header_timeout_seconds,provider_idle_timeout_seconds,rpc_timeout_seconds", "provider response-header timeout, idle timeout, and three RPC journal slack intervals must fit together in time.Duration")
	}
	for name, seconds := range map[string]*int{"shutdown_drain_seconds": s.ShutdownDrainSeconds, "shutdown_budget_seconds": s.ShutdownBudgetSeconds} {
		if seconds != nil && (*seconds <= 0 || int64(*seconds) > maxExecutorInitializeTimeoutSeconds) {
			return invalidSetting(name, "must be positive and fit in time.Duration")
		}
	}
	if s.ShutdownBudgetTimeout()-s.ShutdownDrainTimeout() < 15*time.Second {
		return invalidSetting("shutdown_budget_seconds,shutdown_drain_seconds", "must leave at least 15 seconds after shutdown_drain_seconds for cleanup")
	}
	if s.AutoCompactTokens != nil && *s.AutoCompactTokens <= 0 {
		return invalidSetting("auto_compact_tokens", "must be positive")
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

func (s Settings) SetupTimeout() time.Duration {
	seconds := defaultSetupTimeoutSeconds
	if s.SetupTimeoutSeconds != nil {
		seconds = *s.SetupTimeoutSeconds
	}
	return time.Duration(seconds) * time.Second
}

func (s Settings) CodeModeObservationTimeout() time.Duration {
	seconds := defaultCodeModeObservationTimeoutSeconds
	if s.CodeModeObservationTimeoutSeconds != nil {
		seconds = *s.CodeModeObservationTimeoutSeconds
	}
	return time.Duration(seconds) * time.Second
}

func (s Settings) ExecutorInitializeTimeout() time.Duration {
	seconds := defaultExecutorInitializeTimeoutSeconds
	if s.ExecutorInitializeTimeoutSeconds != nil {
		seconds = *s.ExecutorInitializeTimeoutSeconds
	}
	return time.Duration(seconds) * time.Second
}

func (s Settings) ExecutorReconnectTimeout() time.Duration {
	seconds := defaultExecutorReconnectTimeoutSeconds
	if s.ExecutorReconnectTimeoutSeconds != nil {
		seconds = *s.ExecutorReconnectTimeoutSeconds
	}
	return time.Duration(seconds) * time.Second
}

func (s Settings) ProviderResponseHeaderTimeout() time.Duration {
	seconds := defaultProviderResponseHeaderTimeoutSeconds
	if s.ProviderResponseHeaderTimeoutSeconds != nil {
		seconds = *s.ProviderResponseHeaderTimeoutSeconds
	}
	return time.Duration(seconds) * time.Second
}

func (s Settings) ProviderIdleTimeout() time.Duration {
	seconds := defaultProviderIdleTimeoutSeconds
	if s.ProviderIdleTimeoutSeconds != nil {
		seconds = *s.ProviderIdleTimeoutSeconds
	}
	return time.Duration(seconds) * time.Second
}

// ProviderAttemptLease covers response-header setup, one full semantic-idle interval, and three
// bounded RPC operations for durable head/event/renewal writes. Each semantic event renews
// the idle interval with two RPC operations reserved for evidence and the renewal.
func (s Settings) ProviderAttemptLease() time.Duration {
	phaseSeconds := int64(s.ProviderResponseHeaderTimeout()/time.Second) + int64(s.ProviderIdleTimeout()/time.Second) + 3*int64(s.RPCTimeoutSeconds)
	return time.Duration(phaseSeconds) * time.Second
}

// ProviderProgressLease covers one semantic-idle interval and the bounded database operation
// that follows it. The header phase has already completed when this renewal runs.
func (s Settings) ProviderProgressLease() time.Duration {
	return s.ProviderIdleTimeout() + 2*time.Duration(s.RPCTimeoutSeconds)*time.Second
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
