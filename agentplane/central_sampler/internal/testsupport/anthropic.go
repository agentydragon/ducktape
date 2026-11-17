package testsupport

import (
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/anthropic"
)

// AnthropicTestConfig returns an Anthropic Messages model configuration with prompt caching and adaptive thinking.
func AnthropicTestConfig() samplersettings.ModelConfig {
	return samplersettings.ModelConfig{
		Connection: "anthropic", ProviderModel: "claude-test", API: protocol.APIStyleAnthropicMessages,
		ExecEncoding: protocol.ExecEncodingJSON, ReasoningEfforts: []string{"low", "high"},
		AnthropicMessages: &anthropic.Config{
			MaxTokens:   4096,
			Thinking:    &anthropic.ThinkingConfig{Type: "adaptive", BlockBinding: &anthropic.BlockBindingConfig{PrefixMismatchBehavior: "error"}},
			PromptCache: &anthropic.PromptCacheConfig{TTL: "1h"}, CacheDiagnostics: true, PerMessageEffort: true,
			StrictToolInput: true, EagerInputStreaming: true,
		},
	}
}
