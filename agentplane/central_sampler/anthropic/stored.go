package anthropic

import (
	"bytes"
	"encoding/json"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/storage"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
)

const (
	ContentAnthropicMessage          storage.ContentKind = "anthropic_message"
	ContentAnthropicText             storage.ContentKind = "anthropic_text"
	ContentAnthropicThinking         storage.ContentKind = "anthropic_thinking"
	ContentAnthropicRedactedThinking storage.ContentKind = "anthropic_redacted_thinking"
	ContentAnthropicToolUse          storage.ContentKind = "anthropic_tool_use"
	ContentAnthropicToolResult       storage.ContentKind = "anthropic_tool_result"
	ContentAnthropicImage            storage.ContentKind = "anthropic_image"
	ContentAnthropicCompaction       storage.ContentKind = "anthropic_compaction"
	ContentAnthropicRequest          storage.ContentKind = "anthropic_request"
	ContentAnthropicTool             storage.ContentKind = "anthropic_tool"
)

type contentMode string

const (
	contentModeAbsent contentMode = "absent"
	contentModeArray  contentMode = "array"
	contentModeInline contentMode = "inline"
)

// messageContent stores the provider's envelope fields and references each ordered content block.
// Fields are provider field names with their original JSON values; keeping these as RawMessage
// preserves numbers and metadata the sampler does not interpret.
type messageContent struct {
	Fields      map[string]json.RawMessage `json:"fields"`
	ContentMode contentMode                `json:"content_mode"`
	Blocks      []storage.ContentID        `json:"blocks,omitempty"`
}

// requestContent removes the repetitive system prompt and tool definitions from a request
// profile. Message records are returned separately in their original order.
type requestContent struct {
	Fields   requestMetadata     `json:"fields"`
	SystemID storage.ContentID   `json:"system_id"`
	Tools    []storage.ContentID `json:"tools"`
}

type requestMetadata struct {
	CacheControl *CacheControl        `json:"cache_control,omitempty"`
	Compaction   *CompactionDirective `json:"compaction,omitempty"`
	Diagnostics  *Diagnostics         `json:"diagnostics,omitempty"`
	MaxTokens    int                  `json:"max_tokens"`
	Model        string               `json:"model"`
	OutputConfig *OutputConfig        `json:"output_config,omitempty"`
	Stream       bool                 `json:"stream"`
	Thinking     *ThinkingConfig      `json:"thinking,omitempty"`
}

// StoreRecord stores a native Anthropic message or response envelope as typed content rows.
// Complete content blocks deduplicate as units and remain in their original order.
func StoreRecord(writer storage.ContentWriter, record json.RawMessage) (storage.ContentID, error) {
	fields, err := decodeObject(record)
	if err != nil {
		return "", fmt.Errorf("decode Anthropic record: %w", err)
	}
	content := messageContent{Fields: fields, ContentMode: contentModeAbsent}
	if raw, ok := fields["content"]; ok {
		var blocks []json.RawMessage
		switch {
		case isJSONArray(raw):
			if err := json.Unmarshal(raw, &blocks); err != nil {
				return "", fmt.Errorf("decode Anthropic message content: %w", err)
			}
			content.ContentMode = contentModeArray
			content.Blocks = make([]storage.ContentID, 0, len(blocks))
			for index, block := range blocks {
				id, err := storeBlock(writer, block)
				if err != nil {
					return "", fmt.Errorf("store Anthropic content block %d: %w", index, err)
				}
				content.Blocks = append(content.Blocks, id)
			}
			delete(fields, "content")
		default:
			// Scalar, null and extension forms stay inline; arrays reference whole blocks.
			content.ContentMode = contentModeInline
		}
	}
	return putContent(writer, ContentAnthropicMessage, content)
}

// RestoreRecord resolves an Anthropic message/response content graph back to provider JSON.
func RestoreRecord(reader storage.ContentReader, id storage.ContentID) (json.RawMessage, error) {
	stored, err := loadContent(reader, id, ContentAnthropicMessage)
	if err != nil {
		return nil, err
	}
	var content messageContent
	if err := json.Unmarshal(stored.Payload, &content); err != nil {
		return nil, fmt.Errorf("decode stored Anthropic message %s: %w", id, err)
	}
	fields := cloneFields(content.Fields)
	switch content.ContentMode {
	case contentModeAbsent:
	case contentModeInline:
		if _, ok := fields["content"]; !ok {
			return nil, fmt.Errorf("stored Anthropic message %s has inline content mode without content", id)
		}
	case contentModeArray:
		blocks := make([]json.RawMessage, 0, len(content.Blocks))
		for index, blockID := range content.Blocks {
			block, err := restoreBlock(reader, blockID)
			if err != nil {
				return nil, fmt.Errorf("restore Anthropic content block %d: %w", index, err)
			}
			blocks = append(blocks, block)
		}
		raw, err := json.Marshal(blocks)
		if err != nil {
			return nil, fmt.Errorf("encode Anthropic content blocks: %w", err)
		}
		fields["content"] = raw
	default:
		return nil, fmt.Errorf("stored Anthropic message %s has unknown content mode %q", id, content.ContentMode)
	}
	raw, err := json.Marshal(fields)
	if err != nil {
		return nil, fmt.Errorf("encode Anthropic message %s: %w", id, err)
	}
	return raw, nil
}

// StoreRequest stores a native Anthropic request as a reusable profile plus ordered messages.
// Diagnostics and other per-call fields stay on the profile, while system and tools are shared.
func StoreRequest(writer storage.ContentWriter, request Request) (storage.ContentID, []storage.ContentID, error) {
	metadata := requestMetadata{
		CacheControl: request.CacheControl,
		Compaction:   request.Compaction,
		Diagnostics:  request.Diagnostics,
		MaxTokens:    request.MaxTokens,
		Model:        request.Model,
		OutputConfig: request.OutputConfig,
		Stream:       request.Stream,
		Thinking:     request.Thinking,
	}
	systemID, err := putContent(writer, storage.ContentText, request.System)
	if err != nil {
		return "", nil, fmt.Errorf("store Anthropic system prompt: %w", err)
	}
	toolIDs := make([]storage.ContentID, 0, len(request.Tools))
	for index, tool := range request.Tools {
		toolJSON, err := json.Marshal(tool)
		if err != nil {
			return "", nil, fmt.Errorf("encode Anthropic tool %d: %w", index, err)
		}
		id, err := writer.PutContent(ContentAnthropicTool, toolJSON)
		if err != nil {
			return "", nil, fmt.Errorf("store Anthropic tool %d: %w", index, err)
		}
		toolIDs = append(toolIDs, id)
	}
	messageIDs := make([]storage.ContentID, 0, len(request.Messages))
	for index, message := range request.Messages {
		id, err := StoreRecord(writer, message)
		if err != nil {
			return "", nil, fmt.Errorf("store Anthropic request message %d: %w", index, err)
		}
		messageIDs = append(messageIDs, id)
	}
	profile, err := putContent(writer, ContentAnthropicRequest, requestContent{
		Fields: metadata, SystemID: systemID, Tools: toolIDs,
	})
	if err != nil {
		return "", nil, fmt.Errorf("store Anthropic request profile: %w", err)
	}
	return profile, messageIDs, nil
}

// RestoreRequest reconstructs a request from its shared profile and already-restored messages.
func RestoreRequest(reader storage.ContentReader, profile storage.ContentID, records []json.RawMessage) ([]byte, error) {
	stored, err := loadContent(reader, profile, ContentAnthropicRequest)
	if err != nil {
		return nil, err
	}
	var content requestContent
	if err := json.Unmarshal(stored.Payload, &content); err != nil {
		return nil, fmt.Errorf("decode stored Anthropic request profile %s: %w", profile, err)
	}
	system, err := loadString(reader, content.SystemID, storage.ContentText)
	if err != nil {
		return nil, fmt.Errorf("restore Anthropic system prompt: %w", err)
	}
	tools := make([]json.RawMessage, len(content.Tools))
	for index, toolID := range content.Tools {
		tool, err := loadContent(reader, toolID, ContentAnthropicTool)
		if err != nil {
			return nil, fmt.Errorf("restore Anthropic tool %d: %w", index, err)
		}
		tools[index] = tool.Payload
	}
	return json.Marshal(struct {
		requestMetadata
		System   json.RawMessage   `json:"system"`
		Tools    []json.RawMessage `json:"tools"`
		Messages []json.RawMessage `json:"messages"`
	}{content.Fields, system, tools, records})
}

// ContentReferences returns the provider-owned children of one stored Anthropic row.
// The typed payloads keep context hydration bounded to a small number of batched reads.
func ContentReferences(kind storage.ContentKind, payload json.RawMessage) ([]storage.ContentID, error) {
	var refs []storage.ContentID
	appendRef := func(id storage.ContentID, field string) error {
		if id == "" {
			return fmt.Errorf("stored Anthropic %s has no %s reference: %w", kind, field, protocol.ErrInvalid)
		}
		refs = append(refs, id)
		return nil
	}
	appendRefs := func(ids []storage.ContentID, field string) error {
		for index, id := range ids {
			if err := appendRef(id, fmt.Sprintf("%s[%d]", field, index)); err != nil {
				return err
			}
		}
		return nil
	}

	switch kind {
	case ContentAnthropicMessage:
		var content messageContent
		if err := json.Unmarshal(payload, &content); err != nil {
			return nil, fmt.Errorf("decode stored Anthropic message references: %w", err)
		}
		switch content.ContentMode {
		case contentModeAbsent, contentModeInline:
		case contentModeArray:
			if err := appendRefs(content.Blocks, "blocks"); err != nil {
				return nil, err
			}
		default:
			return nil, fmt.Errorf("stored Anthropic message has unknown content mode %q: %w", content.ContentMode, protocol.ErrInvalid)
		}
	case ContentAnthropicRequest:
		var content requestContent
		if err := json.Unmarshal(payload, &content); err != nil {
			return nil, fmt.Errorf("decode stored Anthropic request references: %w", err)
		}
		if err := appendRef(content.SystemID, "system"); err != nil {
			return nil, err
		}
		if err := appendRefs(content.Tools, "tools"); err != nil {
			return nil, err
		}
	}
	return refs, nil
}

// A native block is atomic: text, signatures, images and nested tool results stay
// together. Sharing the complete block does not permit moving it in provider history.
func storeBlock(writer storage.ContentWriter, raw json.RawMessage) (storage.ContentID, error) {
	fields, err := decodeObject(raw)
	if err != nil {
		return "", fmt.Errorf("decode Anthropic block: %w", err)
	}
	kind := blockStorageKind(BlockType(rawString(fields, "type")))
	if kind == "" {
		return "", fmt.Errorf("unsupported Anthropic content block: %w", protocol.ErrInvalid)
	}
	return writer.PutContent(kind, raw)
}

func restoreBlock(reader storage.ContentReader, id storage.ContentID) (json.RawMessage, error) {
	stored, err := reader.GetContent(id)
	if err != nil {
		return nil, fmt.Errorf("load Anthropic block %s: %w", id, err)
	}
	if err := storage.VerifyContent(stored); err != nil {
		return nil, err
	}
	fields, err := decodeObject(stored.Payload)
	if err != nil {
		return nil, fmt.Errorf("decode stored Anthropic block %s: %w", id, err)
	}
	blockType := BlockType(rawString(fields, "type"))
	kind := blockStorageKind(blockType)
	if kind == "" || stored.Kind != kind {
		return nil, fmt.Errorf("stored Anthropic block %s has type %q in kind %q", id, blockType, stored.Kind)
	}
	return protocol.CloneJSON(stored.Payload), nil
}

func blockStorageKind(blockType BlockType) storage.ContentKind {
	switch blockType {
	case BlockText:
		return ContentAnthropicText
	case BlockThinking:
		return ContentAnthropicThinking
	case BlockRedactedThinking:
		return ContentAnthropicRedactedThinking
	case BlockToolUse:
		return ContentAnthropicToolUse
	case BlockToolResult:
		return ContentAnthropicToolResult
	case BlockImage:
		return ContentAnthropicImage
	case BlockCompaction:
		return ContentAnthropicCompaction
	default:
		return ""
	}
}

func putContent(writer storage.ContentWriter, kind storage.ContentKind, value any) (storage.ContentID, error) {
	payload, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	return writer.PutContent(kind, payload)
}

func loadContent(reader storage.ContentReader, id storage.ContentID, kind storage.ContentKind) (storage.StoredContent, error) {
	content, err := reader.GetContent(id)
	if err != nil {
		return storage.StoredContent{}, fmt.Errorf("load content %s: %w", id, err)
	}
	if err := storage.VerifyContent(content); err != nil {
		return storage.StoredContent{}, err
	}
	if content.Kind != kind {
		return storage.StoredContent{}, fmt.Errorf("content %s has kind %q, want %q", id, content.Kind, kind)
	}
	return content, nil
}

func loadString(reader storage.ContentReader, id storage.ContentID, kind storage.ContentKind) (json.RawMessage, error) {
	if id == "" {
		return nil, fmt.Errorf("missing %s content reference", kind)
	}
	content, err := loadContent(reader, id, kind)
	if err != nil {
		return nil, err
	}
	var value string
	if err := json.Unmarshal(content.Payload, &value); err != nil {
		return nil, fmt.Errorf("content %s is not a JSON string: %w", id, err)
	}
	return append(json.RawMessage(nil), content.Payload...), nil
}

func decodeObject(raw []byte) (map[string]json.RawMessage, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return nil, err
	}
	if fields == nil {
		return nil, fmt.Errorf("expected JSON object")
	}
	return fields, nil
}

func cloneFields(fields map[string]json.RawMessage) map[string]json.RawMessage {
	copyOfFields := make(map[string]json.RawMessage, len(fields)+2)
	for name, raw := range fields {
		copyOfFields[name] = append(json.RawMessage(nil), raw...)
	}
	return copyOfFields
}

func isJSONArray(raw json.RawMessage) bool {
	trimmed := bytes.TrimSpace(raw)
	return len(trimmed) > 0 && trimmed[0] == '['
}

// FreezeRequest retains the authored definitions associated with native continuation.
func FreezeRequest(reader storage.ContentReader, profile storage.ContentID, body Request) (Request, error) {
	raw, err := RestoreRequest(reader, profile, nil)
	if err != nil {
		return Request{}, err
	}
	var previous Request
	if err := json.Unmarshal(raw, &previous); err != nil {
		return Request{}, err
	}
	body.System = previous.System
	body.Tools = previous.Tools
	return body, nil
}
