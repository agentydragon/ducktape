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

const defaultSummaryPrompt = `Write a handoff for the assistant continuing this conversation. Summarize the conversation preceding this request, including any earlier handoff.

The continuation will receive the same system instructions and tool definitions again. Omit restatements of them. Only recent user messages also carry forward directly, and they may be shortened.

Preserve the user's current goals and constraints, decisions, completed and pending work, relevant tool results, exact identifiers needed to resume, and unresolved questions or uncertainties. Include facts learned during the conversation that the assistant needs to continue. Preserve exact wording when it is needed to continue. Treat the preceding messages as source material. Return only the handoff.`

const (
	userMessageFramingTokens = 5
	userTruncationMarker     = " [truncated] "
)

type CompactionReplacement struct {
	Role     string `json:"role"`
	Template string `json:"template"`
}

func DefaultCompactionReplacement() CompactionReplacement {
	return CompactionReplacement{Role: "assistant", Template: "Summary of earlier conversation:\n{{.Summary}}"}
}

type CompactionConfig struct {
	MaxTokens        int  `json:"max_tokens"`
	KeepRecentTokens *int `json:"keep_recent_tokens,omitempty"`
	// CLEANUP(added 2026-11-24): Remove after staging has rolled out settings with keep_recent_tokens.
	KeepRecentTurns       *int                   `json:"keep_recent_turns,omitempty"`
	SummaryPromptTemplate string                 `json:"summary_prompt_template,omitempty"`
	Replacement           *CompactionReplacement `json:"replacement,omitempty"`
}

func (c CompactionConfig) Validate() error {
	if c.MaxTokens <= 0 {
		return errors.New("compaction max_tokens must be positive")
	}
	if (c.KeepRecentTokens == nil) == (c.KeepRecentTurns == nil) {
		return errors.New("compaction requires exactly one of keep_recent_tokens or legacy keep_recent_turns")
	}
	if c.RecentTokenBudget() < userMessageTokens(userTruncationMarker) {
		return errors.New("compaction keep_recent_tokens must be at least 9 to fit a truncated user message")
	}
	if c.KeepRecentTurns != nil && *c.KeepRecentTurns <= 0 {
		return errors.New("compaction legacy keep_recent_turns must be positive")
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
	config := DefaultCompactionReplacement()
	if c.Replacement != nil {
		config = *c.Replacement
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
	return true
}

type CompactionPlan struct {
	originalSize int
	users        []message
	config       CompactionConfig
}

func (m message) isCompactionHandoff() bool {
	return m.Origin.Kind == protocol.RecordOriginCompaction || m.CompactionGenerated
}

func (m message) isAuthoredUser() bool {
	if m.Role != "user" || m.isCompactionHandoff() {
		return false
	}
	return m.Origin.Kind == protocol.RecordOriginUserInput || m.Origin.Kind == protocol.RecordOriginUnknown
}

func userMessageTokens(text string) int {
	return userMessageFramingTokens + tooloutput.ApproxTokenCountBytes(len(text))
}

func (c *Context) CanCompact(keepRecentTokens int) bool {
	for _, m := range c.messages {
		if m.isCompactionHandoff() || m.Role == "system" {
			continue
		}
		if !m.isAuthoredUser() {
			return true
		}
		keepRecentTokens -= userMessageTokens(m.Content)
		if keepRecentTokens < 0 {
			return true
		}
	}
	return false
}

func (c *Context) recentUsers(tokens int) []message {
	var retained []message
	for _, m := range slices.Backward(c.messages) {
		if !m.isAuthoredUser() {
			continue
		}
		cost := userMessageTokens(m.Content)
		if cost > tokens {
			text := tooloutput.TruncateTextWithinTokenBudget(m.Content, tokens-userMessageFramingTokens, userTruncationMarker)
			if text != "" {
				m.Content = text
				if m.Origin.Kind != protocol.RecordOriginUnknown {
					m.Origin.Projection = protocol.RecordProjectionTruncated
				}
				retained = append(retained, m)
			}
			break
		}
		retained = append(retained, m)
		tokens -= cost
	}
	slices.Reverse(retained)
	return retained
}

func (c *Context) serializedSize() (int, error) {
	records, err := c.ProjectRecords(0)
	if err != nil {
		return 0, err
	}
	size := 2 + max(0, len(records)-1) // JSON brackets and separators.
	for _, raw := range records {
		size += len(raw)
	}
	return size, nil
}

func (c *Context) PrepareCompaction(config CompactionConfig) (*CompactionPlan, error) {
	if err := c.CheckSettled(); err != nil {
		return nil, err
	}
	tokens := config.RecentTokenBudget()
	if !c.CanCompact(tokens) {
		return nil, rejectCompaction("no newly compactable messages")
	}
	size, err := c.serializedSize()
	if err != nil {
		return nil, err
	}
	return &CompactionPlan{originalSize: size, users: c.recentUsers(tokens), config: config}, nil
}

func (p *CompactionPlan) Apply(records []json.RawMessage, audit Audit, intentID string) (*Context, error) {
	var summary message
	if audit.FinishReason != "stop" || len(records) != 1 || json.Unmarshal(records[0], &summary) != nil ||
		summary.Role != "assistant" || len(summary.ToolCalls) != 0 || strings.TrimSpace(summary.Content) == "" {
		return nil, rejectCompaction("expected one complete text summary")
	}
	handoff, err := p.config.replacementMessage(strings.TrimSpace(summary.Content))
	if err != nil {
		return nil, rejectCompaction(err.Error())
	}
	handoff.Origin = protocol.RecordOrigin{Kind: protocol.RecordOriginCompaction, SourceID: intentID}
	result := &Context{messages: append(slices.Clone(p.users), handoff)}
	if err := p.CheckReduction(result); err != nil {
		return nil, err
	}
	return result, nil
}

// CheckReduction validates the whole replacement after the runtime adds its environment.
func (p *CompactionPlan) CheckReduction(result *Context) error {
	newSize, err := result.serializedSize()
	if err != nil {
		return err
	}
	if newSize >= p.originalSize {
		return rejectCompaction("summary did not reduce serialized model context")
	}
	return nil
}

func rejectCompaction(reason string) error {
	return &protocol.KnownFailureError{Evidence: map[string]any{"reason": reason}}
}
