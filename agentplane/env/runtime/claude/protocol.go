package claude

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/anthropics/anthropic-sdk-go"
	"github.com/schlunsen/claude-agent-sdk-go/types"
)

type claudeWireFrame struct {
	Type            string                            `json:"type"`
	ParentToolUseID *string                           `json:"parent_tool_use_id"`
	CommandUUID     string                            `json:"command_uuid"`
	State           string                            `json:"state"`
	UUID            string                            `json:"uuid"`
	IsReplay        bool                              `json:"isReplay"`
	UserMessageUUID string                            `json:"user_message_uuid"`
	Subtype         string                            `json:"subtype"`
	IsError         bool                              `json:"is_error"`
	Result          string                            `json:"result"`
	TerminalReason  string                            `json:"terminal_reason"`
	SessionID       string                            `json:"session_id"`
	DurationMS      int64                             `json:"duration_ms"`
	NumTurns        int                               `json:"num_turns"`
	Event           anthropic.MessageStreamEventUnion `json:"event"`
	Message         claudeWireMessage                 `json:"message"`
}

type claudeWireMessage struct {
	ID      string          `json:"id"`
	Content json.RawMessage `json:"content"`
}

// These extensions cover CLI fields absent from the SDK's subtype structs.
type claudeInitializeRequest struct {
	types.SDKControlInitializeRequest
	AppendSystemPrompt string `json:"appendSystemPrompt,omitempty"`
}

type claudeInterruptRequest struct {
	types.SDKControlInterruptRequest
	Reason       string `json:"reason"`
	CancelQueued bool   `json:"cancel_queued"`
}

type claudeSetModelRequest struct {
	Subtype string `json:"subtype"`
	Model   string `json:"model"`
}

type claudeApplyFlagSettingsRequest struct {
	Subtype  string `json:"subtype"`
	Settings struct {
		EffortLevel string `json:"effortLevel"`
	} `json:"settings"`
}

// Override the SDK's map input so decoding never converts native numbers to float64.
type claudePermissionRequest struct {
	types.SDKControlPermissionRequest
	Input json.RawMessage `json:"input"`
}

// The CLI needs explicit null response/error fields, which the SDK body types omit.
type claudeControlResponseBody struct {
	Subtype   string          `json:"subtype"`
	RequestID string          `json:"request_id"`
	Response  json.RawMessage `json:"response"`
	Error     *string         `json:"error"`
}

type claudePermissionAllow struct {
	Behavior     types.PermissionBehavior `json:"behavior"`
	UpdatedInput json.RawMessage          `json:"updatedInput"`
}

func marshalClaudeControlRequest(requestID string, body any) ([]byte, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	return json.Marshal(types.SDKControlRequest{Type: "control_request", RequestID: requestID, Request: raw})
}

func decodeClaudeControlResponse(raw json.RawMessage) (claudeControlResponseBody, error) {
	var packet types.SDKControlResponse
	var body claudeControlResponseBody
	if err := json.Unmarshal(raw, &packet); err != nil {
		return body, err
	}
	if packet.Type != "control_response" {
		return body, fmt.Errorf("expected Claude control_response, got %q", packet.Type)
	}
	if err := json.Unmarshal(packet.Response, &body); err != nil {
		return body, err
	}
	if body.Subtype == "error" && body.Error == nil {
		return body, fmt.Errorf("Claude error response is missing error")
	}
	return body, nil
}

// Claude Code's user tool results accept string content; the API's response block
// union describes assistant blocks instead.
type claudeToolResultBlock struct {
	ToolUseID string          `json:"tool_use_id"`
	Content   json.RawMessage `json:"content"`
	IsError   bool            `json:"is_error"`
}

func (block claudeToolResultBlock) resultText() (string, error) {
	var content string
	if json.Unmarshal(block.Content, &content) == nil {
		return content, nil
	}
	blocks, err := claudeBlocks(block.Content)
	if err != nil {
		return "", err
	}
	var result strings.Builder
	for _, block := range blocks {
		if text, ok := block.AsAny().(anthropic.TextBlock); ok {
			result.WriteString(text.Text)
		}
	}
	return result.String(), nil
}

func claudeToolResults(content json.RawMessage) ([]claudeToolResultBlock, error) {
	var text string
	if json.Unmarshal(content, &text) == nil {
		return nil, nil
	}
	var rawBlocks []json.RawMessage
	if err := json.Unmarshal(content, &rawBlocks); err != nil {
		return nil, err
	}
	var results []claudeToolResultBlock
	for _, raw := range rawBlocks {
		var header struct {
			Type string `json:"type"`
		}
		if err := json.Unmarshal(raw, &header); err != nil {
			return nil, err
		}
		if header.Type != "tool_result" {
			continue
		}
		var result claudeToolResultBlock
		if err := json.Unmarshal(raw, &result); err != nil {
			return nil, err
		}
		results = append(results, result)
	}
	return results, nil
}

func claudeBlocks(content json.RawMessage) ([]anthropic.ContentBlockUnion, error) {
	var text string
	if json.Unmarshal(content, &text) == nil {
		encoded, err := json.Marshal([]anthropic.TextBlock{{Type: "text", Text: text}})
		if err != nil {
			return nil, err
		}
		content = encoded
	}
	var rawBlocks []json.RawMessage
	if err := json.Unmarshal(content, &rawBlocks); err != nil {
		return nil, err
	}
	blocks := make([]anthropic.ContentBlockUnion, 0, len(rawBlocks))
	for _, raw := range rawBlocks {
		block, err := decodeClaudeBlock(raw)
		if err != nil {
			return nil, err
		}
		blocks = append(blocks, block)
	}
	return blocks, nil
}

func decodeClaudeBlock(raw json.RawMessage) (anthropic.ContentBlockUnion, error) {
	var header struct {
		Type string `json:"type"`
	}
	var block anthropic.ContentBlockUnion
	if err := json.Unmarshal(raw, &header); err != nil {
		return block, err
	}
	if header.Type == "" {
		return block, fmt.Errorf("Claude content block is missing type")
	}
	if err := json.Unmarshal(raw, &block); err != nil {
		return block, err
	}
	return block, validateClaudeBlock(block)
}

// The SDK decoder tolerates malformed response fields. Decode known variants
// without their UnmarshalJSON methods before using their typed projections.
func validateClaudeBlock(block anthropic.ContentBlockUnion) error {
	raw := []byte(block.RawJSON())
	switch block.Type {
	case "text":
		type fields anthropic.TextBlock
		var value fields
		if err := json.Unmarshal(raw, &value); err != nil {
			return err
		}
		if !block.JSON.Text.Valid() {
			return fmt.Errorf("Claude text block is missing text")
		}
	case "thinking":
		type fields anthropic.ThinkingBlock
		var value fields
		if err := json.Unmarshal(raw, &value); err != nil {
			return err
		}
		if !block.JSON.Thinking.Valid() {
			return fmt.Errorf("Claude thinking block is missing thinking")
		}
	case "tool_use":
		type fields anthropic.ToolUseBlock
		var value fields
		if err := json.Unmarshal(raw, &value); err != nil {
			return err
		}
		if !block.JSON.ID.Valid() || !block.JSON.Name.Valid() || block.JSON.Input.Raw() == "" {
			return fmt.Errorf("Claude tool_use block is missing id, name, or input")
		}
	}
	return nil
}
