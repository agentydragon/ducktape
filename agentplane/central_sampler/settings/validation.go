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
	defaultExecutorInitializeTimeoutSeconds = 30
	defaultExecutorReconnectTimeoutSeconds  = 120
	defaultShutdownDrainSeconds             = 40
	defaultShutdownBudgetSeconds            = 55
	maxExecutorInitializeTimeoutSeconds     = int64((1<<63 - 1) / int64(time.Second))
)

func (s Settings) Validate() error {
	if s.PlatformInstructions != nil && strings.TrimSpace(*s.PlatformInstructions) == "" {
		return errors.New("platform_instructions must not be empty when configured")
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
		if config.API == protocol.APIStyleResponses && connection.Auth.Type != AuthTypeBearer && connection.Auth.Type != AuthTypeAgentplaneIngress {
			return fmt.Errorf("model %s responses API requires bearer or agentplane_ingress connection auth", model)
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
	if s.ExecutorReconnectTimeoutSeconds != nil {
		seconds := int64(*s.ExecutorReconnectTimeoutSeconds)
		if seconds <= 0 || seconds > maxExecutorInitializeTimeoutSeconds {
			return errors.New("executor_reconnect_timeout_seconds must be positive and fit in time.Duration")
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

func (s Settings) ExecutorReconnectTimeout() time.Duration {
	seconds := defaultExecutorReconnectTimeoutSeconds
	if s.ExecutorReconnectTimeoutSeconds != nil {
		seconds = *s.ExecutorReconnectTimeoutSeconds
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
