package centralsampler

import (
	"bytes"
	"cmp"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
)

const executionStatePrefix = "Active execution handles: "

// AnthropicContext stores one native Messages API message per record. Messages are kept as raw
// JSON so provider-owned fields, content block boundaries, signatures, and usage survive replay.
type AnthropicContext struct {
	Messages      []json.RawMessage `json:"messages"`
	PendingEffort *string           `json:"pending_effort,omitempty"`
}

func (*AnthropicContext) nativeContext() {}

func (*AnthropicContext) API() APIStyle { return APIStyleAnthropicMessages }

func (c *AnthropicContext) Records() ([]json.RawMessage, error) {
	if c == nil {
		return nil, errors.New("nil Anthropic context")
	}
	result := make([]json.RawMessage, len(c.Messages))
	for i, record := range c.Messages {
		if err := validateAnthropicMessage(record); err != nil {
			return nil, fmt.Errorf("Anthropic record %d: %w", i+1, err)
		}
		result[i] = cloneJSON(record)
	}
	return result, nil
}

// WireMessages removes response-envelope metadata that belongs only in storage while preserving
// the native role, content block sequence, and per-message effort control sent to Anthropic.
func (c *AnthropicContext) WireMessages() ([]json.RawMessage, error) {
	records, err := c.Records()
	if err != nil {
		return nil, err
	}
	if _, err := c.PendingCalls(); err != nil {
		return nil, err
	}
	wire := make([]json.RawMessage, 0, len(records))
	for index, record := range records {
		fields := anthropicMessageFields(record)
		message := map[string]json.RawMessage{
			"role":    cloneJSON(fields["role"]),
			"content": cloneJSON(fields["content"]),
		}
		if outputConfig, present := fields["output_config"]; present {
			message["output_config"] = cloneJSON(outputConfig)
		}
		encoded, err := json.Marshal(message)
		if err != nil {
			return nil, fmt.Errorf("encode Anthropic wire record %d: %w", index+1, err)
		}
		wire = append(wire, encoded)
	}
	return wire, nil
}

func (c *AnthropicContext) AppendRecords(records []json.RawMessage) error {
	if c == nil {
		return errors.New("nil Anthropic context")
	}
	for i, record := range records {
		if err := validateAnthropicMessage(record); err != nil {
			return fmt.Errorf("Anthropic record %d: %w", i+1, err)
		}
	}
	for _, record := range records {
		c.Messages = append(c.Messages, cloneJSON(record))
	}
	return nil
}

func (c *AnthropicContext) AppendUser(parts []ContentPart) error {
	if c == nil {
		return errors.New("nil Anthropic context")
	}
	blocks, err := anthropicParts(parts)
	if err != nil {
		return err
	}
	record, err := marshalAnthropicMessage("user", blocks)
	if err != nil {
		return err
	}
	if err := c.AppendRecords([]json.RawMessage{record}); err != nil {
		return err
	}
	if c.PendingEffort != nil {
		if err := c.appendEffortControl(*c.PendingEffort); err != nil {
			return err
		}
		c.PendingEffort = nil
	}
	return nil
}

// AppendNotice adds server-generated context as a native user message without activating a
// deferred effort restoration. Recovery notices are not new user input.
func (c *AnthropicContext) AppendNotice(parts []ContentPart) error {
	if c == nil {
		return errors.New("nil Anthropic context")
	}
	blocks, err := anthropicParts(parts)
	if err != nil {
		return err
	}
	record, err := marshalAnthropicMessage("user", blocks)
	if err != nil {
		return err
	}
	return c.AppendRecords([]json.RawMessage{record})
}

func (c *AnthropicContext) AppendToolResult(call ToolInvocation, parts []ContentPart) error {
	if c == nil {
		return errors.New("nil Anthropic context")
	}
	found, err := c.FindCall(call.CallID)
	if err != nil {
		return err
	}
	if found.SourceRecord != call.SourceRecord || found.Name != call.Name || found.Input != call.Input || found.Encoding != call.Encoding {
		return fmt.Errorf("tool invocation provenance changed for %q", call.CallID)
	}
	blocks, err := anthropicParts(parts)
	if err != nil {
		return err
	}
	answered, err := c.answeredCalls()
	if err != nil {
		return err
	}
	if answered[call.CallID] {
		encoded, err := json.Marshal(blocks)
		if err != nil {
			return err
		}
		text := "Asynchronous output from exec call " + call.CallID + ":\n" + string(encoded)
		return c.AppendNotice([]ContentPart{{Type: "input_text", Text: text}})
	}
	result := map[string]any{
		"type":        "tool_result",
		"tool_use_id": call.CallID,
		"content":     blocks,
	}
	content, err := json.Marshal([]any{result})
	if err != nil {
		return err
	}
	record, err := json.Marshal(map[string]any{"role": "user", "content": json.RawMessage(content)})
	if err != nil {
		return err
	}
	return c.AppendRecords([]json.RawMessage{record})
}

func (c *AnthropicContext) ChangeEffort(effort string) error {
	if c == nil {
		return errors.New("nil Anthropic context")
	}
	if strings.TrimSpace(effort) == "" {
		return errors.New("Anthropic effort must be non-empty")
	}
	if err := c.appendEffortControl(effort); err != nil {
		return err
	}
	// An explicit effort change supersedes any compaction restoration not yet activated.
	c.PendingEffort = nil
	return nil
}

func (c *AnthropicContext) appendEffortControl(effort string) error {
	if strings.TrimSpace(effort) == "" {
		return errors.New("Anthropic effort must be non-empty")
	}
	record, err := json.Marshal(map[string]any{
		"role":          "system",
		"content":       []any{},
		"output_config": map[string]any{"effort": effort},
	})
	if err != nil {
		return err
	}
	return c.AppendRecords([]json.RawMessage{record})
}

func (c *AnthropicContext) AppendExecutionState(state *ExecutionState) error {
	if c == nil {
		return errors.New("nil Anthropic context")
	}
	if state == nil {
		return nil
	}
	snapshot := anthropicExecutionState{Cells: []anthropicActiveCell{}, Processes: []anthropicActiveProcess{}}
	for _, cell := range state.Cells {
		snapshot.Cells = append(snapshot.Cells, anthropicActiveCell{CellID: cell.ID, Status: cell.Status})
	}
	for _, process := range state.Processes {
		if process.ModelSessionID == nil {
			continue
		}
		snapshot.Processes = append(snapshot.Processes, anthropicActiveProcess{
			SessionID: *process.ModelSessionID, Status: process.Status, TTY: process.TTY, ExitCode: process.ExitCode,
		})
	}
	slices.SortFunc(snapshot.Cells, func(a, b anthropicActiveCell) int { return strings.Compare(a.CellID, b.CellID) })
	slices.SortFunc(snapshot.Processes, func(a, b anthropicActiveProcess) int { return cmp.Compare(a.SessionID, b.SessionID) })
	wire, err := json.Marshal(snapshot)
	if err != nil {
		return err
	}
	previous, exists, err := c.lastExecutionState()
	if err != nil {
		return err
	}
	if !exists && len(snapshot.Cells) == 0 && len(snapshot.Processes) == 0 {
		return nil
	}
	if exists && bytes.Equal(previous, wire) {
		return nil
	}
	text := executionStatePrefix + string(wire)
	return c.AppendNotice([]ContentPart{{Type: "input_text", Text: text}})
}

type anthropicExecutionState struct {
	Cells     []anthropicActiveCell    `json:"cells"`
	Processes []anthropicActiveProcess `json:"processes"`
}

type anthropicActiveCell struct {
	CellID string `json:"cell_id"`
	Status string `json:"status"`
}

type anthropicActiveProcess struct {
	SessionID int    `json:"session_id"`
	Status    string `json:"status"`
	TTY       bool   `json:"tty"`
	ExitCode  *int   `json:"exit_code"`
}

func (c *AnthropicContext) lastExecutionState() (json.RawMessage, bool, error) {
	for i := len(c.Messages) - 1; i >= 0; i-- {
		fields := anthropicMessageFields(c.Messages[i])
		if rawString(fields, "role") != "user" {
			continue
		}
		content, err := anthropicContentBlocks(fields["content"])
		if err != nil {
			return nil, false, err
		}
		if len(content) != 1 {
			continue
		}
		var textBlock struct {
			Type string `json:"type"`
			Text string `json:"text"`
		}
		if err := json.Unmarshal(content[0], &textBlock); err != nil || textBlock.Type != "text" || !strings.HasPrefix(textBlock.Text, executionStatePrefix) {
			continue
		}
		return json.RawMessage(strings.TrimPrefix(textBlock.Text, executionStatePrefix)), true, nil
	}
	return nil, false, nil
}

func anthropicContentBlocks(raw json.RawMessage) ([]json.RawMessage, error) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null")) {
		return nil, nil
	}
	if trimmed[0] == '"' {
		return nil, nil
	}
	var blocks []json.RawMessage
	if err := json.Unmarshal(trimmed, &blocks); err != nil {
		return nil, err
	}
	return blocks, nil
}

func (c *AnthropicContext) FindCall(callID string) (ToolInvocation, error) {
	if c == nil {
		return ToolInvocation{}, errors.New("nil Anthropic context")
	}
	calls, err := c.toolCalls()
	if err != nil {
		return ToolInvocation{}, err
	}
	for _, call := range calls {
		if call.CallID == callID {
			return call, nil
		}
	}
	return ToolInvocation{}, ErrMissing
}

func (c *AnthropicContext) PendingCalls() ([]ToolInvocation, error) {
	if c == nil {
		return nil, errors.New("nil Anthropic context")
	}
	calls, err := c.toolCalls()
	if err != nil {
		return nil, err
	}
	answered, err := c.answeredCalls()
	if err != nil {
		return nil, err
	}
	pending := make([]ToolInvocation, 0, len(calls))
	for _, call := range calls {
		if !answered[call.CallID] {
			pending = append(pending, call)
		}
	}
	return pending, nil
}

func (c *AnthropicContext) toolCalls() ([]ToolInvocation, error) {
	calls := []ToolInvocation{}
	seen := map[string]bool{}
	for recordIndex, record := range c.Messages {
		fields := anthropicMessageFields(record)
		if len(fields) == 0 {
			return nil, fmt.Errorf("decode Anthropic record %d", recordIndex+1)
		}
		if rawString(fields, "role") != "assistant" {
			continue
		}
		content, err := anthropicContentBlocks(fields["content"])
		if err != nil {
			return nil, fmt.Errorf("decode Anthropic record %d content: %w", recordIndex+1, err)
		}
		for _, blockRaw := range content {
			var block map[string]json.RawMessage
			if err := json.Unmarshal(blockRaw, &block); err != nil {
				return nil, fmt.Errorf("decode Anthropic content block: %w", err)
			}
			if rawString(block, "type") != "tool_use" {
				continue
			}
			callID, name := rawString(block, "id"), rawString(block, "name")
			input := bytes.TrimSpace(block["input"])
			var object map[string]json.RawMessage
			if callID == "" || name == "" || len(input) == 0 || json.Unmarshal(input, &object) != nil || object == nil {
				return nil, errors.New("invalid Anthropic tool_use block")
			}
			if seen[callID] {
				return nil, fmt.Errorf("duplicate Anthropic tool call ID %q", callID)
			}
			seen[callID] = true
			calls = append(calls, ToolInvocation{
				Encoding: ExecEncodingJSON, CallID: callID, Name: name, Input: string(input), SourceRecord: recordIndex + 1,
			})
		}
	}
	return calls, nil
}

func (c *AnthropicContext) answeredCalls() (map[string]bool, error) {
	known := map[string]bool{}
	calls, err := c.toolCalls()
	if err != nil {
		return nil, err
	}
	for _, call := range calls {
		known[call.CallID] = true
	}
	answered := map[string]bool{}
	for recordIndex, record := range c.Messages {
		fields := anthropicMessageFields(record)
		if len(fields) == 0 {
			return nil, fmt.Errorf("decode Anthropic record %d", recordIndex+1)
		}
		if rawString(fields, "role") != "user" {
			continue
		}
		content, err := anthropicContentBlocks(fields["content"])
		if err != nil {
			return nil, fmt.Errorf("decode Anthropic record %d content: %w", recordIndex+1, err)
		}
		for _, blockRaw := range content {
			var block map[string]json.RawMessage
			if err := json.Unmarshal(blockRaw, &block); err != nil {
				return nil, fmt.Errorf("decode Anthropic content block: %w", err)
			}
			if rawString(block, "type") != "tool_result" {
				continue
			}
			var callID string
			if err := json.Unmarshal(block["tool_use_id"], &callID); err != nil || callID == "" || !known[callID] || answered[callID] {
				return nil, fmt.Errorf("invalid or duplicate Anthropic tool_result at record %d", recordIndex+1)
			}
			answered[callID] = true
		}
	}
	return answered, nil
}

func (c *AnthropicContext) CheckSettled() error {
	calls, err := c.PendingCalls()
	if err != nil {
		return err
	}
	if len(calls) != 0 {
		ids := make([]string, len(calls))
		for index, call := range calls {
			ids[index] = call.CallID
		}
		slices.Sort(ids)
		return &UnknownEffectError{Reason: fmt.Sprintf("unresolved Anthropic tool calls %v", ids)}
	}
	return nil
}

func (c *AnthropicContext) TurnComplete() bool {
	if c == nil || len(c.Messages) == 0 {
		return false
	}
	var message struct {
		Role       string `json:"role"`
		StopReason string `json:"stop_reason"`
	}
	if json.Unmarshal(c.Messages[len(c.Messages)-1], &message) != nil {
		return false
	}
	return message.Role == "assistant" && message.StopReason == "end_turn"
}

func (c *AnthropicContext) MarshalJSON() ([]byte, error) {
	if c == nil {
		return []byte("null"), nil
	}
	if _, err := c.Records(); err != nil {
		return nil, err
	}
	if c.PendingEffort != nil && strings.TrimSpace(*c.PendingEffort) == "" {
		return nil, errors.New("Anthropic pending effort must be non-empty")
	}
	messages := c.Messages
	if messages == nil {
		messages = []json.RawMessage{}
	}
	return json.Marshal(struct {
		Messages      []json.RawMessage `json:"messages"`
		PendingEffort *string           `json:"pending_effort,omitempty"`
	}{Messages: messages, PendingEffort: c.PendingEffort})
}

func (c *AnthropicContext) UnmarshalJSON(data []byte) error {
	if c == nil {
		return errors.New("nil Anthropic context")
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	if fields == nil {
		return errors.New("Anthropic context must be an object")
	}
	for key := range fields {
		if key != "messages" && key != "pending_effort" {
			return fmt.Errorf("unknown Anthropic context field %q", key)
		}
	}
	var messages []json.RawMessage
	if encoded, ok := fields["messages"]; ok {
		if err := json.Unmarshal(encoded, &messages); err != nil {
			return fmt.Errorf("Anthropic context messages: %w", err)
		}
	} else {
		return errors.New("Anthropic context has no messages field")
	}
	var pending *string
	if encoded, ok := fields["pending_effort"]; ok {
		var effort string
		if err := json.Unmarshal(encoded, &effort); err != nil {
			return fmt.Errorf("Anthropic pending effort: %w", err)
		}
		if strings.TrimSpace(effort) == "" {
			return errors.New("Anthropic pending effort must be non-empty")
		}
		pending = &effort
	}
	for i, record := range messages {
		if err := validateAnthropicMessage(record); err != nil {
			return fmt.Errorf("Anthropic record %d: %w", i+1, err)
		}
	}
	c.Messages, c.PendingEffort = messages, pending
	return nil
}

func validateAnthropicMessage(raw json.RawMessage) error {
	var fields map[string]json.RawMessage
	if len(raw) == 0 || json.Unmarshal(raw, &fields) != nil || fields == nil {
		return errors.New("message must be an object")
	}
	var role string
	if err := json.Unmarshal(fields["role"], &role); err != nil {
		return errors.New("message role is required")
	}
	switch role {
	case "user", "assistant":
		if len(fields["content"]) == 0 || bytes.Equal(bytes.TrimSpace(fields["content"]), []byte("null")) {
			return errors.New("message content is required")
		}
		blocks, err := anthropicContentBlocks(fields["content"])
		if err != nil {
			return fmt.Errorf("invalid content blocks: %w", err)
		}
		for index, blockRaw := range blocks {
			var block map[string]json.RawMessage
			if err := json.Unmarshal(blockRaw, &block); err != nil || block == nil {
				return fmt.Errorf("content block %d must be an object", index+1)
			}
			kind := rawString(block, "type")
			switch kind {
			case "text":
				if _, ok := block["text"]; !ok {
					return fmt.Errorf("content block %d text block has no text", index+1)
				}
			case "image":
				if role != "user" {
					return fmt.Errorf("content block %d image is only valid on a user message", index+1)
				}
			case "tool_use":
				if role != "assistant" || rawString(block, "id") == "" || rawString(block, "name") == "" {
					return fmt.Errorf("content block %d has invalid tool_use identity", index+1)
				}
				var input map[string]json.RawMessage
				if err := json.Unmarshal(block["input"], &input); err != nil || input == nil {
					return fmt.Errorf("content block %d tool_use input must be an object", index+1)
				}
			case "tool_result":
				if role != "user" || rawString(block, "tool_use_id") == "" || len(block["content"]) == 0 {
					return fmt.Errorf("content block %d has invalid tool_result", index+1)
				}
			case "thinking":
				if role != "assistant" || rawString(block, "signature") == "" {
					return fmt.Errorf("content block %d has invalid or unsigned thinking", index+1)
				}
			case "redacted_thinking":
				if role != "assistant" {
					return fmt.Errorf("content block %d redacted thinking is only valid on an assistant message", index+1)
				}
			case "compaction":
				if role != "assistant" || rawString(block, "content") == "" || rawString(block, "signature") == "" {
					return fmt.Errorf("content block %d has invalid or unsigned compaction", index+1)
				}
			default:
				return fmt.Errorf("content block %d has unsupported Anthropic type %q", index+1, kind)
			}
		}
	case "system":
		var content []json.RawMessage
		if err := json.Unmarshal(fields["content"], &content); err != nil || len(content) != 0 {
			return errors.New("system effort control must have empty content")
		}
		var outputConfig map[string]json.RawMessage
		if err := json.Unmarshal(fields["output_config"], &outputConfig); err != nil || rawString(outputConfig, "effort") == "" {
			return errors.New("system effort control requires output_config.effort")
		}
	default:
		return fmt.Errorf("unsupported message role %q", role)
	}
	return nil
}

func marshalAnthropicMessage(role string, content any) (json.RawMessage, error) {
	encoded, err := json.Marshal(map[string]any{"role": role, "content": content})
	return encoded, err
}
