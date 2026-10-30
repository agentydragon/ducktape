package centralsampler

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fixtureSettings() Settings {
	return Settings{
		Models:      map[string]ModelConfig{"test-model": {Connection: "provider", ProviderModel: "test-model", API: APIStyleResponses, Responses: ResponsesConfig{Dialect: ResponsesDialectLite}, ExecEncoding: ExecEncodingFreeform, ReasoningEfforts: []string{"", "high"}}},
		Connections: map[string]ConnectionConfig{"provider": {BaseURL: "https://model.invalid", Auth: AuthConfig{Type: AuthTypeBearer, Credential: "test-credential"}}},
		DatabaseURL: "postgresql://fixture.invalid/sampler", Listen: "127.0.0.1:0", Audience: "test-sampler", AllowedAccounts: []string{"test/client"}, CodeModeHost: "/fixture/host", ExecutorCodex: "/fixture/codex", Logs: "/fixture/logs", DirectExecutorTarget: "127.0.0.1:4501", DirectExecutorInsecureLocaltest: true, MaxLiveSessions: 2, TurnTimeoutSeconds: 40, RPCTimeoutSeconds: 10, LeaseSeconds: 60,
	}
}

func TestExecutorInitializeTimeoutDefaultsIndependentlyAndCanBeConfigured(t *testing.T) {
	settings := fixtureSettings()
	if got := settings.ExecutorInitializeTimeout(); got != 30*time.Second {
		t.Fatalf("default executor initialization timeout=%s, want 30s", got)
	}
	if err := settings.Validate(); err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(settings)
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := DecodeSettings(strings.NewReader(string(encoded)))
	if err != nil {
		t.Fatal(err)
	}
	if got := decoded.ExecutorInitializeTimeout(); got != 30*time.Second {
		t.Fatalf("omitted executor initialization timeout=%s, want 30s", got)
	}

	configured := 120
	settings.ExecutorInitializeTimeoutSeconds = &configured
	if got := settings.ExecutorInitializeTimeout(); got != 120*time.Second {
		t.Fatalf("configured executor initialization timeout=%s, want 120s", got)
	}
	if err := settings.Validate(); err != nil {
		t.Fatal(err)
	}

	maximum := int(maxExecutorInitializeTimeoutSeconds)
	settings.ExecutorInitializeTimeoutSeconds = &maximum
	if err := settings.Validate(); err != nil {
		t.Fatalf("rejected maximum representable timeout: %v", err)
	}
	if got := settings.ExecutorInitializeTimeout(); got <= 0 {
		t.Fatalf("maximum representable timeout overflowed: %s", got)
	}

	for _, invalid := range []int{0, -1, maximum + 1} {
		settings.ExecutorInitializeTimeoutSeconds = &invalid
		if err := settings.Validate(); err == nil {
			t.Fatalf("accepted invalid executor_initialize_timeout_seconds=%d", invalid)
		}
	}
}

func TestSettingsRequireExplicitSupportedModelConfiguration(t *testing.T) {
	for name, config := range map[string]ModelConfig{
		"missing connection":           {ProviderModel: "test-model", API: APIStyleResponses, Responses: ResponsesConfig{Dialect: ResponsesDialectLite}, ExecEncoding: ExecEncodingFreeform, ReasoningEfforts: []string{"high"}},
		"missing provider model":       {Connection: "provider", API: APIStyleResponses, Responses: ResponsesConfig{Dialect: ResponsesDialectLite}, ExecEncoding: ExecEncodingFreeform, ReasoningEfforts: []string{"high"}},
		"missing api":                  {Connection: "provider", ProviderModel: "test-model", Responses: ResponsesConfig{Dialect: ResponsesDialectLite}, ExecEncoding: ExecEncodingFreeform, ReasoningEfforts: []string{"high"}},
		"unsupported api":              {Connection: "provider", ProviderModel: "test-model", API: "unknown", ExecEncoding: ExecEncodingJSON, ReasoningEfforts: []string{"high"}},
		"missing responses config":     {Connection: "provider", ProviderModel: "test-model", API: APIStyleResponses, ExecEncoding: ExecEncodingFreeform, ReasoningEfforts: []string{"high"}},
		"unsupported responses mode":   {Connection: "provider", ProviderModel: "test-model", API: APIStyleResponses, Responses: ResponsesConfig{Dialect: "unknown"}, ExecEncoding: ExecEncodingFreeform, ReasoningEfforts: []string{"high"}},
		"missing exec encoding":        {Connection: "provider", ProviderModel: "test-model", API: APIStyleResponses, Responses: ResponsesConfig{Dialect: ResponsesDialectLite}, ReasoningEfforts: []string{"high"}},
		"unsupported exec encoding":    {Connection: "provider", ProviderModel: "test-model", API: APIStyleResponses, Responses: ResponsesConfig{Dialect: ResponsesDialectLite}, ExecEncoding: "yaml", ReasoningEfforts: []string{"high"}},
		"anthropic needs config":       {Connection: "provider", ProviderModel: "test-model", API: APIStyleAnthropicMessages, ExecEncoding: ExecEncodingJSON, ReasoningEfforts: []string{"high"}},
		"anthropic requires json exec": {Connection: "provider", ProviderModel: "test-model", API: APIStyleAnthropicMessages, AnthropicMessages: &AnthropicMessagesConfig{MaxTokens: 4096}, ExecEncoding: ExecEncodingFreeform, ReasoningEfforts: []string{"high"}},
	} {
		t.Run(name, func(t *testing.T) {
			s := fixtureSettings()
			s.Models["test-model"] = config
			if err := s.Validate(); err == nil {
				t.Fatalf("accepted model configuration %+v", config)
			}
		})
	}
}

func TestAnthropicSettingsResolveNamedConnectionAndEnforceControls(t *testing.T) {
	s := fixtureSettings()
	connection := s.Connections["provider"]
	connection.Auth.Type = AuthTypeAnthropicAPIKey
	s.Connections["provider"] = connection
	s.Models["test-model"] = ModelConfig{
		Connection:       "provider",
		ProviderModel:    "claude-test",
		API:              APIStyleAnthropicMessages,
		ExecEncoding:     ExecEncodingJSON,
		ReasoningEfforts: []string{"high"},
		AnthropicMessages: &AnthropicMessagesConfig{
			MaxTokens: 4096,
			Thinking: &AnthropicThinkingConfig{
				Type:         "adaptive",
				BlockBinding: &AnthropicBlockBindingConfig{PrefixMismatchBehavior: "drop_block"},
			},
			PromptCache: &AnthropicPromptCacheConfig{TTL: "1h"},
			Compaction:  &AnthropicCompactionConfig{MaxTokens: 12000, KeepRecentTurns: 1},
		},
	}
	if err := s.Validate(); err != nil {
		t.Fatal(err)
	}
	resolved, err := s.Connection("test-model")
	if err != nil {
		t.Fatal(err)
	}
	if resolved.BaseURL != "https://model.invalid" || resolved.Auth.Type != AuthTypeAnthropicAPIKey {
		t.Fatalf("resolved connection=%+v", resolved)
	}
	encoded, err := json.Marshal(s.Models["test-model"])
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), `"responses"`) || !strings.Contains(string(encoded), `"block_binding":{"prefix_mismatch_behavior":"drop_block"}`) {
		t.Fatalf("unexpected Anthropic model JSON: %s", encoded)
	}

	for name, edit := range map[string]func(*ModelConfig){
		"responses dialect is API-specific": func(config *ModelConfig) {
			config.Responses = ResponsesConfig{Dialect: ResponsesDialectLite}
		},
		"positive token limit": func(config *ModelConfig) { config.AnthropicMessages.MaxTokens = 0 },
		"enabled thinking requires a budget": func(config *ModelConfig) {
			config.AnthropicMessages.Thinking = &AnthropicThinkingConfig{Type: "enabled"}
		},
		"enabled thinking budget is below the output limit": func(config *ModelConfig) {
			budget := config.AnthropicMessages.MaxTokens
			config.AnthropicMessages.Thinking = &AnthropicThinkingConfig{Type: "enabled", BudgetTokens: &budget}
		},
		"block binding mode is closed": func(config *ModelConfig) {
			config.AnthropicMessages.Thinking.BlockBinding.PrefixMismatchBehavior = "ignore"
		},
		"block binding requires adaptive thinking": func(config *ModelConfig) {
			config.AnthropicMessages.Thinking.Type = "disabled"
		},
		"prompt cache TTL is closed": func(config *ModelConfig) {
			config.AnthropicMessages.PromptCache = &AnthropicPromptCacheConfig{TTL: "30m"}
		},
		"recent turns cannot be negative": func(config *ModelConfig) {
			config.AnthropicMessages.Compaction.KeepRecentTurns = -1
		},
		"per-message effort needs adaptive thinking": func(config *ModelConfig) {
			config.AnthropicMessages.PerMessageEffort = true
			config.AnthropicMessages.Thinking.Type = "disabled"
			config.AnthropicMessages.Thinking.BlockBinding = nil
		},
		"Anthropic efforts are explicit levels": func(config *ModelConfig) {
			config.ReasoningEfforts = []string{""}
		},
	} {
		t.Run(name, func(t *testing.T) {
			invalid := s.Models["test-model"]
			invalid.AnthropicMessages = copyAnthropicMessages(s.Models["test-model"].AnthropicMessages)
			edit(&invalid)
			if err := invalid.Validate(); err == nil {
				t.Fatal("accepted invalid Anthropic model configuration")
			}
		})
	}
}

func copyAnthropicMessages(config *AnthropicMessagesConfig) *AnthropicMessagesConfig {
	copy := *config
	if config.Thinking != nil {
		thinking := *config.Thinking
		if thinking.BlockBinding != nil {
			blockBinding := *thinking.BlockBinding
			thinking.BlockBinding = &blockBinding
		}
		copy.Thinking = &thinking
	}
	if config.PromptCache != nil {
		promptCache := *config.PromptCache
		copy.PromptCache = &promptCache
	}
	if config.Compaction != nil {
		compaction := *config.Compaction
		copy.Compaction = &compaction
	}
	return &copy
}

func TestModelConfigCompatibilityTracksWireSemantics(t *testing.T) {
	responses := fixtureSettings().Models["test-model"]
	if !responses.Compatible(responses) {
		t.Fatal("identical Responses routes should be compatible")
	}
	incompatibleResponses := responses
	incompatibleResponses.Responses.Dialect = ResponsesDialectStandard
	if responses.Compatible(incompatibleResponses) {
		t.Fatal("different Responses dialects should not be compatible")
	}
	incompatibleResponses = responses
	incompatibleResponses.Connection = "other-provider"
	if responses.Compatible(incompatibleResponses) {
		t.Fatal("different provider connections should not be compatible")
	}
	changedProviderModel := responses
	changedProviderModel.ProviderModel = "other-model"
	if !responses.Compatible(changedProviderModel) {
		t.Fatal("Responses routes with the same wire semantics may change provider model")
	}

	anthropic := ModelConfig{
		Connection: "anthropic", ProviderModel: "claude-test", API: APIStyleAnthropicMessages, ExecEncoding: ExecEncodingJSON,
		AnthropicMessages: &AnthropicMessagesConfig{MaxTokens: 4096, Thinking: &AnthropicThinkingConfig{Type: "adaptive"}},
	}
	if !anthropic.Compatible(anthropic) {
		t.Fatal("identical Anthropic configurations should be compatible")
	}
	incompatibleAnthropic := anthropic
	incompatibleAnthropic.AnthropicMessages = copyAnthropicMessages(anthropic.AnthropicMessages)
	incompatibleAnthropic.AnthropicMessages.Thinking.Type = "disabled"
	if anthropic.Compatible(incompatibleAnthropic) {
		t.Fatal("different Anthropic thinking modes should not be compatible")
	}
	incompatibleAnthropic = anthropic
	incompatibleAnthropic.ProviderModel = "claude-other"
	if anthropic.Compatible(incompatibleAnthropic) {
		t.Fatal("Anthropic routes bound to different provider models should not be compatible")
	}
	incompatibleAnthropic = anthropic
	incompatibleAnthropic.Connection = "other-anthropic"
	if anthropic.Compatible(incompatibleAnthropic) {
		t.Fatal("Anthropic routes on different connections should not be compatible")
	}
}

func TestSettingsRejectPartialAndUnknownConfiguration(t *testing.T) {
	for name, edit := range map[string]func(*Settings){
		"history token missing": func(s *Settings) { s.HistoryTarget = "history.invalid:8080" },
		"gateway token missing": func(s *Settings) {
			s.DirectExecutorTarget = ""
			s.DirectExecutorInsecureLocaltest = false
			s.GatewayTarget = "gateway.invalid:8080"
		},
		"two executor transports":              func(s *Settings) { s.GatewayTarget = "gateway.invalid:8080"; s.GatewayTokenFile = "/fixture/token" },
		"no executor transport":                func(s *Settings) { s.DirectExecutorTarget = ""; s.DirectExecutorInsecureLocaltest = false },
		"insecure non-loopback direct target":  func(s *Settings) { s.DirectExecutorTarget = "executor.invalid:4501" },
		"insecure direct target with hostname": func(s *Settings) { s.DirectExecutorTarget = "localhost:4501" },
		"insecure direct target with bad port": func(s *Settings) { s.DirectExecutorTarget = "127.0.0.1:grpc" },
		"direct target without localtest mode": func(s *Settings) { s.DirectExecutorInsecureLocaltest = false },
		"unknown template": func(s *Settings) {
			s.DirectExecutorTarget = ""
			s.DirectExecutorInsecureLocaltest = false
			s.GatewayTarget = "{host}:8080"
			s.GatewayTokenFile = "/fixture/token"
		},
		"missing connection URL": func(s *Settings) {
			connection := s.Connections["provider"]
			connection.BaseURL = ""
			s.Connections["provider"] = connection
		},
		"invalid URL with credentials": func(s *Settings) {
			connection := s.Connections["provider"]
			connection.BaseURL = "https://user:password@model.invalid"
			s.Connections["provider"] = connection
		},
		"invalid URL with query": func(s *Settings) {
			connection := s.Connections["provider"]
			connection.BaseURL = "https://model.invalid/?token=secret"
			s.Connections["provider"] = connection
		},
		"invalid URL with fragment": func(s *Settings) {
			connection := s.Connections["provider"]
			connection.BaseURL = "https://model.invalid/#fragment"
			s.Connections["provider"] = connection
		},
		"unknown model connection": func(s *Settings) {
			config := s.Models["test-model"]
			config.Connection = "missing"
			s.Models["test-model"] = config
		},
		"incompatible connection auth": func(s *Settings) {
			connection := s.Connections["provider"]
			connection.Auth.Type = AuthTypeAnthropicAPIKey
			s.Connections["provider"] = connection
		},
		"no model efforts": func(s *Settings) {
			config := s.Models["test-model"]
			config.ReasoningEfforts = nil
			s.Models["test-model"] = config
		},
		"no models":       func(s *Settings) { s.Models = nil },
		"zero capacity":   func(s *Settings) { s.MaxLiveSessions = 0 },
		"invalid account": func(s *Settings) { s.AllowedAccounts = []string{"test/client/other"} },
		"credential missing": func(s *Settings) {
			connection := s.Connections["provider"]
			connection.Auth.Credential = ""
			s.Connections["provider"] = connection
		},
		"credentials duplicated": func(s *Settings) {
			connection := s.Connections["provider"]
			connection.Auth.CredentialFile = "/fixture/token"
			s.Connections["provider"] = connection
		},
		"unsupported auth type": func(s *Settings) {
			connection := s.Connections["provider"]
			connection.Auth.Type = "basic"
			s.Connections["provider"] = connection
		},
		"empty connections": func(s *Settings) { s.Connections = nil },
	} {
		t.Run(name, func(t *testing.T) {
			s := fixtureSettings()
			edit(&s)
			if s.Validate() == nil {
				t.Fatal("accepted invalid settings")
			}
		})
	}
	s := fixtureSettings()
	encoded, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	invalid := strings.TrimSuffix(string(encoded), "}") + `,"unknown":true}`
	if _, err := DecodeSettings(strings.NewReader(invalid)); err == nil {
		t.Fatal("accepted unknown setting")
	}
	invalid = strings.Replace(string(encoded), `"test-credential"`, `{"secret":"test-credential"}`, 1)
	if _, err := DecodeSettings(strings.NewReader(invalid)); err == nil {
		t.Fatal("accepted invalid credential type")
	}
	if _, err := DecodeSettings(strings.NewReader(string(encoded) + ` {}`)); err == nil {
		t.Fatal("accepted a second configuration object")
	}
}

func TestLoadSettingsUsesDatabaseURLEnvironmentOverride(t *testing.T) {
	settings := fixtureSettings()
	encoded, err := json.Marshal(settings)
	if err != nil {
		t.Fatal(err)
	}
	var values map[string]any
	if err := json.Unmarshal(encoded, &values); err != nil {
		t.Fatal(err)
	}
	delete(values, "database_url")
	encoded, err = json.Marshal(values)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "settings.json")
	if err := os.WriteFile(path, encoded, 0o600); err != nil {
		t.Fatal(err)
	}
	want := "postgresql://rotated-user:rotated-password@database.invalid/sampler"
	t.Setenv("AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL", want)
	got, err := LoadSettings(path)
	if err != nil {
		t.Fatal(err)
	}
	if got.DatabaseURL != want {
		t.Fatalf("database URL=%q, want environment override %q", got.DatabaseURL, want)
	}
}

func TestSettingsReadRotatingCredentialFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte("first-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	auth := AuthConfig{Type: AuthTypeBearer, CredentialFile: path}
	first, err := auth.ReadCredential()
	if err != nil {
		t.Fatal(err)
	}
	if first != "first-token" {
		t.Fatalf("first credential=%q", first)
	}
	if err := os.WriteFile(path, []byte("second-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	second, err := auth.ReadCredential()
	if err != nil {
		t.Fatal(err)
	}
	if second != "second-token" {
		t.Fatalf("second credential=%q after projected file rotation", second)
	}
}

func TestGatewayTemplateCannotInjectAddress(t *testing.T) {
	s := fixtureSettings()
	s.DirectExecutorTarget = ""
	s.DirectExecutorInsecureLocaltest = false
	s.GatewayTarget = "{sandbox}-control.{namespace}.svc.cluster.local:8080"
	s.GatewayTokenFile = "/fixture/token"
	address, err := s.GatewayAddress("test", "executor")
	if err != nil || address != "executor-control.test.svc.cluster.local:8080" {
		t.Fatalf("address=%q err=%v", address, err)
	}
	for _, name := range []string{"bad.example", "UPPER", "", strings.Repeat("a", 64), "../other", "elsewhere:1234/"} {
		if _, err := s.GatewayAddress("test", name); err == nil {
			t.Fatalf("accepted executor name %q", name)
		}
	}
}
