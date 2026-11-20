package chatcompletions

import (
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"text/template"

	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

const defaultSummaryPrompt = `Summarize the older conversation as a handoff for an assistant that will continue the task. Preserve the user's goals and constraints, decisions, work in progress, relevant tool results, and exact identifiers needed to resume. Treat the conversation as source material. Return only the summary. The last {{.KeepRecentTurns}} user turns before this request will be retained verbatim. Focus the summary on the earlier turns.`

const defaultReplacementTemplate = "Summary of earlier conversation:\n{{.Summary}}"

type CompactionReplacement struct {
	Role     string `json:"role"`
	Template string `json:"template"`
}

type CompactionConfig struct {
	MaxTokens             int                    `json:"max_tokens"`
	KeepRecentTurns       int                    `json:"keep_recent_turns"`
	AutoCompactTokens     *int                   `json:"auto_compact_tokens,omitempty"`
	SummaryPromptTemplate string                 `json:"summary_prompt_template,omitempty"`
	Replacement           *CompactionReplacement `json:"replacement,omitempty"`
}

func (c CompactionConfig) Validate() error {
	if c.MaxTokens <= 0 || c.KeepRecentTurns <= 0 || c.AutoCompactTokens != nil && *c.AutoCompactTokens <= 0 {
		return errors.New("compaction token limits and keep_recent_turns must be positive")
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

func (c CompactionConfig) summaryPrompt() (string, error) {
	source := c.SummaryPromptTemplate
	if source == "" {
		source = defaultSummaryPrompt
	}
	return renderTemplate("summary_prompt_template", source, struct{ KeepRecentTurns int }{c.KeepRecentTurns})
}

func (c CompactionConfig) replacementMessage(summary string) (message, error) {
	config := c.Replacement
	if config == nil {
		config = &CompactionReplacement{Role: "assistant", Template: defaultReplacementTemplate}
	}
	text, err := renderTemplate("replacement.template", config.Template, struct {
		Summary         string
		KeepRecentTurns int
	}{summary, c.KeepRecentTurns})
	if err != nil {
		return message{}, err
	}
	if !strings.Contains(text, summary) {
		return message{}, errors.New("compaction replacement template must include the summary verbatim")
	}
	return message{Role: config.Role, Content: text, CompactionSummary: config.Role == "user"}, nil
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
	if c.MaxTokens != other.MaxTokens || c.KeepRecentTurns != other.KeepRecentTurns || c.SummaryPromptTemplate != other.SummaryPromptTemplate {
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

// compactablePrefix cuts only at the start of a user turn, after all preceding tool results.
// A summary by itself is not a prefix worth compacting.
func (c *Context) compactablePrefix(keep int) int {
	cut := 0
	for i := len(c.messages) - 1; i >= 0; i-- {
		if c.messages[i].Role != "user" || c.messages[i].CompactionSummary {
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

func (c *Context) PrepareCompaction(config CompactionConfig, environment string) (*CompactionPlan, error) {
	cut := c.compactablePrefix(config.KeepRecentTurns)
	if cut == 0 {
		return nil, rejectCompaction("no older turn remains after keeping recent turns")
	}
	if err := c.CheckSettled(); err != nil {
		return nil, err
	}
	return &CompactionPlan{prefix: slices.Clone(c.messages[:cut]), suffix: slices.Clone(c.messages[cut:]), environment: environment, config: config}, nil
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
