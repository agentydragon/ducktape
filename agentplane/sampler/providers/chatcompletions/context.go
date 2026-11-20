package chatcompletions

import (
	"bytes"
	"encoding/json"
	"fmt"
	"slices"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

type function struct {
	Name      string `json:"name,omitempty"`
	Arguments string `json:"arguments,omitempty"`
}

type toolCall struct {
	ID       string   `json:"id,omitempty"`
	Type     string   `json:"type,omitempty"`
	Function function `json:"function"`
}

type message struct {
	Role string `json:"role"`
	// A summary rendered as a user message is not an authored turn.
	CompactionSummary bool       `json:"sampler_compaction_summary,omitempty"`
	Content           string     `json:"content,omitempty"`
	Reasoning         string     `json:"reasoning_content,omitempty"`
	ToolCalls         []toolCall `json:"tool_calls,omitempty"`
	ToolID            string     `json:"tool_call_id,omitempty"`
}

// Chat Completions requires content for system, user and tool messages, even when empty.
func (m message) MarshalJSON() ([]byte, error) {
	type wire message
	if m.Role == "assistant" && len(m.ToolCalls) != 0 && m.Content == "" {
		return json.Marshal(wire(m))
	}
	return json.Marshal(struct {
		wire
		Content string `json:"content"`
	}{wire(m), m.Content})
}

type Context struct {
	messages []message
}

var _ protocol.Context = (*Context)(nil)

func (*Context) API() protocol.APIStyle { return protocol.APIStyleChatCompletions }
func (*Context) ChangeEffort(effort string) error {
	if effort != "" {
		return protocol.ErrPrecondition
	}
	return nil
}

func (c *Context) MarshalJSON() ([]byte, error)        { return json.Marshal(c.messages) }
func (c *Context) RecordCount() int                    { return len(c.messages) }
func (c *Context) Clone() protocol.Context             { return &Context{messages: slices.Clone(c.messages)} }
func (c *Context) Records() ([]json.RawMessage, error) { return c.RecordsFrom(0) }

func (c *Context) RecordsFrom(start int) ([]json.RawMessage, error) {
	return c.recordsFrom(start, false)
}

func (c *Context) ProjectRecords(start int) ([]json.RawMessage, error) {
	return c.recordsFrom(start, true)
}

func (c *Context) recordsFrom(start int, project bool) ([]json.RawMessage, error) {
	if start < 0 || start > len(c.messages) {
		return nil, protocol.ErrInvalid
	}
	records := make([]json.RawMessage, 0, len(c.messages)-start)
	for _, m := range c.messages[start:] {
		if project {
			m.CompactionSummary = false
		}
		raw, err := json.Marshal(m)
		if err != nil {
			return nil, err
		}
		records = append(records, raw)
	}
	return records, nil
}

func (c *Context) AppendRecords(records []json.RawMessage) error {
	candidate := slices.Clone(c.messages)
	for _, raw := range records {
		var m message
		if err := json.Unmarshal(raw, &m); err != nil {
			return err
		}
		candidate = append(candidate, m)
	}
	if _, _, err := inspect(candidate); err != nil {
		return err
	}
	c.messages = candidate
	return nil
}

func (c *Context) AppendUser(parts []protocol.ContentPart) error {
	return c.appendText("user", parts)
}

func (c *Context) AppendNotice(parts []protocol.ContentPart) error {
	return c.appendText("system", parts)
}

func (c *Context) appendText(role string, parts []protocol.ContentPart) error {
	text, err := textParts(parts)
	if err != nil {
		return err
	}
	c.messages = append(c.messages, message{Role: role, Content: text})
	return nil
}

func textParts(parts []protocol.ContentPart) (string, error) {
	text := make([]string, 0, len(parts))
	for _, part := range parts {
		if part.Type != "input_text" {
			return "", fmt.Errorf("Chat Completions does not support %q content: %w", part.Type, protocol.ErrInvalid)
		}
		text = append(text, part.Text)
	}
	return strings.Join(text, "\n"), nil
}

func (c *Context) AppendToolResult(call protocol.ToolInvocation, parts []protocol.ContentPart) error {
	calls, answered, err := inspect(c.messages)
	if err != nil {
		return err
	}
	index := slices.IndexFunc(calls, func(known protocol.ToolInvocation) bool { return known.CallID == call.CallID })
	if index < 0 {
		return protocol.ErrMissing
	}
	known := calls[index]
	if call.SourceRecord != 0 && known.SourceRecord != call.SourceRecord {
		return protocol.ErrInvalid
	}
	call.SourceRecord = known.SourceRecord
	if known != call {
		return protocol.ErrInvalid
	}
	text, err := textParts(parts)
	if err != nil {
		text = "Tool returned content that this text-only Chat Completions route cannot display."
	}
	if answered[call.CallID] {
		// Repeated notifications are context messages. Chat Completions permits one
		// tool result for each call in the preceding assistant message.
		c.messages = append(c.messages, message{Role: "system", Content: "Asynchronous output from exec call " + call.CallID + ":\n" + text})
		return nil
	}
	c.messages = append(c.messages, message{Role: "tool", ToolID: call.CallID, Content: text})
	return nil
}

func (c *Context) FindCall(id string) (protocol.ToolInvocation, error) {
	calls, _, err := inspect(c.messages)
	if err != nil {
		return protocol.ToolInvocation{}, err
	}
	for _, call := range calls {
		if call.CallID == id {
			return call, nil
		}
	}
	return protocol.ToolInvocation{}, protocol.ErrMissing
}

func (c *Context) PendingCalls() ([]protocol.ToolInvocation, error) {
	calls, answered, err := inspect(c.messages)
	if err != nil {
		return nil, err
	}
	return slices.DeleteFunc(calls, func(call protocol.ToolInvocation) bool { return answered[call.CallID] }), nil
}

func (c *Context) CheckSettled() error {
	calls, err := c.PendingCalls()
	if err != nil {
		return err
	}
	if len(calls) != 0 {
		return &protocol.UnknownEffectError{Reason: "unresolved Chat Completions tool calls"}
	}
	return nil
}

func (c *Context) TurnComplete() bool {
	if len(c.messages) == 0 {
		return false
	}
	last := c.messages[len(c.messages)-1]
	return last.Role == "assistant" && len(last.ToolCalls) == 0
}

func inspect(messages []message) ([]protocol.ToolInvocation, map[string]bool, error) {
	var calls []protocol.ToolInvocation
	answered := map[string]bool{}
	open := 0
	for i, m := range messages {
		switch m.Role {
		case "system", "user", "assistant":
			if open != 0 || m.ToolID != "" || m.Role != "assistant" && (len(m.ToolCalls) != 0 || m.Reasoning != "") {
				return nil, nil, protocol.ErrInvalid
			}
			for _, tool := range m.ToolCalls {
				call := protocol.ToolInvocation{Encoding: protocol.ExecEncodingJSON, CallID: tool.ID, Name: tool.Function.Name, Input: tool.Function.Arguments, SourceRecord: i + 1}
				args := bytes.TrimSpace([]byte(call.Input))
				if !call.HasValidShape() || tool.Type != "function" || !bytes.HasPrefix(args, []byte("{")) || !json.Valid(args) {
					return nil, nil, fmt.Errorf("invalid Chat Completions tool call %q: %w", tool.ID, protocol.ErrInvalid)
				}
				if _, exists := answered[call.CallID]; exists {
					return nil, nil, &protocol.UnknownEffectError{Reason: "duplicate tool call " + call.CallID}
				}
				answered[call.CallID] = false
				calls = append(calls, call)
				open++
			}
		case "tool":
			if done, exists := answered[m.ToolID]; !exists || done || len(m.ToolCalls) != 0 || m.Reasoning != "" {
				return nil, nil, &protocol.UnknownEffectError{Reason: "unmatched Chat Completions tool result " + m.ToolID}
			}
			answered[m.ToolID] = true
			open--
		default:
			return nil, nil, protocol.ErrInvalid
		}
	}
	return calls, answered, nil
}
