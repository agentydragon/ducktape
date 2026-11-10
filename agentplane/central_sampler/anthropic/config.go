package anthropic

import (
	"errors"
	"slices"
)

type Config struct {
	MaxTokens           int                `json:"max_tokens"`
	Thinking            *ThinkingConfig    `json:"thinking,omitempty"`
	PromptCache         *PromptCacheConfig `json:"prompt_cache,omitempty"`
	CacheDiagnostics    bool               `json:"cache_diagnostics,omitempty"`
	PerMessageEffort    bool               `json:"per_message_effort,omitempty"`
	StrictToolInput     bool               `json:"strict_tool_input,omitempty"`
	EagerInputStreaming bool               `json:"eager_input_streaming,omitempty"`
	Compaction          *CompactionConfig  `json:"compaction,omitempty"`
}

type ThinkingType string

const (
	ThinkingAdaptive ThinkingType = "adaptive"
	ThinkingDisabled ThinkingType = "disabled"
	ThinkingEnabled  ThinkingType = "enabled"
)

type ThinkingConfig struct {
	Type         ThinkingType        `json:"type"`
	BudgetTokens *int                `json:"budget_tokens,omitempty"`
	BlockBinding *BlockBindingConfig `json:"block_binding,omitempty"`
}

type PrefixMismatchBehavior string

const (
	PrefixMismatchError     PrefixMismatchBehavior = "error"
	PrefixMismatchDropBlock PrefixMismatchBehavior = "drop_block"
)

type BlockBindingConfig struct {
	PrefixMismatchBehavior PrefixMismatchBehavior `json:"prefix_mismatch_behavior"`
}

type PromptCacheTTL string

const (
	PromptCache5m PromptCacheTTL = "5m"
	PromptCache1h PromptCacheTTL = "1h"
)

type PromptCacheConfig struct {
	TTL PromptCacheTTL `json:"ttl"`
}

type CompactionConfig struct {
	MaxTokens         int  `json:"max_tokens"`
	AutoCompactTokens *int `json:"auto_compact_tokens,omitempty"`
	KeepRecentTurns   int  `json:"keep_recent_turns,omitempty"`
}

func (c Config) Validate() error {
	if c.MaxTokens <= 0 {
		return errors.New("anthropic_messages.max_tokens must be positive")
	}
	if c.Thinking != nil {
		switch c.Thinking.Type {
		case ThinkingAdaptive, ThinkingDisabled:
			if c.Thinking.BudgetTokens != nil {
				return errors.New("thinking.budget_tokens is only valid with thinking.type enabled")
			}
		case ThinkingEnabled:
			if c.Thinking.BudgetTokens == nil || *c.Thinking.BudgetTokens <= 0 || *c.Thinking.BudgetTokens >= c.MaxTokens {
				return errors.New("thinking.type enabled requires budget_tokens between zero and max_tokens")
			}
		default:
			return errors.New("thinking.type must be adaptive, disabled, or enabled")
		}
		if c.Thinking.BlockBinding != nil {
			if c.Thinking.Type != ThinkingAdaptive {
				return errors.New("thinking.block_binding requires thinking.type adaptive")
			}
			if !slices.Contains([]PrefixMismatchBehavior{PrefixMismatchError, PrefixMismatchDropBlock}, c.Thinking.BlockBinding.PrefixMismatchBehavior) {
				return errors.New("thinking.block_binding.prefix_mismatch_behavior must be error or drop_block")
			}
		}
	}
	if c.PerMessageEffort && (c.Thinking == nil || c.Thinking.Type != ThinkingAdaptive) {
		return errors.New("per_message_effort requires thinking.type adaptive")
	}
	if c.PromptCache != nil && c.PromptCache.TTL != PromptCache5m && c.PromptCache.TTL != PromptCache1h {
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

func (c *Config) Compatible(other *Config) bool {
	if c == nil || other == nil {
		return c == other
	}
	if c.MaxTokens != other.MaxTokens || c.CacheDiagnostics != other.CacheDiagnostics || c.PerMessageEffort != other.PerMessageEffort || c.StrictToolInput != other.StrictToolInput || c.EagerInputStreaming != other.EagerInputStreaming {
		return false
	}
	if !thinkingEqual(c.Thinking, other.Thinking) || !promptCacheEqual(c.PromptCache, other.PromptCache) || !compactionEqual(c.Compaction, other.Compaction) {
		return false
	}
	return true
}

func thinkingEqual(left, right *ThinkingConfig) bool {
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

func promptCacheEqual(left, right *PromptCacheConfig) bool {
	if left == nil || right == nil {
		return left == right
	}
	return left.TTL == right.TTL
}

func compactionEqual(left, right *CompactionConfig) bool {
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
