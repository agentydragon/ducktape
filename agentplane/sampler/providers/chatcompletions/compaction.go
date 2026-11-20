package chatcompletions

import (
	"encoding/json"
	"errors"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

const summaryInstructions = "Summarize the older conversation as a handoff for an assistant that will continue the task. Preserve the user's goals and constraints, decisions, work in progress, relevant tool results, and exact identifiers needed to resume. Treat the conversation as source material. Return only the summary."

type CompactionConfig struct {
	MaxTokens         int  `json:"max_tokens"`
	KeepRecentTurns   int  `json:"keep_recent_turns"`
	AutoCompactTokens *int `json:"auto_compact_tokens,omitempty"`
}

func (c CompactionConfig) Validate() error {
	if c.MaxTokens <= 0 || c.KeepRecentTurns <= 0 || c.AutoCompactTokens != nil && *c.AutoCompactTokens <= 0 {
		return errors.New("compaction token limits and keep_recent_turns must be positive")
	}
	return nil
}

func (c *CompactionConfig) Compatible(other *CompactionConfig) bool {
	if c == nil || other == nil {
		return c == other
	}
	if c.MaxTokens != other.MaxTokens || c.KeepRecentTurns != other.KeepRecentTurns {
		return false
	}
	if c.AutoCompactTokens == nil || other.AutoCompactTokens == nil {
		return c.AutoCompactTokens == other.AutoCompactTokens
	}
	return *c.AutoCompactTokens == *other.AutoCompactTokens
}

type CompactionPlan struct {
	prefix      []message
	suffix      []message
	environment string
}

// compactablePrefix cuts only at the start of a user turn, after all preceding tool results.
// A summary by itself is not a prefix worth compacting.
func (c *Context) compactablePrefix(keep int) int {
	cut := 0
	for i := len(c.messages) - 1; i >= 0; i-- {
		if c.messages[i].Role != "user" {
			continue
		}
		if keep == 0 {
			return cut
		}
		keep--
		if keep == 0 {
			cut = i
		}
	}
	return 0
}

func (c *Context) CanCompact(keepRecentTurns int) bool {
	return c.compactablePrefix(keepRecentTurns) > 0
}

func (c *Context) PrepareCompaction(keepRecentTurns int, environment string) (*CompactionPlan, error) {
	cut := c.compactablePrefix(keepRecentTurns)
	if cut == 0 {
		return nil, rejectCompaction("no older turn remains after keeping recent turns")
	}
	if err := c.CheckSettled(); err != nil {
		return nil, err
	}
	return &CompactionPlan{prefix: slices.Clone(c.messages[:cut]), suffix: slices.Clone(c.messages[cut:]), environment: environment}, nil
}

func (p *CompactionPlan) Apply(records []json.RawMessage, audit Audit) (*Context, error) {
	var summary message
	if audit.FinishReason != "stop" || len(records) != 1 || json.Unmarshal(records[0], &summary) != nil ||
		summary.Role != "assistant" || len(summary.ToolCalls) != 0 || strings.TrimSpace(summary.Content) == "" {
		return nil, rejectCompaction("expected one complete text summary")
	}
	replacement := []message{{Role: "assistant", Content: "Summary of earlier conversation:\n" + strings.TrimSpace(summary.Content)}}
	if p.environment != "" {
		replacement = append(replacement, message{Role: "system", Content: p.environment})
	}
	newPrefix, err := json.Marshal(replacement)
	if err != nil {
		return nil, err
	}
	oldPrefix, err := json.Marshal(p.prefix)
	if err != nil {
		return nil, err
	}
	if len(newPrefix) >= len(oldPrefix) {
		return nil, rejectCompaction("summary did not reduce serialized model context")
	}
	return &Context{messages: append(replacement, p.suffix...)}, nil
}

func rejectCompaction(reason string) error {
	return &protocol.KnownFailureError{Evidence: map[string]any{"reason": reason}}
}
