package centralsampler

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strings"
)

type ResponsesItemType string

const (
	ResponsesItemAdditionalTools   ResponsesItemType = "additional_tools"
	ResponsesItemMessage           ResponsesItemType = "message"
	ResponsesItemReasoning         ResponsesItemType = "reasoning"
	ResponsesItemCustomToolCall    ResponsesItemType = "custom_tool_call"
	ResponsesItemCustomToolOutput  ResponsesItemType = "custom_tool_call_output"
	ResponsesItemFunctionCall      ResponsesItemType = "function_call"
	ResponsesItemFunctionOutput    ResponsesItemType = "function_call_output"
	ResponsesItemCompaction        ResponsesItemType = "compaction"
	ResponsesItemCompactionTrigger ResponsesItemType = "compaction_trigger"
)

type ResponsesEventType string

const (
	ResponsesEventOutputItemAdded ResponsesEventType = "response.output_item.added"
	ResponsesEventOutputItemDone  ResponsesEventType = "response.output_item.done"
	ResponsesEventCompleted       ResponsesEventType = "response.completed"
	ResponsesEventFailed          ResponsesEventType = "response.failed"
	ResponsesEventIncomplete      ResponsesEventType = "response.incomplete"
	ResponsesEventError           ResponsesEventType = "error"
)

// ResponsesItem is the Codex Responses item envelope. Raw retains provider-owned fields and
// numeric precision when an item is read from the provider or the durable conversation.
type ResponsesItem struct {
	Type             ResponsesItemType         `json:"type"`
	ID               string                    `json:"id,omitempty"`
	Role             string                    `json:"role,omitempty"`
	CallID           string                    `json:"call_id,omitempty"`
	Name             string                    `json:"name,omitempty"`
	Input            string                    `json:"input,omitempty"`
	Arguments        string                    `json:"arguments,omitempty"`
	Content          json.RawMessage           `json:"content,omitempty"`
	Summary          json.RawMessage           `json:"summary,omitempty"`
	Output           []ResponsesContentPart    `json:"output,omitempty"`
	EncryptedContent string                    `json:"encrypted_content,omitempty"`
	Tools            []ResponsesAdditionalTool `json:"tools,omitempty"`
	Raw              json.RawMessage           `json:"-"`
}

func (item *ResponsesItem) UnmarshalJSON(data []byte) error {
	if len(data) == 0 || data[0] != '{' {
		return errors.New("Responses item must be an object")
	}
	var wire struct {
		Type             ResponsesItemType         `json:"type"`
		ID               string                    `json:"id"`
		Role             string                    `json:"role"`
		CallID           string                    `json:"call_id"`
		Name             string                    `json:"name"`
		Input            string                    `json:"input"`
		Arguments        string                    `json:"arguments"`
		Content          json.RawMessage           `json:"content"`
		Summary          json.RawMessage           `json:"summary"`
		Output           []ResponsesContentPart    `json:"output"`
		EncryptedContent string                    `json:"encrypted_content"`
		Tools            []ResponsesAdditionalTool `json:"tools"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return err
	}
	*item = ResponsesItem{
		Type: wire.Type, ID: wire.ID, Role: wire.Role, CallID: wire.CallID, Name: wire.Name,
		Input: wire.Input, Arguments: wire.Arguments, Content: cloneJSON(wire.Content), Summary: cloneJSON(wire.Summary),
		Output: wire.Output, EncryptedContent: wire.EncryptedContent, Tools: wire.Tools, Raw: cloneJSON(data),
	}
	return nil
}

func (item ResponsesItem) MarshalJSON() ([]byte, error) {
	if len(item.Raw) != 0 {
		return cloneJSON(item.Raw), nil
	}
	type wire ResponsesItem
	return json.Marshal(wire(item))
}

// ToolCall returns a model tool call. Input holds a custom call's raw input or a function
// call's JSON arguments.
func (item ResponsesItem) ToolCall() (ResponsesToolCall, bool) {
	switch item.Type {
	case ResponsesItemCustomToolCall:
		return ResponsesToolCall{Type: item.Type, CallID: item.CallID, Name: item.Name, Input: item.Input}, true
	case ResponsesItemFunctionCall:
		return ResponsesToolCall{Type: item.Type, CallID: item.CallID, Name: item.Name, Input: item.Arguments}, true
	default:
		return ResponsesToolCall{}, false
	}
}

// HasValidToolCallShape checks the fields required to answer a Responses tool call. Tool names
// are intentionally not part of this check; an unknown name still has a valid call/output pair.
func (item ResponsesItem) HasValidToolCallShape() bool {
	call, ok := item.ToolCall()
	if !ok || !call.HasValidShape() {
		return false
	}
	if len(item.Raw) == 0 {
		return true
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(item.Raw, &fields); err != nil || fields == nil {
		return false
	}
	inputField := "input"
	if item.Type == ResponsesItemFunctionCall {
		inputField = "arguments"
	}
	for _, field := range []string{"call_id", "name", inputField} {
		encoded, present := fields[field]
		if !present {
			return false
		}
		var value *string
		if err := json.Unmarshal(encoded, &value); err != nil || value == nil {
			return false
		}
	}
	return true
}

// IsToolOutput reports whether the item answers a model tool call.
func (item ResponsesItem) IsToolOutput() bool {
	return item.Type == ResponsesItemCustomToolOutput || item.Type == ResponsesItemFunctionOutput
}

func (item ResponsesItem) TextParts() ([]ResponsesContentPart, error) {
	raw := item.Content
	if item.Type == ResponsesItemReasoning {
		raw = item.Summary
	}
	if len(raw) == 0 || !bytes.HasPrefix(bytes.TrimSpace(raw), []byte("[")) {
		return nil, ErrInvalid
	}
	var parts []ResponsesContentPart
	if err := json.Unmarshal(raw, &parts); err != nil {
		return nil, err
	}
	return parts, nil
}

type ResponsesToolCall struct {
	Type   ResponsesItemType `json:"type"`
	CallID string            `json:"call_id"`
	Name   string            `json:"name"`
	Input  string            `json:"input"`
}

func (call ResponsesToolCall) HasValidShape() bool {
	return (call.Type == ResponsesItemCustomToolCall || call.Type == ResponsesItemFunctionCall) &&
		strings.TrimSpace(call.CallID) != "" && strings.TrimSpace(call.Name) != ""
}

func (call ResponsesToolCall) IsConfiguredToolName(config ModelConfig) bool {
	if config.ExecEncoding == ExecEncodingJSON {
		return call.Name == "exec"
	}
	return call.Name == "exec" || call.Name == "wait"
}

// IsModelTool checks the exact tool surface declared for this model route.
func (call ResponsesToolCall) IsModelTool(config ModelConfig) bool {
	if config.ExecEncoding == ExecEncodingJSON {
		return call.Name == "exec" && call.Type == ResponsesItemFunctionCall
	}
	return call.Name == "exec" && call.Type == ResponsesItemCustomToolCall ||
		call.Name == "wait" && call.Type == ResponsesItemFunctionCall
}

// OutputItem answers the call with the output item type its call type requires.
func (call ResponsesToolCall) OutputItem(content []ResponsesContentPart) ResponsesItem {
	outputType := ResponsesItemCustomToolOutput
	if call.Type == ResponsesItemFunctionCall {
		outputType = ResponsesItemFunctionOutput
	}
	return ResponsesItem{Type: outputType, CallID: call.CallID, Output: content}
}

func unsupportedToolCallMessage(call ResponsesToolCall) string {
	if call.Type == ResponsesItemCustomToolCall {
		return "unsupported custom tool call: " + call.Name
	}
	return "unsupported call: " + call.Name
}

type ResponsesContentPart struct {
	Type string          `json:"type"`
	Text string          `json:"text"`
	Raw  json.RawMessage `json:"-"`
}

func (part *ResponsesContentPart) UnmarshalJSON(data []byte) error {
	if len(data) == 0 || data[0] != '{' {
		return errors.New("Responses content part must be an object")
	}
	var wire struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return err
	}
	*part = ResponsesContentPart{Type: wire.Type, Text: wire.Text, Raw: cloneJSON(data)}
	return nil
}

func (part ResponsesContentPart) MarshalJSON() ([]byte, error) {
	if len(part.Raw) != 0 {
		return cloneJSON(part.Raw), nil
	}
	type wire ResponsesContentPart
	return json.Marshal(wire(part))
}

// ResponsesAdditionalTool is a `custom` tool (Format) or a `function` tool (Strict, Parameters).
type ResponsesAdditionalTool struct {
	Type        string               `json:"type"`
	Name        string               `json:"name"`
	Description string               `json:"description"`
	Strict      *bool                `json:"strict,omitempty"`
	Parameters  json.RawMessage      `json:"parameters,omitempty"`
	Format      *ResponsesToolFormat `json:"format,omitempty"`
}

type ResponsesToolFormat struct {
	Type       string `json:"type"`
	Syntax     string `json:"syntax,omitempty"`
	Definition string `json:"definition,omitempty"`
}

type ResponsesCodeToolDefinition struct {
	Name         string               `json:"name"`
	ToolName     ResponsesToolName    `json:"tool_name"`
	Description  string               `json:"description"`
	Kind         string               `json:"kind"`
	InputSchema  *ResponsesJSONSchema `json:"input_schema"`
	OutputSchema *ResponsesJSONSchema `json:"output_schema"`
}

type ResponsesToolName struct {
	Name      string  `json:"name"`
	Namespace *string `json:"namespace"`
}

// Codex rust-v0.162.1 tools/src/json_schema/types.rs JsonSchema: absent keywords are omitted.
type ResponsesJSONSchema struct {
	Type                 string                         `json:"type,omitempty"`
	Description          string                         `json:"description,omitempty"`
	Enum                 []string                       `json:"enum,omitempty"`
	Items                *ResponsesJSONSchema           `json:"items,omitempty"`
	Properties           map[string]ResponsesJSONSchema `json:"properties,omitempty"`
	Required             []string                       `json:"required,omitempty"`
	AdditionalProperties *bool                          `json:"additionalProperties,omitempty"`
}

// ResponsesInput is a wire input item: a retained native item or a public text tool result.
type ResponsesInput interface {
	responsesInput()
}

func (ResponsesItem) responsesInput() {}

type ResponsesTextToolOutput struct {
	Type   ResponsesItemType `json:"type"`
	ID     string            `json:"id,omitempty"`
	CallID string            `json:"call_id"`
	Output string            `json:"output"`
}

func (ResponsesTextToolOutput) responsesInput() {}

type ResponsesRequest struct {
	Model          string                    `json:"model"`
	Instructions   string                    `json:"instructions"`
	Input          []ResponsesInput          `json:"input"`
	Stream         bool                      `json:"stream"`
	Store          bool                      `json:"store"`
	Include        []string                  `json:"include,omitempty"`
	PromptCacheKey string                    `json:"prompt_cache_key"`
	ClientMetadata *ResponsesClientMetadata  `json:"client_metadata,omitempty"`
	Reasoning      *ResponsesReasoningConfig `json:"reasoning,omitempty"`
	Tools          []ResponsesAdditionalTool `json:"tools,omitempty"`
	// Codex rust-v0.162.1 core/src/client.rs build_responses_request: Responses Lite disables
	// parallel tool calls.
	ParallelToolCalls bool `json:"parallel_tool_calls"`
}

type ResponsesClientMetadata struct {
	SessionID string `json:"session_id"`
	ThreadID  string `json:"thread_id"`
	TurnID    string `json:"turn_id"`
}

type ResponsesReasoningConfig struct {
	Effort  string `json:"effort,omitempty"`
	Summary string `json:"summary,omitempty"`
	// Codex rust-v0.162.1 core/src/client.rs build_reasoning: the Responses Lite header
	// (provider.go) requires `all_turns`; the provider rejects any other value.
	Context string `json:"context,omitempty"`
}

// Codex rust-v0.162.1 core/src/client.rs build_reasoning and session/step_settings.rs: the summary is
// the configured model_reasoning_summary, else the model's default_reasoning_summary. Agentplane's
// model routes (e.g. chatgpt-sub/oai-responses/gpt-6-luna) are not slugs in Codex's bundled
// catalog, so Codex uses models-manager/src/model_info.rs model_info_from_slug fallback metadata:
// supports_reasoning_summary_parameter with default "auto". Wrapped Codex therefore sends "auto".
const codexReasoningSummary = "auto"

type ResponsesEvent struct {
	Type        ResponsesEventType `json:"type"`
	ItemID      string             `json:"item_id,omitempty"`
	Delta       *string            `json:"delta,omitempty"`
	Item        *ResponsesItem     `json:"item,omitempty"`
	Response    *ResponsesTerminal `json:"-"`
	ResponseRaw json.RawMessage    `json:"-"`
	Raw         json.RawMessage    `json:"-"`
}

func (event *ResponsesEvent) UnmarshalJSON(data []byte) error {
	if len(data) == 0 || data[0] != '{' {
		return errors.New("Responses event must be an object")
	}
	var wire struct {
		Type     ResponsesEventType `json:"type"`
		ItemID   string             `json:"item_id"`
		Delta    *string            `json:"delta"`
		Item     *ResponsesItem     `json:"item"`
		Response json.RawMessage    `json:"response"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return err
	}
	if wire.Type == "" {
		return errors.New("Responses event has no type")
	}
	var response *ResponsesTerminal
	if len(wire.Response) > 0 && bytes.HasPrefix(bytes.TrimSpace(wire.Response), []byte("{")) {
		response = new(ResponsesTerminal)
		if err := json.Unmarshal(wire.Response, response); err != nil {
			return err
		}
	}
	*event = ResponsesEvent{
		Type: wire.Type, ItemID: wire.ItemID, Delta: wire.Delta, Item: wire.Item,
		Response: response, ResponseRaw: cloneJSON(wire.Response), Raw: cloneJSON(data),
	}
	return nil
}

func (event ResponsesEvent) MarshalJSON() ([]byte, error) {
	if len(event.Raw) != 0 {
		return cloneJSON(event.Raw), nil
	}
	type wire ResponsesEvent
	return json.Marshal(wire(event))
}

type ResponsesTerminal struct {
	Status json.RawMessage `json:"status,omitempty"`
	Usage  json.RawMessage `json:"usage,omitempty"`
	Raw    json.RawMessage `json:"-"`
}

func (terminal *ResponsesTerminal) UnmarshalJSON(data []byte) error {
	if len(data) == 0 || data[0] != '{' {
		return errors.New("Responses terminal response must be an object")
	}
	var wire struct {
		Status json.RawMessage `json:"status"`
		Usage  json.RawMessage `json:"usage"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return err
	}
	*terminal = ResponsesTerminal{
		Status: cloneJSON(wire.Status),
		Usage:  cloneJSON(wire.Usage), Raw: cloneJSON(data),
	}
	return nil
}

func (terminal ResponsesTerminal) StatusValue() (string, bool) {
	if len(terminal.Status) == 0 {
		return "", false
	}
	var status string
	if err := json.Unmarshal(terminal.Status, &status); err != nil {
		return "", false
	}
	return status, true
}

func (terminal ResponsesTerminal) MarshalJSON() ([]byte, error) {
	if len(terminal.Raw) != 0 {
		return cloneJSON(terminal.Raw), nil
	}
	type wire ResponsesTerminal
	return json.Marshal(wire(terminal))
}

func (terminal ResponsesTerminal) TotalTokens() *int {
	if len(terminal.Usage) == 0 || !bytes.HasPrefix(bytes.TrimSpace(terminal.Usage), []byte("{")) {
		return nil
	}
	var usage struct {
		TotalTokens json.RawMessage `json:"total_tokens"`
	}
	if err := json.Unmarshal(terminal.Usage, &usage); err != nil || len(usage.TotalTokens) == 0 {
		return nil
	}
	var tokens int64
	if err := json.Unmarshal(usage.TotalTokens, &tokens); err != nil {
		return nil
	}
	if int64(int(tokens)) != tokens {
		return nil
	}
	value := int(tokens)
	return &value
}

func cloneJSON(value []byte) json.RawMessage {
	if value == nil {
		return nil
	}
	return append(json.RawMessage(nil), value...)
}

func decodeJSONValue(data []byte) (any, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		if err == nil {
			return nil, errors.New("JSON contains trailing data")
		}
		return nil, err
	}
	return value, nil
}

func equalResponsesItems(left, right []ResponsesItem) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		leftJSON, leftErr := json.Marshal(left[index])
		rightJSON, rightErr := json.Marshal(right[index])
		if leftErr != nil || rightErr != nil {
			return false
		}
		leftValue, leftErr := decodeJSONValue(leftJSON)
		rightValue, rightErr := decodeJSONValue(rightJSON)
		if leftErr != nil || rightErr != nil {
			return false
		}
		leftCanonical, leftErr := json.Marshal(leftValue)
		rightCanonical, rightErr := json.Marshal(rightValue)
		if leftErr != nil || rightErr != nil || !bytes.Equal(leftCanonical, rightCanonical) {
			return false
		}
	}
	return true
}

func responsesText(item ResponsesItem) (string, error) {
	parts, err := item.TextParts()
	if err != nil {
		return "", err
	}
	// Reasoning summary parts are separate paragraphs; native Codex Sessions join them with a newline
	// (agentplane/runner/codex/adapter.go). Message content parts are one continuous text.
	separator := ""
	if item.Type == ResponsesItemReasoning {
		separator = "\n"
	}
	texts := make([]string, len(parts))
	for index, part := range parts {
		texts[index] = part.Text
	}
	return strings.Join(texts, separator), nil
}
