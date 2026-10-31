package anthropic

import (
	"bytes"
	"encoding/json"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
)

// Wire structs declare fields in JSON key order: their encoding stays byte-identical to the
// sorted-key encoding the stored records and cached prompt prefixes were built from.

type Role string

const (
	RoleUser      Role = "user"
	RoleAssistant Role = "assistant"
	// RoleSystem only carries a per-message effort control.
	RoleSystem Role = "system"
)

type BlockType string

const (
	BlockText             BlockType = "text"
	BlockImage            BlockType = "image"
	BlockToolUse          BlockType = "tool_use"
	BlockToolResult       BlockType = "tool_result"
	BlockThinking         BlockType = "thinking"
	BlockRedactedThinking BlockType = "redacted_thinking"
	BlockCompaction       BlockType = "compaction"
)

type StopReason string

const (
	StopEndTurn    StopReason = "end_turn"
	StopToolUse    StopReason = "tool_use"
	StopCompaction StopReason = "compaction"
)

type DeltaType string

const (
	DeltaText      DeltaType = "text_delta"
	DeltaThinking  DeltaType = "thinking_delta"
	DeltaSignature DeltaType = "signature_delta"
	DeltaInputJSON DeltaType = "input_json_delta"
)

func roleOf(fields map[string]json.RawMessage) Role { return Role(rawString(fields, "role")) }

func blockTypeOf(fields map[string]json.RawMessage) BlockType {
	return BlockType(rawString(fields, "type"))
}

func stopReasonOf(fields map[string]json.RawMessage) StopReason {
	return StopReason(rawString(fields, "stop_reason"))
}

// inputBlock is a content block the sampler authors; provider-authored blocks stay raw JSON.
type inputBlock interface{ inputBlockType() BlockType }

type textBlock struct {
	Text string    `json:"text"`
	Type BlockType `json:"type"`
}

type imageBlock struct {
	Source base64ImageSource `json:"source"`
	Type   BlockType         `json:"type"`
}

type base64ImageSource struct {
	Data      string `json:"data"`
	MediaType string `json:"media_type"`
	Type      string `json:"type"`
}

type toolResultBlock struct {
	Content   []inputBlock `json:"content"`
	ToolUseID string       `json:"tool_use_id"`
	Type      BlockType    `json:"type"`
}

func (textBlock) inputBlockType() BlockType       { return BlockText }
func (imageBlock) inputBlockType() BlockType      { return BlockImage }
func (toolResultBlock) inputBlockType() BlockType { return BlockToolResult }

func newTextBlock(text string) textBlock { return textBlock{Text: text, Type: BlockText} }

func newImageBlock(mediaType, data string) imageBlock {
	return imageBlock{Source: base64ImageSource{Data: data, MediaType: mediaType, Type: "base64"}, Type: BlockImage}
}

func newToolResultBlock(callID string, content []inputBlock) toolResultBlock {
	return toolResultBlock{Content: content, ToolUseID: callID, Type: BlockToolResult}
}

// inputMessage is a message the sampler authors: user input or a system effort control.
type inputMessage struct {
	Content      []inputBlock  `json:"content"`
	OutputConfig *OutputConfig `json:"output_config,omitempty"`
	Role         Role          `json:"role"`
}

// wireMessage is a stored message with response-envelope metadata removed.
type wireMessage struct {
	Content      json.RawMessage `json:"content"`
	OutputConfig json.RawMessage `json:"output_config,omitempty"`
	Role         json.RawMessage `json:"role"`
}

type OutputConfig struct {
	Effort string `json:"effort"`
}

// Request is a Messages API request body.
type Request struct {
	CacheControl *CacheControl `json:"cache_control,omitempty"`
	// Compaction is set only on a compaction request.
	Compaction   *CompactionDirective `json:"compaction,omitempty"`
	Diagnostics  *Diagnostics         `json:"diagnostics,omitempty"`
	MaxTokens    int                  `json:"max_tokens"`
	Messages     []json.RawMessage    `json:"messages"`
	Model        string               `json:"model"`
	OutputConfig *OutputConfig        `json:"output_config,omitempty"`
	Stream       bool                 `json:"stream"`
	System       string               `json:"system"`
	Thinking     *ThinkingConfig      `json:"thinking,omitempty"`
	Tools        []Tool               `json:"tools"`
}

type CacheControl struct {
	TTL  PromptCacheTTL `json:"ttl"`
	Type string         `json:"type"`
}

type CompactionDirective struct {
	Type string `json:"type"`
}

type Diagnostics struct {
	// PreviousMessageID is null on the first request of a conversation.
	PreviousMessageID *string `json:"previous_message_id"`
}

type Tool struct {
	Description         string          `json:"description"`
	EagerInputStreaming bool            `json:"eager_input_streaming"`
	InputSchema         toolInputSchema `json:"input_schema"`
	Name                string          `json:"name"`
	Strict              bool            `json:"strict"`
}

// toolInputSchema is the JSON Schema subset the exec tool parameters use. Strict tool decoding
// rejects minimum/maximum, so they are accepted and dropped (the host enforces code XOR cell_id
// and numeric bounds); any other keyword is rejected rather than silently lost.
type toolInputSchema struct {
	AdditionalProperties *bool                      `json:"additionalProperties,omitempty"`
	Description          string                     `json:"description,omitempty"`
	Properties           map[string]toolInputSchema `json:"properties,omitempty"`
	Required             []string                   `json:"required,omitempty"`
	Type                 string                     `json:"type,omitempty"`
}

func (schema *toolInputSchema) UnmarshalJSON(data []byte) error {
	type fields toolInputSchema
	var wire struct {
		fields
		Schema  json.RawMessage `json:"$schema"`
		Minimum json.RawMessage `json:"minimum"`
		Maximum json.RawMessage `json:"maximum"`
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&wire); err != nil {
		return err
	}
	*schema = toolInputSchema(wire.fields)
	return nil
}

func decodeToolInputSchema(parameters json.RawMessage) (toolInputSchema, error) {
	var schema toolInputSchema
	if err := json.Unmarshal(parameters, &schema); err != nil {
		return toolInputSchema{}, err
	}
	if schema.Type != "object" {
		return toolInputSchema{}, errors.New("Anthropic exec tool parameters must be an object schema")
	}
	return schema, nil
}

// Audit is the stored result of an Anthropic sampling or compaction intent.
type Audit struct {
	ContextTokens     *int              `json:"context_tokens"`
	Events            []json.RawMessage `json:"events"`
	HTTPStatus        int               `json:"http_status"`
	ProviderRequestID string            `json:"provider_request_id"`
	// Replacement is the validated compacted context; set only on a compaction result.
	Replacement *Context        `json:"replacement,omitempty"`
	Response    json.RawMessage `json:"response"`
}

// streamEvent is the evidence of one Messages SSE event, kept as received.
type streamEvent struct {
	WireEvent string `json:"wire_event"`
}

func (streamEvent) Kind() protocol.ProviderEvidenceKind { return protocol.EvidenceAnthropicEvent }
