package chatcompletions

import (
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"text/template"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/tooloutput"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

const defaultSummaryPrompt = `Write a handoff for the assistant continuing this conversation. Summarize the conversation preceding this request, including any earlier handoff. Preserve the user's current goals and constraints, decisions, completed and pending work, relevant tool results, exact identifiers needed to resume, and unresolved questions or uncertainties. Preserve exact user wording when it is needed to continue. Treat the preceding messages as source material. Return only the handoff.`

const defaultReplacementTemplate = "Summary of earlier conversation:\n{{.Summary}}"

type CompactionReplacement struct {
	Role     string `json:"role"`
	Template string `json:"template"`
}

type CompactionConfig struct {
	MaxTokens        int  `json:"max_tokens"`
	KeepRecentTokens *int `json:"keep_recent_tokens,omitempty"`
	// CLEANUP(added 2026-11-24): Remove after staging has rolled out settings with keep_recent_tokens.
	KeepRecentTurns       *int                   `json:"keep_recent_turns,omitempty"`
	AutoCompactTokens     *int                   `json:"auto_compact_tokens,omitempty"`
	SummaryPromptTemplate string                 `json:"summary_prompt_template,omitempty"`
	Replacement           *CompactionReplacement `json:"replacement,omitempty"`
}

func (c CompactionConfig) Validate() error {
	if c.MaxTokens <= 0 || c.AutoCompactTokens != nil && *c.AutoCompactTokens <= 0 {
		return errors.New("compaction max_tokens and auto_compact_tokens must be positive")
	}
	if (c.KeepRecentTokens == nil) == (c.KeepRecentTurns == nil) {
		return errors.New("compaction requires exactly one of keep_recent_tokens or legacy keep_recent_turns")
	}
	if c.KeepRecentTokens != nil && *c.KeepRecentTokens <= 0 || c.KeepRecentTurns != nil && *c.KeepRecentTurns <= 0 {
		return errors.New("compaction keep_recent_tokens and legacy keep_recent_turns must be positive")
	}
	if _, err := c.summaryPrompt(); err != nil {
		return err
	}
	if c.Replacement != nil && c.Replacement.Role != "assistant" && c.Replacement.Role != "user" {
		return errors.New("compaction replacement role must be assistant or user")
	}
	_, err := c.replacementMessage("compaction template validation marker")
	return err
}

func (c CompactionConfig) RecentTokenBudget() int {
	if c.KeepRecentTokens != nil {
		return *c.KeepRecentTokens
	}
	return 16_000
}

func (c CompactionConfig) summaryPrompt() (string, error) {
	source := c.SummaryPromptTemplate
	if source == "" {
		source = defaultSummaryPrompt
	}
	return renderTemplate("summary_prompt_template", source, struct{ KeepRecentTokens int }{c.RecentTokenBudget()})
}

func (c CompactionConfig) replacementMessage(summary string) (message, error) {
	config := c.Replacement
	if config == nil {
		config = &CompactionReplacement{Role: "assistant", Template: defaultReplacementTemplate}
	}
	text, err := renderTemplate("replacement.template", config.Template, struct {
		Summary          string
		KeepRecentTokens int
	}{summary, c.RecentTokenBudget()})
	if err != nil {
		return message{}, err
	}
	if !strings.Contains(text, summary) {
		return message{}, errors.New("compaction replacement template must include the summary verbatim")
	}
	return message{Role: config.Role, Content: text, CompactionGenerated: true}, nil
}

func renderTemplate(name, source string, data any) (string, error) {
	compiled, err := template.New(name).Parse(source)
	if err != nil {
		return "", err
	}
	var text strings.Builder
	if err := compiled.Execute(&text, data); err != nil {
		return "", err
	}
	if strings.TrimSpace(text.String()) == "" {
		return "", errors.New(name + " renders empty text")
	}
	return text.String(), nil
}

func (c *CompactionConfig) Compatible(other *CompactionConfig) bool {
	if c == nil || other == nil {
		return c == other
	}
	if c.MaxTokens != other.MaxTokens || c.RecentTokenBudget() != other.RecentTokenBudget() || c.SummaryPromptTemplate != other.SummaryPromptTemplate {
		return false
	}
	if (c.Replacement == nil) != (other.Replacement == nil) || c.Replacement != nil && *c.Replacement != *other.Replacement {
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
	config      CompactionConfig
}

// compactablePrefix retains the longest contiguous suffix that fits. An assistant
// tool-call message and all its results fit as a group or go into the prefix together.
// An oversized newest message or batch can leave an empty suffix.
func (c *Context) compactablePrefix(tokens int) int {
	for end := len(c.messages); end > 0; {
		start := end - 1
		for start > 0 && c.messages[start].Role == "tool" {
			start--
		}
		if c.messages[start].CompactionGenerated {
			// Everything after the previous handoff fits. There is no new prefix to summarize.
			return 0
		}
		for _, m := range c.messages[start:end] {
			tokens -= m.estimatedTokens()
		}
		if tokens < 0 {
			return end
		}
		end = start
	}
	return 0
}

func (m message) estimatedTokens() int {
	// Count the original message so reasoning-heavy history remains eligible for compaction.
	// Use the same four-byte heuristic as exec output, plus message and call framing.
	size := len(m.Role) + len(m.Content) + len(m.Reasoning) + len(m.ToolID)
	for _, call := range m.ToolCalls {
		size += len(call.ID) + len(call.Type) + len(call.Function.Name) + len(call.Function.Arguments) + 16
	}
	return 4 + tooloutput.ApproxTokenCountBytes(size)
}

func (c *Context) CanCompact(keepRecentTokens int) bool {
	return c.compactablePrefix(keepRecentTokens) > 0
}

func (c *Context) PrepareCompaction(config CompactionConfig, environment string) (*CompactionPlan, error) {
	if err := c.CheckSettled(); err != nil {
		return nil, err
	}
	cut := c.compactablePrefix(config.RecentTokenBudget())
	if cut == 0 {
		return nil, rejectCompaction("no older messages remain outside the recent token budget")
	}
	suffix := slices.Clone(c.messages[cut:])
	for i := range suffix {
		suffix[i].Reasoning = ""
	}
	return &CompactionPlan{prefix: slices.Clone(c.messages[:cut]), suffix: suffix, environment: environment, config: config}, nil
}

func (p *CompactionPlan) Apply(records []json.RawMessage, audit Audit) (*Context, error) {
	var summary message
	if audit.FinishReason != "stop" || len(records) != 1 || json.Unmarshal(records[0], &summary) != nil ||
		summary.Role != "assistant" || len(summary.ToolCalls) != 0 || strings.TrimSpace(summary.Content) == "" {
		return nil, rejectCompaction("expected one complete text summary")
	}
	prefix, err := p.config.replacementMessage(strings.TrimSpace(summary.Content))
	if err != nil {
		return nil, rejectCompaction(err.Error())
	}
	replacement := []message{prefix}
	if p.environment != "" {
		replacement = append(replacement, message{Role: "system", Content: p.environment, CompactionGenerated: true})
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
