package responses

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
)

type ItemType string

const (
	ItemAdditionalTools   ItemType = "additional_tools"
	ItemMessage           ItemType = "message"
	ItemReasoning         ItemType = "reasoning"
	ItemCustomToolCall    ItemType = "custom_tool_call"
	ItemCustomToolOutput  ItemType = "custom_tool_call_output"
	ItemFunctionCall      ItemType = "function_call"
	ItemFunctionOutput    ItemType = "function_call_output"
	ItemCompaction        ItemType = "compaction"
	ItemCompactionTrigger ItemType = "compaction_trigger"
)

type EventType string

const (
	EventOutputItemAdded EventType = "response.output_item.added"
	EventOutputItemDone  EventType = "response.output_item.done"
	EventCompleted       EventType = "response.completed"
	EventFailed          EventType = "response.failed"
	EventIncomplete      EventType = "response.incomplete"
	EventError           EventType = "error"
)

// Item is a native Responses record. Raw retains provider-owned fields and numeric precision
// when an item is read from the provider or durable conversation.
type Item struct {
	Type             ItemType               `json:"type"`
	ID               string                 `json:"id,omitempty"`
	Role             string                 `json:"role,omitempty"`
	CallID           string                 `json:"call_id,omitempty"`
	Name             string                 `json:"name,omitempty"`
	Input            string                 `json:"input,omitempty"`
	Arguments        string                 `json:"arguments,omitempty"`
	Content          json.RawMessage        `json:"content,omitempty"`
	Summary          json.RawMessage        `json:"summary,omitempty"`
	Output           []protocol.ContentPart `json:"output,omitempty"`
	EncryptedContent string                 `json:"encrypted_content,omitempty"`
	Tools            []AdditionalTool       `json:"tools,omitempty"`
	Raw              json.RawMessage        `json:"-"`
}

func (item *Item) UnmarshalJSON(data []byte) error {
	if len(data) == 0 || data[0] != '{' {
		return errors.New("Responses item must be an object")
	}
	var wire struct {
		Type             ItemType               `json:"type"`
		ID               string                 `json:"id"`
		Role             string                 `json:"role"`
		CallID           string                 `json:"call_id"`
		Name             string                 `json:"name"`
		Input            string                 `json:"input"`
		Arguments        string                 `json:"arguments"`
		Content          json.RawMessage        `json:"content"`
		Summary          json.RawMessage        `json:"summary"`
		Output           []protocol.ContentPart `json:"output"`
		EncryptedContent string                 `json:"encrypted_content"`
		Tools            []AdditionalTool       `json:"tools"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return err
	}
	*item = Item{
		Type: wire.Type, ID: wire.ID, Role: wire.Role, CallID: wire.CallID, Name: wire.Name,
		Input: wire.Input, Arguments: wire.Arguments, Content: protocol.CloneJSON(wire.Content), Summary: protocol.CloneJSON(wire.Summary),
		Output: wire.Output, EncryptedContent: wire.EncryptedContent, Tools: wire.Tools, Raw: protocol.CloneJSON(data),
	}
	return nil
}

func (item Item) MarshalJSON() ([]byte, error) {
	if len(item.Raw) != 0 {
		return protocol.CloneJSON(item.Raw), nil
	}
	type wire Item
	return json.Marshal(wire(item))
}

// ToolCall returns a model tool call. Input holds a custom call's raw input or a function
// call's JSON arguments.
func (item Item) ToolCall() (protocol.ToolInvocation, bool) {
	switch item.Type {
	case ItemCustomToolCall:
		return protocol.ToolInvocation{Encoding: protocol.ExecEncodingFreeform, CallID: item.CallID, Name: item.Name, Input: item.Input}, true
	case ItemFunctionCall:
		return protocol.ToolInvocation{Encoding: protocol.ExecEncodingJSON, CallID: item.CallID, Name: item.Name, Input: item.Arguments}, true
	default:
		return protocol.ToolInvocation{}, false
	}
}

// HasValidToolCallShape checks the fields required to answer a Responses tool call. Tool names
// are intentionally not part of this check; an unknown name still has a valid call/output pair.
func (item Item) HasValidToolCallShape() bool {
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
	if call.Encoding == protocol.ExecEncodingJSON {
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
func (item Item) IsToolOutput() bool {
	return item.Type == ItemCustomToolOutput || item.Type == ItemFunctionOutput
}

func (item Item) TextParts() ([]protocol.ContentPart, error) {
	raw := item.Content
	if item.Type == ItemReasoning {
		raw = item.Summary
	}
	if len(raw) == 0 || !bytes.HasPrefix(bytes.TrimSpace(raw), []byte("[")) {
		return nil, protocol.ErrInvalid
	}
	var parts []protocol.ContentPart
	if err := json.Unmarshal(raw, &parts); err != nil {
		return nil, err
	}
	return parts, nil
}

// AdditionalTool is a `custom` tool (Format) or a `function` tool (Strict, Parameters).
type AdditionalTool struct {
	Type        string          `json:"type"`
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Strict      *bool           `json:"strict,omitempty"`
	Parameters  json.RawMessage `json:"parameters,omitempty"`
	Format      *ToolFormat     `json:"format,omitempty"`
}

type ToolFormat struct {
	Type       string `json:"type"`
	Syntax     string `json:"syntax,omitempty"`
	Definition string `json:"definition,omitempty"`
}

// RequestInput is a retained native item or a public text tool result.
type RequestInput interface {
	requestInput()
}

func (Item) requestInput() {}

type TextToolOutput struct {
	Type   ItemType `json:"type"`
	ID     string   `json:"id,omitempty"`
	CallID string   `json:"call_id"`
	Output string   `json:"output"`
}

func (TextToolOutput) requestInput() {}

type Request struct {
	Model             string           `json:"model"`
	Instructions      string           `json:"instructions"`
	Input             []RequestInput   `json:"input"`
	Stream            bool             `json:"stream"`
	Store             bool             `json:"store"`
	Include           []string         `json:"include,omitempty"`
	PromptCacheKey    string           `json:"prompt_cache_key"`
	ClientMetadata    *ClientMetadata  `json:"client_metadata,omitempty"`
	Reasoning         *ReasoningConfig `json:"reasoning,omitempty"`
	Tools             []AdditionalTool `json:"tools,omitempty"`
	ParallelToolCalls bool             `json:"parallel_tool_calls"`
}

type ClientMetadata struct {
	SessionID string `json:"session_id"`
	ThreadID  string `json:"thread_id"`
	TurnID    string `json:"turn_id"`
}

type ReasoningConfig struct {
	Effort  string `json:"effort,omitempty"`
	Summary string `json:"summary,omitempty"`
	Context string `json:"context,omitempty"`
}

type Event struct {
	Type        EventType       `json:"type"`
	ItemID      string          `json:"item_id,omitempty"`
	Delta       *string         `json:"delta,omitempty"`
	Item        *Item           `json:"item,omitempty"`
	Response    *Terminal       `json:"-"`
	ResponseRaw json.RawMessage `json:"-"`
	Raw         json.RawMessage `json:"-"`
}

func (event *Event) UnmarshalJSON(data []byte) error {
	if len(data) == 0 || data[0] != '{' {
		return errors.New("Responses event must be an object")
	}
	var wire struct {
		Type     EventType       `json:"type"`
		ItemID   string          `json:"item_id"`
		Delta    *string         `json:"delta"`
		Item     *Item           `json:"item"`
		Response json.RawMessage `json:"response"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return err
	}
	if wire.Type == "" {
		return errors.New("Responses event has no type")
	}
	var response *Terminal
	if len(wire.Response) > 0 && bytes.HasPrefix(bytes.TrimSpace(wire.Response), []byte("{")) {
		response = new(Terminal)
		if err := json.Unmarshal(wire.Response, response); err != nil {
			return err
		}
	}
	*event = Event{
		Type: wire.Type, ItemID: wire.ItemID, Delta: wire.Delta, Item: wire.Item,
		Response: response, ResponseRaw: protocol.CloneJSON(wire.Response), Raw: protocol.CloneJSON(data),
	}
	return nil
}

func (event Event) MarshalJSON() ([]byte, error) {
	if len(event.Raw) != 0 {
		return protocol.CloneJSON(event.Raw), nil
	}
	type wire Event
	return json.Marshal(wire(event))
}

type Terminal struct {
	Status json.RawMessage `json:"status,omitempty"`
	Usage  json.RawMessage `json:"usage,omitempty"`
	Raw    json.RawMessage `json:"-"`
}

func (terminal *Terminal) UnmarshalJSON(data []byte) error {
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
	*terminal = Terminal{Status: protocol.CloneJSON(wire.Status), Usage: protocol.CloneJSON(wire.Usage), Raw: protocol.CloneJSON(data)}
	return nil
}

func (terminal Terminal) StatusValue() (string, bool) {
	if len(terminal.Status) == 0 {
		return "", false
	}
	var status string
	if err := json.Unmarshal(terminal.Status, &status); err != nil {
		return "", false
	}
	return status, true
}

func (terminal Terminal) MarshalJSON() ([]byte, error) {
	if len(terminal.Raw) != 0 {
		return protocol.CloneJSON(terminal.Raw), nil
	}
	type wire Terminal
	return json.Marshal(wire(terminal))
}

func (terminal Terminal) TotalTokens() *int {
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
	if err := json.Unmarshal(usage.TotalTokens, &tokens); err != nil || int64(int(tokens)) != tokens {
		return nil
	}
	value := int(tokens)
	return &value
}

type Response struct {
	Terminal *Terminal
	Output   []Item
	Events   []Event
}

// Audit is the stored result of a Responses sampling or compaction intent.
type Audit struct {
	Events   []Event   `json:"events"`
	Response *Terminal `json:"response"`
}

func (response Response) Audit() *Audit {
	return &Audit{Response: response.Terminal, Events: response.Events}
}

func Message(role, text string) (Item, error) {
	encoded, err := json.Marshal(text)
	if err != nil {
		return Item{}, err
	}
	return Item{Type: ItemMessage, Role: role, Content: encoded}, nil
}

func Text(item Item) (string, error) {
	parts, err := item.TextParts()
	if err != nil {
		return "", err
	}
	separator := ""
	if item.Type == ItemReasoning {
		separator = "\n"
	}
	texts := make([]string, len(parts))
	for index, part := range parts {
		texts[index] = part.Text
	}
	return strings.Join(texts, separator), nil
}
